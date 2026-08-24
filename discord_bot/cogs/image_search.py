"""
Discord port of handlers/image_search_handler.py (+ modules/image_search.py,
which is reused as-is — no changes needed there, it's already
platform-agnostic).

Flow, same shape as Telegram's:
  /imagesearch image:<attachment> -> reverse image search via Yandex ->
  preview thumbnails posted immediately, unblurred, in full -> only the
  *source links* are gated: 1 free reveal per user (tracked in the same
  users.free_image_search_used column Telegram uses, so a user's free
  reveal is shared across both platforms), then GHS 10 (config.PRICE_REGISTRY
  "image_search_unlock", overridable per clone) via the same
  initialize/verify Paystack pattern as media_connect.py and ai_store.py.

  A second, separate paywall offers a direct "Open in Yandex" link
  (IMAGE_SEARCH_YANDEX_FEE_GHS/month subscription) — reuses the exact same
  image_search_yandex_subscriptions table and db methods Telegram's
  handlers/image_search_handler.py already writes to, so a subscription
  bought on one platform is active on the other too (same user_id, clone_id
  scoping).

Bot owner (DISCORD_CLONE_ADMIN_IDS) bypasses both paywalls, same convention
as admin.py/clone_admin.py.

Search results and the pending Yandex image URL are kept in-memory on the
view itself (not context.user_data, since discord.py has no per-user
scratch dict) — same "expires, resend the image" tradeoff Telegram's
context.user_data-based version already has.
"""

import asyncio
import logging

import discord
from discord import app_commands
from discord.ext import commands

from database import db
from payments import paystack, stripe_gateway, resolve_gateway, resolve_gateway_for_provider, gateway_charge_amount, charge_error_message
from modules.image_search import reverse_image_search
from config import DISCORD_CLONE_ADMIN_IDS, IMAGE_SEARCH_YANDEX_FEE_GHS, IMAGE_SEARCH_YANDEX_DAYS
from discord_bot.cogs._views_shared import ActionButton

logger = logging.getLogger(__name__)

MAX_PREVIEWS = 5  # matches modules.image_search.reverse_image_search's default max_results


def _is_owner(user_id: int) -> bool:
    return user_id in DISCORD_CLONE_ADMIN_IDS


def _clone_id_of(interaction: discord.Interaction) -> int:
    return getattr(interaction.client, "clone_id", None) or 0


def _yandex_url(image_url: str) -> str:
    from urllib.parse import quote
    return f"https://yandex.com/images/search?url={quote(image_url, safe='')}&rpt=imageview"


def _links_embed(results: list) -> discord.Embed:
    embed = discord.Embed(title="Source links", color=discord.Color.blurple())
    lines = []
    for i, r in enumerate(results, 1):
        url = r.get("url", "#")
        title = (r.get("title") or f"Result {i}")[:60]
        lines.append(f"{i}. [{title}]({url})")
    embed.description = "\n".join(lines)
    return embed


class RevealView(discord.ui.View):
    """Free-reveal / pay-to-unlock buttons attached to the results message.
    Holds the search results in memory (5 min timeout — plenty for someone
    to decide whether to pay)."""

    def __init__(self, cog: "ImageSearchCog", results: list, free_available: bool, price: float):
        super().__init__(timeout=300)
        self.cog = cog
        self.results = results
        if free_available:
            self.add_item(RevealButton(cog, "🆓 Reveal Source Links (Free)", discord.ButtonStyle.success, free=True))
        else:
            self.add_item(RevealButton(cog, f"🔓 Unlock Source Links — GHS {price:g}", discord.ButtonStyle.primary, free=False))


class RevealButton(discord.ui.Button):
    def __init__(self, cog: "ImageSearchCog", label: str, style: discord.ButtonStyle, free: bool):
        super().__init__(label=label, style=style)
        self.cog = cog
        self.free = free

    async def callback(self, interaction: discord.Interaction):
        view: RevealView = self.view
        if self.free:
            await self.cog.handle_free_unlock(interaction, view.results)
        else:
            await self.cog.handle_pay_unlock(interaction, view.results)


class VerifyUnlockView(discord.ui.View):
    def __init__(self, cog: "ImageSearchCog", results: list, reference: str, provider: str, api_key: str):
        super().__init__(timeout=900)
        self.cog = cog
        self.results = results
        self.reference = reference
        self.provider = provider
        self.api_key = api_key
        self.add_item(ActionButton("✅ Verify Payment", discord.ButtonStyle.success, cog, "handle_verify_unlock_button"))


