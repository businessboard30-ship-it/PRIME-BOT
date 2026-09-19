# path: payments_manual.py

"""Payment routing helpers.

Buyers in Ghana pay through Paystack (the automatic gateway path in
payments.py/resolve_gateway()); everyone else pays through Gumroad
(gumroad_payments.py, confirmed automatically by its ping webhook).

/paymentmode picks how a purchase is routed:
  - "split"   (default) the buyer chooses Ghana (Paystack) or International (Gumroad)
  - "auto"    everyone goes through Paystack/Stripe
  - "gumroad" everyone goes through Gumroad

Every path ends in the same UNLOCK_HANDLERS entry for the payment_type, so
what "paid" means never diverges between providers. The admin
Approve/Reject buttons and /approvepayment, /rejectpayment stay as a
provider-agnostic manual override.
"""

import logging
import re
import secrets
import asyncio
from typing import Optional

import discord

from database import db
from config import DISCORD_CLONE_ADMIN_IDS

logger = logging.getLogger(__name__)

PROVIDER = "gumroad"

_APPROVAL_ID_RE = r"(\d+)"


async def _resolve_approvers(bot: discord.Client, guild_id: Optional[int],
                              payment_type: Optional[str] = None) -> list[int]:
    """Main-bot admins always get the DM. If this payment happened inside
    a guild running a Discord clone, that clone's owner is added too —
    looked up via the clone's owner_id, not guesswork.

    Carve-out: discord_clone registrations (including clone-of-clone ones
    made through a hosting clone's own "Build Bot" wizard) never add the
    hosting clone's owner here, regardless of which clone process the
    registration happened through — that revenue and approval right
    belongs solely to the main project owner (DISCORD_CLONE_ADMIN_IDS),
    never to whichever clone owner happened to host the sub-clone's
    registration."""
    approvers = set(DISCORD_CLONE_ADMIN_IDS)
    if payment_type == "discord_clone":
        return list(approvers)
    clone_id = getattr(bot, "clone_id", None)
    if clone_id:
        clone = await db.get_discord_clone(clone_id)
        if clone and clone.get("owner_id"):
            approvers.add(clone["owner_id"])
    return list(approvers)


class ManualPaymentResolution:
    """Result of resolve_manual_payment_approval/_rejection — enough for a
    caller (button callback or slash command) to report back to whoever
    triggered it without needing to know the DB row shape itself."""

    def __init__(self, ok: bool, message: str, row: Optional[dict] = None):
        self.ok = ok
        self.message = message
        self.row = row


async def resolve_manual_payment_approval(bot: discord.Client, payment_id: int, amount: Optional[float] = None) -> ManualPaymentResolution:
    """Shared by _ManualPayApproveButton's click and the /approvepayment
    slash command — same lookup, same UNLOCK_HANDLERS dispatch, same
    buyer DM, so a payment approved from a command is applied identically
    to one approved from the DM card. Does NOT touch the DM card's own
    message/view (only the button callback does that, since a slash
    command has no card message to edit) — callers that DO have a card
    message handle disabling/editing it themselves after this returns ok.

    amount: the real GHS amount, confirmed by the approving admin against
    the payment dashboard (manually logged payments carry a 0.0
    placeholder amount until an admin supplies the real one). Pass None only
    for a caller that genuinely can't ask (there is currently none) — the
    payment still gets approved/unlocked, just with the old amount-blind
    behavior."""
    row = await db.get_payment_row_by_id(payment_id)
    if not row or row.get("status") != "awaiting_review":
        return ManualPaymentResolution(False, "This payment's already been resolved or wasn't found.", row)

    handler = UNLOCK_HANDLERS.get(row["payment_type"])
    if handler is None:
        return ManualPaymentResolution(
            False, f"No unlock handler wired for `{row['payment_type']}` yet — approve manually in code.", row
        )

    if amount is not None:
        await db.mark_payment_paid_with_amount(row["paystack_reference"], amount)
    else:
        await db.mark_payment_paid(row["paystack_reference"])
    await handler(row["paystack_reference"], row["user_id"], row.get("chat_id"), row.get("group_id"))
    await _notify_buyer(bot, row["user_id"], row["payment_type"], approved=True)
    return ManualPaymentResolution(True, f"Approved and unlocked `{row['payment_type']}` for <@{row['user_id']}>.", row)


async def resolve_manual_payment_rejection(bot: discord.Client, payment_id: int) -> ManualPaymentResolution:
    """Reject counterpart to resolve_manual_payment_approval — see that
    function's docstring for the shared-logic rationale."""
    row = await db.get_payment_row_by_id(payment_id)
    if not row or row.get("status") != "awaiting_review":
        return ManualPaymentResolution(False, "This payment's already been resolved or wasn't found.", row)

    await db.mark_manual_payment_rejected(payment_id)
    await _notify_buyer(bot, row["user_id"], row["payment_type"], approved=False)
    return ManualPaymentResolution(True, f"Rejected the `{row['payment_type']}` payment from <@{row['user_id']}>.", row)


class _ManualPayApproveAmountModal(discord.ui.Modal, title="Confirm amount paid"):
    """Shown when Approve is tapped — the admin is already told to check
    the payment dashboard for a matching sale before approving (see the DM
    card's own text), so this just asks them to also copy the real amount
    from there instead of the payment staying logged at its 0.0
    placeholder forever. See resolve_manual_payment_approval's docstring
    for why that placeholder exists and why nothing used to correct it."""

    amount = discord.ui.TextInput(
        label="Amount paid, in GHS (from the payment dashboard)",
        placeholder="e.g. 35.00",
        required=True, max_length=12,
    )

    def __init__(self, payment_id: int, view: "ManualApprovalView"):
        super().__init__()
        self.payment_id = payment_id
        self._approval_view = view

    async def on_submit(self, interaction: discord.Interaction):
        try:
            amount_value = float(str(self.amount).strip())
        except ValueError:
            await interaction.response.send_message(
                "That doesn't look like a number — tap Approve again and enter e.g. `35.00`.", ephemeral=True,
            )
            return
        await interaction.response.defer()

        result = await resolve_manual_payment_approval(interaction.client, self.payment_id, amount=amount_value)
        if not result.ok:
            await interaction.followup.send(result.message, ephemeral=True)
            return

        for child in self._approval_view.children:
            child.disabled = True
        await interaction.message.edit(
            content=f"{interaction.message.content}\n\n✅ **Approved** by {interaction.user.mention} — GHS {amount_value:g}",
            view=self._approval_view,
        )


class _ManualPayApproveButton(discord.ui.DynamicItem[discord.ui.Button], template=rf"^manualpay_approve:{_APPROVAL_ID_RE}$"):
    """Approve half of the admin DM card. DynamicItem (not a plain View)
    so it survives restarts: nothing about this button can rely on
    in-memory state; everything it needs is re-derived from payment_id at
    click time via a fresh DB lookup."""

    def __init__(self, payment_id: int):
        self.payment_id = payment_id
        super().__init__(discord.ui.Button(
            label="✅ Approve", style=discord.ButtonStyle.success,
            custom_id=f"manualpay_approve:{payment_id}",
        ))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: "re.Match"):
        return cls(int(match.group(1)))

    async def callback(self, interaction: discord.Interaction):
        # Modal must be the FIRST response to this interaction — can't
        # defer() first and send one after, unlike Reject below.
        await interaction.response.send_modal(_ManualPayApproveAmountModal(self.payment_id, self.view))


class _ManualPayRejectButton(discord.ui.DynamicItem[discord.ui.Button], template=rf"^manualpay_reject:{_APPROVAL_ID_RE}$"):
    def __init__(self, payment_id: int):
        self.payment_id = payment_id
        super().__init__(discord.ui.Button(
            label="❌ Reject", style=discord.ButtonStyle.danger,
            custom_id=f"manualpay_reject:{payment_id}",
        ))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: "re.Match"):
        return cls(int(match.group(1)))

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        result = await resolve_manual_payment_rejection(interaction.client, self.payment_id)
        if not result.ok:
            await interaction.followup.send(result.message, ephemeral=True)
            return

        for child in self.view.children:
            child.disabled = True
        await interaction.message.edit(
            content=f"{interaction.message.content}\n\n❌ **Rejected** by {interaction.user.mention}",
            view=self.view,
        )