class YandexOfferView(discord.ui.View):
    def __init__(self, cog: "ImageSearchCog", image_url: str, unlocked: bool):
        super().__init__(timeout=300)
        self.cog = cog
        self.image_url = image_url
        if unlocked:
            self.add_item(discord.ui.Button(label="🔎 Open in Yandex", style=discord.ButtonStyle.link, url=_yandex_url(image_url)))
        else:
            self.add_item(ActionButton(
                f"🔎 Open in Yandex — GHS {IMAGE_SEARCH_YANDEX_FEE_GHS}/month",
                discord.ButtonStyle.secondary, cog, "handle_yandex_subscribe",
            ))


class VerifyYandexView(discord.ui.View):
    def __init__(self, cog: "ImageSearchCog"):
        super().__init__(timeout=900)
        self.add_item(ActionButton("✅ Verify Payment", discord.ButtonStyle.success, cog, "handle_yandex_verify"))


class ImageSearchCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # user_id -> {"reference", "provider", "api_key", "results"} for
        # in-flight source-link unlock payments; separate from Yandex sub
        # payments below since both can be pending at once.
        self._pending_unlock: dict[int, dict] = {}
        self._pending_yandex: dict[int, dict] = {}
        # user_id -> last searched image URL, so the Yandex subscribe/verify
        # buttons (which don't carry results) know what to link to.
        self._last_image_url: dict[int, str] = {}

    @app_commands.command(name="imagesearch", description="Reverse image search — find where an image is from")
    @app_commands.describe(image="The image to search for")
    async def imagesearch(self, interaction: discord.Interaction, image: discord.Attachment):
        if not (image.content_type or "").startswith("image/"):
            await interaction.response.send_message("That attachment isn't an image.", ephemeral=True)
            return

        await interaction.response.defer()
        image_url = image.url
        self._last_image_url[interaction.user.id] = image_url

        results = await reverse_image_search(image_url, max_results=MAX_PREVIEWS)

        if results is None:
            detail = getattr(reverse_image_search, "last_error", None)
            if _is_owner(interaction.user.id) and detail:
                await interaction.followup.send(f"⚠️ Image search error (admin detail):\n{detail[:500]}")
            elif detail and "didn't respond" in detail:
                await interaction.followup.send(f"⚠️ {detail}")
            else:
                await interaction.followup.send("⚠️ Reverse image search is temporarily unavailable. Try again later.")
            return

        if not results:
            await interaction.followup.send("No matches found for this image.")
            await self._send_yandex_option(interaction, interaction.user.id, image_url, followup=True)
            return

        # Preview thumbnails are always shown in full, unblurred — only the
        # source link behind each result is gated below.
        for i, r in enumerate(results, 1):
            thumb = r.get("thumbnail")
            if not thumb:
                continue
            title = r.get("title")
            embed = discord.Embed(title=f"Match {i}" + (f" — {title[:80]}" if title else ""))
            embed.set_image(url=thumb)
            try:
                await interaction.followup.send(embed=embed)
            except discord.HTTPException as e:
                logger.warning(f"[discord] Failed to send image-search preview {i}: {e}")

        clone_id = _clone_id_of(interaction)
        user = await db.get_user(interaction.user.id, clone_id=clone_id)
        free_used = bool(user and user.get("free_image_search_used"))

        if _is_owner(interaction.user.id):
            await interaction.followup.send(
                f"Found {len(results)} match(es). Owner bypass — revealing links now.",
                embed=_links_embed(results),
            )
        elif not free_used:
            view = RevealView(self, results, free_available=True, price=0)
            await interaction.followup.send(
                f"Found {len(results)} match(es). You get 1 free source-link reveal — this one's on the house.",
                view=view,
            )
        else:
            price = await db.get_clone_price(clone_id, "image_search_unlock")
            view = RevealView(self, results, free_available=False, price=price)
            await interaction.followup.send(
                f"Found {len(results)} match(es). Unlock the source links for GHS {price:g}.",
                view=view,
            )

        await self._send_yandex_option(interaction, interaction.user.id, image_url, followup=True)

    async def _send_yandex_option(self, interaction: discord.Interaction, user_id: int, image_url: str, followup: bool):
        clone_id = _clone_id_of(interaction)
        unlocked = _is_owner(user_id) or await db.is_image_search_yandex_active(user_id, clone_id)
        view = YandexOfferView(self, image_url, unlocked)
        text = (
            "Want more matches? Open this image directly on Yandex's reverse search."
            if unlocked else
            f"Want to jump straight to Yandex's own reverse-search results for this image? "
            f"Subscribe for GHS {IMAGE_SEARCH_YANDEX_FEE_GHS}/month to unlock direct Yandex search on every image you send."
        )
        send = interaction.followup.send if followup else interaction.response.send_message
        await send(text, view=view)

    # ── Free / paid source-link reveal ──────────────────────────────────

    async def handle_free_unlock(self, interaction: discord.Interaction, results: list):
        await interaction.response.defer(ephemeral=True)
        clone_id = _clone_id_of(interaction)
        user = await db.get_user(interaction.user.id, clone_id=clone_id)
        if user and user.get("free_image_search_used"):
            await interaction.followup.send("Your free search is already used — this one needs payment.", ephemeral=True)
            return
        await db.mark_free_image_search_used(interaction.user.id, clone_id=clone_id)
        await interaction.followup.send(embed=_links_embed(results))

    async def handle_pay_unlock(self, interaction: discord.Interaction, results: list):
        await interaction.response.defer(ephemeral=True)
        clone_id = _clone_id_of(interaction)
        price = await db.get_clone_price(clone_id, "image_search_unlock")
        gateway, api_key, provider = await resolve_gateway(clone_id, platform="discord")
        email = f"user_{interaction.user.id}@discord.user"

        charge = gateway_charge_amount(provider, price)
        if charge.get("error"):
            await interaction.followup.send(charge_error_message(charge), ephemeral=True)
            return

        payment_result = await asyncio.to_thread(
            gateway.initialize_payment,
            email, charge["amount_minor_units"], interaction.user.id,
            f"ImageSearchUnlock_{interaction.user.id}",
            payment_type="image_search_unlock",
            extra_metadata={"clone_id": clone_id, "provider": "discord"},
            api_key=api_key,
            currency=charge["currency"],
        )

        if not payment_result or payment_result.get("status") != "success":
            logger.error(f"[discord] Payment init failed for image-search unlock, user {interaction.user.id}")
            await interaction.followup.send("❌ Couldn't start checkout right now. Try again shortly.", ephemeral=True)
            return

        reference = payment_result.get("reference")
        self._pending_unlock[interaction.user.id] = {
            "reference": reference, "provider": provider, "api_key": api_key, "results": results,
        }
        # Persist too — the in-memory dict above is lost on a restart, and
        # there's otherwise no webhook backstop for this payment_type (see
        # api/paystack_webhook.py's 'image_search_unlock' case), so a user
        # who pays but never taps Verify (or does, after a restart wiped
        # the in-memory state) would be charged with no way to get their
        # links.
        await db.start_image_search_unlock_payment(interaction.user.id, clone_id, reference, results, provider=provider)
        view = VerifyUnlockView(self, results, reference, provider, api_key)
        charged_amount_display = (
            f"GHS {price:g}.00" if charge["currency"] == "GHS"
            else f"{charge['amount_minor_units'] / 100:.2f} {charge['currency'].upper()} (converted from GHS {price:g})"
        )
        await interaction.followup.send(
            f"💳 **Unlock Source Links**\nPay {charged_amount_display}, then tap **Verify Payment**.\n"
            f"[Complete payment]({payment_result.get('authorization_url')})",
            view=view, ephemeral=True,
        )

    async def handle_verify_unlock_button(self, interaction: discord.Interaction):
        pending = self._pending_unlock.get(interaction.user.id)
        if pending:
            gateway = stripe_gateway if pending["provider"] == "stripe" else paystack
            result = await asyncio.to_thread(gateway.verify_payment, pending["reference"], api_key=pending["api_key"])
            if result.get("status") == "success":
                self._pending_unlock.pop(interaction.user.id, None)
                await interaction.response.send_message(embed=_links_embed(pending["results"]))
                return
            await interaction.response.send_message("Payment not confirmed yet. Complete payment, then tap Verify again.", ephemeral=True)
            return

        # In-memory state is gone (bot restart) — fall back to the
        # persisted row. It may already be 'completed' if the webhook beat
        # the user to it (api/paystack_webhook.py's 'image_search_unlock'
        # case); either way we still have the paid-for results to show.
        db_pending = await db.get_image_search_unlock_payment_for_user(interaction.user.id)
        if not db_pending:
            await interaction.response.send_message("No pending payment found.", ephemeral=True)
            return
        if db_pending["status"] != "completed":
            gateway, api_key = await resolve_gateway_for_provider(db_pending["clone_id"], db_pending.get("provider"), platform="discord")
            result = await asyncio.to_thread(gateway.verify_payment, db_pending["payment_reference"], api_key=api_key)
            if result.get("status") != "success":
                await interaction.response.send_message("Payment not confirmed yet. Complete payment, then tap Verify again.", ephemeral=True)
                return
            await db.complete_image_search_unlock_payment(db_pending["payment_reference"])
        await interaction.response.send_message(embed=_links_embed(db_pending["results"]))

    # ── Yandex direct-search subscription ───────────────────────────────

    async def handle_yandex_subscribe(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        clone_id = _clone_id_of(interaction)
        email = f"user_{interaction.user.id}@discord.user"

        # Route through the same clone-aware gateway/currency path as the
        # source-link unlock above (handle_pay_unlock) — this used to be
        # hardcoded to paystack.initialize_payment with a raw GHS amount,
        # bypassing both a clone's own connected Stripe key and any
        # non-GHS currency conversion.
        gateway, api_key, provider = await resolve_gateway(clone_id, platform="discord")
        charge = gateway_charge_amount(provider, IMAGE_SEARCH_YANDEX_FEE_GHS)
        if charge.get("error"):
            await interaction.followup.send(charge_error_message(charge), ephemeral=True)
            return

        payment_result = await asyncio.to_thread(
            gateway.initialize_payment,
            email, charge["amount_minor_units"], interaction.user.id,
            f"YandexSearchSub_{interaction.user.id}",
            payment_type="image_search_yandex",
            extra_metadata={"clone_id": clone_id, "provider": "discord"},
            api_key=api_key,
            currency=charge["currency"],
        )

        if not payment_result or payment_result.get("status") != "success":
            logger.error(f"[discord] Payment init failed for Yandex search subscription, user {interaction.user.id}")
            await interaction.followup.send("❌ Couldn't start checkout right now. Try again shortly.", ephemeral=True)
            return

        reference = payment_result.get("reference")
        await db.start_image_search_yandex_payment(interaction.user.id, clone_id, reference, provider=provider)
        self._pending_yandex[interaction.user.id] = {"reference": reference, "provider": provider, "api_key": api_key}

        charged_amount_display = (
            f"GHS {IMAGE_SEARCH_YANDEX_FEE_GHS:g}/month" if charge["currency"] == "GHS"
            else f"{charge['amount_minor_units'] / 100:.2f} {charge['currency'].upper()}/month (converted from GHS {IMAGE_SEARCH_YANDEX_FEE_GHS:g})"
        )
        view = VerifyYandexView(self)
        await interaction.followup.send(
            f"💳 **Yandex Direct Search — {charged_amount_display}**\n"
            f"Pay, then tap **Verify Payment**. Unlocks a direct 'Open in Yandex' link on every "
            f"image you send for {IMAGE_SEARCH_YANDEX_DAYS} days.\n"
            f"[Complete payment]({payment_result.get('authorization_url')})",
            view=view, ephemeral=True,
        )

    async def handle_yandex_verify(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        clone_id = _clone_id_of(interaction)
        pending = self._pending_yandex.get(interaction.user.id)
        if not pending:
            # In-memory state is gone (bot restart) — fall back to the
            # persisted row, same restart-safety as handle_verify_unlock_button.
            db_pending = await db.get_image_search_yandex_subscription(interaction.user.id, clone_id)
            if not db_pending or not db_pending.get("payment_reference"):
                await interaction.followup.send("No pending payment found.", ephemeral=True)
                return
            if db_pending["status"] == "active":
                await interaction.followup.send("✅ Already active.", ephemeral=True)
                return
            pending = {
                "reference": db_pending["payment_reference"],
                "provider": db_pending.get("provider") or "paystack",
            }
            pending["api_key"] = None

        gateway, api_key = await resolve_gateway_for_provider(clone_id, pending.get("provider"), platform="discord")
        result = await asyncio.to_thread(gateway.verify_payment, pending["reference"], api_key=api_key)
        if result.get("status") != "success":
            await interaction.followup.send("Payment not confirmed yet. Complete payment, then tap Verify again.", ephemeral=True)
            return

        self._pending_yandex.pop(interaction.user.id, None)
        await db.activate_image_search_yandex_subscription(interaction.user.id, clone_id, days=IMAGE_SEARCH_YANDEX_DAYS)
        await db.save_image_search_yandex_authorization(interaction.user.id, clone_id, result.get("authorization_code"))

        image_url = self._last_image_url.get(interaction.user.id)
        buttons = []
        if image_url:
            buttons.append(discord.ui.Button(label="🔎 Open in Yandex", style=discord.ButtonStyle.link, url=_yandex_url(image_url)))
        renew_note = (
            "It'll auto-renew from the same card each month — use `/imagesearch` again and tap cancel from there anytime to stop that."
            if result.get("authorization_code") else
            f"Heads up: your card couldn't be saved for auto-renewal, so you'll need to resubscribe manually after {IMAGE_SEARCH_YANDEX_DAYS} days."
        )
        view = None
        if buttons:
            view = discord.ui.View(timeout=300)
            for b in buttons:
                view.add_item(b)
        await interaction.followup.send(
            f"✅ **Subscribed!** Direct Yandex search is active for the next {IMAGE_SEARCH_YANDEX_DAYS} days. {renew_note}",
            view=view,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(ImageSearchCog(bot))