class ManualApprovalView(discord.ui.View):
    """timeout=None + DynamicItem children (see above) so this survives a
    bot restart instead of expiring in-memory — a real concern here since
    review can happen well after the DM was sent."""

    def __init__(self, payment_id: int):
        super().__init__(timeout=None)
        self.payment_id = payment_id
        self.add_item(_ManualPayApproveButton(payment_id))
        self.add_item(_ManualPayRejectButton(payment_id))


# Registered in discord_bot/bot.py via bot.add_dynamic_items(*MANUAL_PAYMENT_DYNAMIC_ITEMS)
# so Approve/Reject keep working after a restart, same mechanism as every
# other persistent button in this codebase.
MANUAL_PAYMENT_DYNAMIC_ITEMS = (_ManualPayApproveButton, _ManualPayRejectButton)


async def _notify_buyer(bot: discord.Client, buyer_id: int, payment_type: str, approved: bool) -> None:
    try:
        user = await bot.fetch_user(buyer_id)
        if approved:
            await user.send(f"✅ Your payment for **{payment_type}** was confirmed and applied — enjoy!")
        else:
            await user.send(
                f"❌ We couldn't confirm your payment for **{payment_type}**. "
                f"If you already paid, contact support with your reference."
            )
    except discord.HTTPException:
        logger.warning(f"[manual-pay] couldn't DM buyer {buyer_id} about {payment_type} decision")


async def start_manual_payment(interaction: discord.Interaction, payment_type: str,
                                amount_display: str, guild_id: Optional[int] = None,
                                reference: Optional[str] = None) -> str:
    """Start a Gumroad checkout for payment_type (the international path).
    Kept under this name so existing callers don't change. Call after
    interaction.response.defer(ephemeral=True, thinking=True).

    reference: pass a pre-generated reference when the caller stashed
    payment-type-specific data (e.g. clone_admin.py's
    store_discord_clone_pending_payment) under it beforehand.
    guild_id: the guild this purchase is FOR for whole-guild unlocks,
    None for account-level purchases."""
    from gumroad_payments import start_gumroad_payment
    return await start_gumroad_payment(
        interaction, payment_type, amount_display, guild_id=guild_id, reference=reference
    )


_REGION_KEY = "pay_region:{}"


async def _saved_region(user_id: int) -> Optional[str]:
    """'ghana' / 'international' if we already know, else None. Order:
    remembered earlier choice, then a /currency the buyer set to GHS."""
    saved = await db.get_global_setting(_REGION_KEY.format(user_id))
    if saved in ("ghana", "international"):
        return saved
    try:
        if (await db.get_user_currency(user_id) or "").upper() == "GHS":
            return "ghana"
    except Exception:
        pass
    return None


class _RegionChoiceView(discord.ui.View):
    """Shown only the FIRST time a buyer pays; the answer is remembered."""

    def __init__(self, user_id: int, on_ghana, on_international):
        super().__init__(timeout=600)
        self._user_id = user_id
        self._on_ghana = on_ghana
        self._on_international = on_international

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self._user_id

    @discord.ui.button(label="Ghana — Paystack", emoji="🇬🇭", style=discord.ButtonStyle.success)
    async def ghana(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        await db.set_global_setting(_REGION_KEY.format(self._user_id), "ghana")
        await self._on_ghana(interaction)

    @discord.ui.button(label="International — Gumroad", emoji="🌍", style=discord.ButtonStyle.primary)
    async def international(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        await db.set_global_setting(_REGION_KEY.format(self._user_id), "international")
        await self._on_international(interaction)


async def offer_region_choice(interaction: discord.Interaction, *, on_ghana, on_international) -> None:
    """Call after interaction.response.defer(...). Discord never tells a bot
    where a user is, so: use what we already know (remembered choice or a
    GHS currency) and go straight to checkout; ask once only if unknown."""
    region = await _saved_region(interaction.user.id)
    if region == "ghana":
        await on_ghana(interaction)
        return
    if region == "international":
        await on_international(interaction)
        return
    await interaction.followup.send(
        "Where are you paying from? (asked once, then remembered)\n"
        "🇬🇭 **Ghana** — Paystack (Mobile Money / local cards)\n"
        "🌍 **Anywhere else** — Gumroad (international cards / PayPal)",
        view=_RegionChoiceView(interaction.user.id, on_ghana, on_international),
        ephemeral=True,
    )


# ─────────────────────────────────────────────────────────────────────
# Unlock handlers — reused as-is from the automatic path. Each takes
# (reference, buyer_id, guild_id, clone_id).
# ─────────────────────────────────────────────────────────────────────

async def _unlock_welcome_card_pack(reference: str, buyer_id: int, guild_id: Optional[int], clone_id: Optional[int]):
    await db.unlock_welcome_card_pack(guild_id, clone_id=clone_id)


async def _unlock_ultra_pack(reference: str, buyer_id: int, guild_id: Optional[int], clone_id: Optional[int]):
    await db.unlock_ultra_pack(guild_id, clone_id=clone_id)


async def _unlock_discord_clone(reference: str, buyer_id: int, guild_id: Optional[int], clone_id: Optional[int]):
    """discord_clone's pending row (token, bot_user_id, etc.) was already
    stashed by clone_admin.py BEFORE payment via
    store_discord_clone_pending_payment(reference=reference, ...) — this
    mirrors what api/paystack_webhook.py's discord_clone case does, and is
    idempotent the same way."""
    await db.complete_discord_clone_pending_payment(reference)


async def _unlock_discord_clone_monetization(reference: str, buyer_id: int, guild_id: Optional[int], clone_id: Optional[int]):
    """Mirrors activate_discord_monetization_subscription_by_reference's
    existing role as the webhook backstop for /clonemonetize activate —
    looks the target clone up by payment_reference (stashed by
    db.start_discord_monetization_payment before payment) instead of
    needing the target clone_id passed in here."""
    from config import CLONE_MONETIZATION_DAYS
    await db.activate_discord_monetization_subscription_by_reference(reference, days=CLONE_MONETIZATION_DAYS)


async def _unlock_custom_role(reference: str, buyer_id: int, guild_id: Optional[int], clone_id: Optional[int]):
    """Per-user entitlement, not the role itself — see
    db.grant_custom_role_entitlement's docstring. guild_id is required here
    (start_manual_payment must be called with guild_id=interaction.guild.id
    for this payment_type) since the perk is scoped to one guild, same as
    the wizard that consumes it."""
    await db.grant_custom_role_entitlement(guild_id, buyer_id, clone_id=clone_id)


async def _unlock_music_pro(reference: str, buyer_id: int, guild_id: Optional[int], clone_id: Optional[int]):
    """Replaces the old manual /activate-pro command (discord_bot/cogs/
    music.py) as the thing that actually flips db.discord_pro_guilds — the
    command itself still exists as a fallback, but approving here does the
    same db.set_guild_pro call automatically. guild_id is required (Music
    Pro is per-server, same as welcome_card_pack/ultra_welcome_pack) —
    start_manual_payment must be called with guild_id=interaction.guild.id
    for this payment_type."""
    await db.set_guild_pro(guild_id, True, buyer_id, clone_id=clone_id)


async def _unlock_xp_boost(reference: str, buyer_id: int, guild_id: Optional[int], clone_id: Optional[int]):
    """Per-user, per-guild temporary XP multiplier (see
    leveling-boost-build-prompt.md §2 and database/migrations/013_xp_boost.
    sql's discord_xp_boosts table). guild_id is required — the boost only
    applies in the server it was bought from, so
    discord_bot/cogs/_views_music_panel.py-style buttons for this
    payment_type must call start_manual_payment with
    guild_id=interaction.guild.id, same as custom_role/music_pro above."""
    from config import XP_BOOST_MULTIPLIER, XP_BOOST_DURATION_DAYS
    await db.activate_xp_boost(guild_id, buyer_id, XP_BOOST_MULTIPLIER, XP_BOOST_DURATION_DAYS, clone_id=clone_id)


def _make_unlock_xp_server_boost_tier(tier_key: str):
    """One handler per config.XP_SERVER_BOOST_TIERS entry — each tier is
    its own product/payment_type, but they all just activate a
    guild-wide multiplier for that tier's duration. buyer_id is whoever
    paid, but the boost itself applies to every member of guild_id, not
    just them.

    (The old XP Wallet's _make_unlock_xp_wallet_tier factory used to sit
    here — removed along with the wallet product itself; see config.py's
    XP_WALLET removal note for why.)"""
    async def _handler(reference: str, buyer_id: int, guild_id: Optional[int], clone_id: Optional[int]):
        from config import XP_SERVER_BOOST_TIERS
        tier = XP_SERVER_BOOST_TIERS[tier_key]
        await db.activate_guild_xp_boost(guild_id, tier["multiplier"], tier["duration_hours"], clone_id=clone_id)
    return _handler


UNLOCK_HANDLERS = {
    "welcome_card_pack": _unlock_welcome_card_pack,
    "ultra_welcome_pack": _unlock_ultra_pack,
    "discord_clone": _unlock_discord_clone,
    "discord_clone_monetization": _unlock_discord_clone_monetization,
    "custom_role": _unlock_custom_role,
    "music_pro": _unlock_music_pro,
    "xp_boost": _unlock_xp_boost,
    "xp_server_boost": _make_unlock_xp_server_boost_tier("xp_server_boost"),
    "xp_server_boost_month": _make_unlock_xp_server_boost_tier("xp_server_boost_month"),
}


def _public_base_url() -> str:
    import os
    return os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")


_INTENT_KEY = "payintent:{}"
_INTENT_TTL_SECONDS = 3600


async def start_geo_payment(interaction: discord.Interaction, *, payment_type: str, price_usd: float,
                            product_title: str, product_description: str, amount_display: str,
                            guild_id: Optional[int] = None) -> None:
    """One 'Pay' button, no questions. It opens <PUBLIC_BASE_URL>/pay?t=<token>,
    which looks up the visitor's country from their IP and redirects to
    Paystack (Ghana) or Gumroad (everywhere else) — see api/pay_redirect.py.
    Call after interaction.response.defer(ephemeral=True, thinking=True)."""
    import json
    import time
    clone_id = getattr(interaction.client, "clone_id", None)
    token = secrets.token_urlsafe(12)
    await db.set_global_setting(_INTENT_KEY.format(token), json.dumps({
        "payment_type": payment_type, "user_id": interaction.user.id, "guild_id": guild_id,
        "clone_id": clone_id, "price_usd": price_usd, "amount_display": amount_display,
        "locale": str(getattr(interaction, "locale", "") or ""), "created": time.time(),
    }))
    embed = discord.Embed(
        title=product_title,
        description=(
            f"**Price:** {amount_display}\n\n{product_description}\n\n"
            f"Tap **Pay** — you'll be sent to the right checkout automatically. "
            f"Gumroad purchases unlock by themselves; if you paid with Paystack, come back and tap **Verify**."
        ),
        color=discord.Color.gold(),
    )
    view = _GenericVerifyPaymentView(payment_type, guild_id)
    view.add_item(discord.ui.Button(
        label="💳 Pay", url=f"{_public_base_url()}/pay?t={token}", style=discord.ButtonStyle.link,
    ))
    await interaction.followup.send(embed=embed, view=view, ephemeral=True)


async def create_checkout_for_intent(intent: dict, country: Optional[str]) -> Optional[str]:
    """Called by api/pay_redirect.py. Ghana (country 'GH') -> Paystack in GHS;
    anything else -> Gumroad. Logs the pending payment row and returns the
    checkout URL, or None if the provider couldn't start a checkout."""
    payment_type = intent["payment_type"]
    user_id = int(intent["user_id"])
    guild_id = intent.get("guild_id")
    clone_id = intent.get("clone_id")
    price_usd = float(intent["price_usd"])

    if (country or "").upper() != "GH":
        import gumroad_payments as gp
        reference = gp.new_reference(payment_type, user_id)
        link = gp.build_link(payment_type, user_id, reference)
        if not link:
            return None
        await db.log_payment(
            user_id, gp.expected_price_usd(payment_type) or price_usd, reference, status="pending",
            payment_type=payment_type, chat_id=guild_id, provider="gumroad", clone_id=clone_id,
        )
        return link

    from payments import resolve_gateway
    import utils.currency as fx
    gateway, api_key, provider = await resolve_gateway(clone_id or 0, platform="discord")
    if provider == "stripe":
        amount_minor_units, charge_currency = round(price_usd * 100), "usd"
    else:
        amount_minor_units, charge_currency = fx.usd_to_minor_units(price_usd, "GHS")
    result = await asyncio.to_thread(
        gateway.initialize_payment,
        f"user_{user_id}@animebot.com", amount_minor_units, user_id,
        f"{payment_type}_{user_id}_{guild_id or 0}",
        payment_type=payment_type, extra_metadata={"guild_id": guild_id, "provider": "discord"},
        api_key=api_key, currency=charge_currency,
    )
    if not result or result.get("status") != "success":
        logger.error(f"[geo-pay:{payment_type}] initialize_payment failed for user {user_id}: {result!r}")
        return None
    await db.log_payment(
        user_id, price_usd, result["reference"], status="pending",
        payment_type=payment_type, chat_id=guild_id, provider=provider, clone_id=clone_id,
    )
    return result["authorization_url"]


async def start_dual_mode_payment(interaction: discord.Interaction, *, payment_type: str,
                                   price_usd: float, product_title: str, product_description: str,
                                   amount_display_manual: str, guild_id: Optional[int] = None,
                                   force_mode: Optional[str] = None,
                                   currency_override: Optional[str] = None) -> None:
    """Single entry point for a one-time paid feature that supports BOTH
    the Gumroad (international) and Paystack (Ghana) paths,
    switched live via /paymentmode instead of a hardcoded choice — call
    this instead of calling start_manual_payment directly, so a feature
    never has to hand-roll the automatic-gateway half itself (that used to
    mean copy-pasting ~40 lines per feature — see views_card_pack.py's
    start_card_pack_payment/start_ultra_pack_payment, written before this
    helper existed, which is why those two still have their own copies).

    payment_type MUST already have an UNLOCK_HANDLERS entry above — that
    same function fires on a successful purchase in EITHER mode: the
    manual path's admin-approval dispatch already used it (see
    process_manual_payment_decision below), and _GenericVerifyPaymentView
    below reuses it for the automatic path's Verify button too, so the
    actual unlock logic is never duplicated per mode.

    Call after interaction.response.defer(ephemeral=True, thinking=True).
    guild_id: pass the guild this purchase is FOR when it's a whole-guild
    or per-guild-effect unlock — None for account-level purchases. Same
    convention start_manual_payment already uses."""
    clone_id = getattr(interaction.client, "clone_id", None)
    mode = force_mode or await db.get_payment_mode(clone_id)
    if mode == "split" and _public_base_url():
        await start_geo_payment(
            interaction, payment_type=payment_type, price_usd=price_usd, product_title=product_title,
            product_description=product_description, amount_display=amount_display_manual, guild_id=guild_id,
        )
        return
    if mode == "split":
        def _again(m, cur=None):
            async def _run(i: discord.Interaction):
                await start_dual_mode_payment(
                    i, payment_type=payment_type, price_usd=price_usd, product_title=product_title,
                    product_description=product_description, amount_display_manual=amount_display_manual,
                    guild_id=guild_id, force_mode=m, currency_override=cur,
                )
            return _run
        await offer_region_choice(interaction, on_ghana=_again("auto", "GHS"), on_international=_again("gumroad"))
        return
    if mode == "gumroad":
        await start_manual_payment(interaction, payment_type, amount_display_manual, guild_id=guild_id)
        return

    from payments import resolve_gateway
    import utils.currency as fx
    user = interaction.user
    gateway, api_key, provider = await resolve_gateway(clone_id or 0, platform="discord")
    email = f"user_{user.id}@animebot.com"

    if provider == "stripe":
        amount_minor_units = round(price_usd * 100)
        charge_currency = "usd"
    else:
        stored_currency = await db.get_user_currency(user.id)
        target_currency = currency_override or stored_currency or (fx.currency_from_locale(getattr(interaction, "locale", None)) or "USD")
        amount_minor_units, charge_currency = fx.usd_to_minor_units(price_usd, target_currency)

    payment_result = await asyncio.to_thread(
        gateway.initialize_payment,
        email, amount_minor_units, user.id,
        f"{payment_type}_{user.id}_{guild_id or 0}",
        payment_type=payment_type, extra_metadata={"guild_id": guild_id, "provider": "discord"},
        api_key=api_key, currency=charge_currency,
    )
    if not payment_result or payment_result.get("status") != "success":
        logger.error(
            f"[dual-payment:{payment_type}] gateway.initialize_payment failed for user {user.id} "
            f"guild {guild_id} provider={provider!r} api_key_set={bool(api_key)} result={payment_result!r}"
        )
        await interaction.followup.send("Couldn't start a payment right now — please try again shortly.", ephemeral=True)
        return

    reference = payment_result["reference"]
    payment_link = payment_result["authorization_url"]
    await db.log_payment(
        user.id, price_usd, reference, status="pending",
        payment_type=payment_type, chat_id=guild_id, provider=provider,
    )

    charged_amount_display = (
        f"${price_usd:g} USD" if charge_currency.upper() == "USD"
        else f"{amount_minor_units / fx.MINOR_UNIT_MULTIPLIER.get(charge_currency, 100):.2f} {charge_currency.upper()} (≈ ${price_usd:g} USD)"
    )
    embed = discord.Embed(
        title=product_title,
        description=(
            f"**Amount:** {charged_amount_display}\n\n{product_description}\n\n"
            f"Tap **Pay** below, complete checkout, then come back and tap **Verify**."
        ),
        color=discord.Color.gold(),
    )
    view = _GenericVerifyPaymentView(payment_type, guild_id)
    view.add_item(discord.ui.Button(label="💳 Pay Now", url=payment_link, style=discord.ButtonStyle.link))
    await interaction.followup.send(embed=embed, view=view, ephemeral=True)


class _GenericVerifyPaymentView(discord.ui.View):
    """Ephemeral 'I've Paid — Verify' button for start_dual_mode_payment's
    automatic-gateway half. Generic across every payment_type — unlike
    views_card_pack.py's VerifyCardPackPaymentView (which hardcodes
    db.unlock_welcome_card_pack), this always dispatches through
    UNLOCK_HANDLERS, so adding a new dual-mode feature never means writing
    a new Verify view too. Not persistent (no fixed custom_id), same
    reasoning as VerifyCardPackPaymentView — only ever handed to the
    specific buyer who just triggered this specific purchase."""

    def __init__(self, payment_type: str, guild_id: Optional[int]):
        super().__init__(timeout=600)
        self.payment_type = payment_type
        self.guild_id = guild_id

    @discord.ui.button(label="✅ I've Paid — Verify", style=discord.ButtonStyle.success)
    async def verify(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        user = interaction.user

        pending = await db.get_latest_pending_payment(user.id, self.payment_type, self.guild_id)
        if not pending:
            await interaction.followup.send(
                "I don't see a pending payment for you here — start the purchase again.", ephemeral=True,
            )
            return

        if (pending.get("provider") or "") == "gumroad":
            await interaction.followup.send(
                "Gumroad purchases unlock automatically within a few seconds of paying — "
                "you'll get a DM when it's done. Nothing to verify here.", ephemeral=True,
            )
            return

        reference = pending["paystack_reference"]
        clone_id = getattr(interaction.client, "clone_id", None)
        from payments import resolve_gateway_for_provider
        gateway, api_key = await resolve_gateway_for_provider(clone_id or 0, pending.get("provider") or "paystack", platform="discord")
        result = await asyncio.to_thread(gateway.verify_payment, reference, api_key=api_key)

        if result and result.get("status") == "success":
            await db.mark_payment_paid(reference)
            handler = UNLOCK_HANDLERS.get(self.payment_type)
            if handler:
                await handler(reference, user.id, self.guild_id, clone_id)
            await interaction.followup.send("✅ Payment confirmed and unlocked!", ephemeral=True)
        else:
            await interaction.followup.send(
                "Payment not confirmed yet. If you just paid, wait a few seconds and tap Verify again.", ephemeral=True,
            )
