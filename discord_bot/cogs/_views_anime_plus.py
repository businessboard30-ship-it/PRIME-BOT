# path: discord_bot/cogs/_views_anime_plus.py

"""
Anime feed D5: the Upgrade button on every card and its private (ephemeral) card. Persistent DynamicItems: all state is in the
database (the person's latest payment row), nothing in memory, so Upgrade / Pay / Verify keep working on any old card after a restart.
A premium SERVER needs no upgrade (everything is already on); otherwise a person can buy a personal monthly upgrade that never makes
the server premium. The payment is completed by the webhook (api/paystack_webhook.py, case 'anime_plus') or by the Verify tap.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime
from typing import Optional

import discord

from modules import anime_plus as plus
from discord_bot.cogs._views_anime_feed import ACCENT, OOPS

logger = logging.getLogger(__name__)

NO_PINGS = discord.AllowedMentions.none()
UPGRADE_HINT = "Tap **Upgrade** on any anime card to unlock it."


def _clone_of(interaction: discord.Interaction) -> int:
    return getattr(interaction.client, "clone_id", None) or 0


def _gid(interaction: discord.Interaction) -> Optional[int]:
    return interaction.guild.id if interaction.guild else None


def _stamp(dt: Optional[datetime]) -> str:
    return f"<t:{int(dt.timestamp())}:D>" if dt else "soon"


def upgrade_text(state: str, expires: Optional[datetime], price_text: str, pending: bool) -> str:
    feats = "\n".join(f"• {f}" for f in plus.FEATURES)
    if state == "server":
        body = "✅ **This server is premium**, so everything below is already on for you. Nothing to buy."
        if expires:
            body += f"\n-# Your own personal upgrade (active until {_stamp(expires)}) also works in servers that are not premium."
    elif state == "personal":
        body = f"✅ **Your upgrade is active until {_stamp(expires)}.** Pay again to add {plus.PLUS_DAYS} more days."
    else:
        body = f"Unlock more for yourself, in any server: **{price_text}** for {plus.PLUS_DAYS} days."
    parts = ["## ⭐ Anime upgrade", body, "", "**What you get**", feats, "",
             "-# It's personal: it never makes a server premium. Premium servers unlock all of this for everyone in them."]
    if pending:
        parts.append("-# You have a checkout waiting. Paid already? Tap **I've paid** to check.")
    return "\n".join(parts)


def upgrade_view(state: str, expires: Optional[datetime], price_text: str, pending: bool) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=None)
    box = discord.ui.Container(accent_colour=ACCENT)
    box.add_item(discord.ui.TextDisplay(upgrade_text(state, expires, price_text, pending)[:3900]))
    buttons = []
    if state != "server":
        buttons.append(_PayButton("Add more days" if state == "personal" else "Upgrade"))
    if pending:
        buttons.append(_VerifyButton())
    if buttons:
        box.add_item(discord.ui.ActionRow(*buttons))
    view.add_item(box)
    return view


def _price_text() -> str:
    import config
    return f"${config.ANIME_PLUS_FEE_USD:g} USD"


_watchers: set = set()                                                   # strong references so a watcher is not garbage collected
WATCH_EVERY = 15
WATCH_FOR = 15 * 60


async def _watch(user: discord.abc.User, reference: str, provider: str, clone: int) -> None:
    """Checks the payment by itself every 15 s for 15 minutes, so nobody has to tap anything. Whoever completes it first (this
    watcher, the webhook, or the person's 'I've paid' tap) wins; the days are added once. If the bot restarts mid-wait, the webhook
    (or the 'I've paid' button) still completes it."""
    try:
        from payments import resolve_gateway_for_provider
        waited = 0
        while waited < WATCH_FOR:
            await asyncio.sleep(WATCH_EVERY)
            waited += WATCH_EVERY
            status = await plus.payment_status(reference)
            if status is None:
                return
            done = status == "completed"
            if not done:
                gateway, api_key = await resolve_gateway_for_provider(clone, provider, platform="discord")
                result = await asyncio.to_thread(gateway.verify_payment, reference, api_key=api_key)
                if (result or {}).get("status") != "success":
                    continue
                done = bool(await plus.complete_payment(reference)) or True
            if done:
                expires = await plus.personal_expiry(user.id, clone)
                try:
                    await user.send(f"✅ Payment confirmed. Your anime upgrade is active until {_stamp(expires)}.")
                except discord.HTTPException:
                    pass                                              # closed DMs: the upgrade is active regardless
                return
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.warning("[anime-plus] payment watcher stopped", exc_info=True)


def _start_watch(user, reference: str, provider: str, clone: int) -> None:
    task = asyncio.create_task(_watch(user, reference, provider, clone))
    _watchers.add(task)
    task.add_done_callback(_watchers.discard)


async def _pending(user_id: int, clone_id: int) -> bool:
    try:
        row = await plus.latest_payment(user_id, clone_id)
    except Exception:
        return False
    return bool(row and row["status"] == "pending")


async def _fail(interaction: discord.Interaction) -> None:
    try:
        if interaction.response.is_done():
            await interaction.followup.send(OOPS, ephemeral=True)
        else:
            await interaction.response.send_message(OOPS, ephemeral=True)
    except discord.HTTPException:
        pass


class _UpgradeButton(discord.ui.DynamicItem[discord.ui.Button], template=r"^anime_plus$"):
    def __init__(self):
        super().__init__(discord.ui.Button(label="Upgrade", style=discord.ButtonStyle.secondary, emoji="⭐", custom_id="anime_plus"))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        return cls()

    async def callback(self, interaction: discord.Interaction):
        try:
            await interaction.response.defer(ephemeral=True)
            clone = _clone_of(interaction)
            state, expires = await plus.status(interaction.user.id, _gid(interaction), clone)
            view = upgrade_view(state, expires, _price_text(), await _pending(interaction.user.id, clone))
            await interaction.followup.send(view=view, ephemeral=True, allowed_mentions=NO_PINGS)
        except Exception:
            logger.exception("[anime-plus] upgrade card failed")
            await _fail(interaction)


class _PayButton(discord.ui.DynamicItem[discord.ui.Button], template=r"^anime_plus_pay$"):
    def __init__(self, label: str = "Upgrade"):
        super().__init__(discord.ui.Button(label=label, style=discord.ButtonStyle.success, emoji="💳", custom_id="anime_plus_pay"))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        return cls()

    async def callback(self, interaction: discord.Interaction):
        try:
            await interaction.response.defer(ephemeral=True)
            clone = _clone_of(interaction)
            state, _ = await plus.status(interaction.user.id, _gid(interaction), clone)
            if state == "server":                                           # never charge someone who already has it for free
                await interaction.followup.send("✅ This server is premium, so you already have everything. Nothing to buy.", ephemeral=True)
                return
            import config
            import utils.currency as fx
            from payments import resolve_gateway
            price_usd = float(config.ANIME_PLUS_FEE_USD)
            gateway, api_key, provider = await resolve_gateway(clone, platform="discord")
            if provider == "stripe":                                       # base price is USD: no conversion for Stripe
                minor, currency = round(price_usd * 100), "usd"
            else:                                                          # Paystack account settles in GHS: convert from the USD price
                minor, currency = fx.usd_to_minor_units(price_usd, "GHS")
            result = await asyncio.to_thread(
                gateway.initialize_payment, f"user_{interaction.user.id}@animebot.com", minor, interaction.user.id,
                f"AnimePlus_{interaction.user.id}", payment_type="anime_plus",
                extra_metadata={"clone_id": clone, "provider": "discord", "product": config.ANIME_PLUS_PRODUCT_NAME,
                                "description": config.ANIME_PLUS_PRODUCT_DESCRIPTION},
                api_key=api_key, currency=currency)
            if not result or result.get("status") != "success":
                logger.error("[anime-plus] payment init failed for user %s provider=%r result=%r", interaction.user.id, provider, result)
                await interaction.followup.send("❌ Couldn't start checkout right now. Try again shortly.", ephemeral=True)
                return
            await plus.start_payment(interaction.user.id, clone, result.get("reference"), provider)    # persisted BEFORE the link is shown
            shown = (f"${price_usd:g} USD" if currency.upper() == "USD"
                     else f"{minor / fx.MINOR_UNIT_MULTIPLIER.get(currency, 100):.2f} {currency.upper()} (about ${price_usd:g} USD)")
            _start_watch(interaction.user, result.get("reference"), provider, clone)
            view = discord.ui.LayoutView(timeout=None)
            box = discord.ui.Container(accent_colour=ACCENT)
            box.add_item(discord.ui.TextDisplay(f"## 💳 {config.ANIME_PLUS_PRODUCT_NAME}\n{config.ANIME_PLUS_PRODUCT_DESCRIPTION}\n\nPay **{shown}**. I check the payment by myself and DM you when it's confirmed (or tap **I've paid**).\n[Complete payment]({result.get('authorization_url')})"))
            box.add_item(discord.ui.ActionRow(_VerifyButton()))
            view.add_item(box)
            await interaction.followup.send(view=view, ephemeral=True, allowed_mentions=NO_PINGS)
        except Exception:
            logger.exception("[anime-plus] checkout failed")
            await _fail(interaction)


class _VerifyButton(discord.ui.DynamicItem[discord.ui.Button], template=r"^anime_plus_verify$"):
    """Checks the person's latest checkout. If the webhook already completed it, this just confirms."""

    def __init__(self):
        super().__init__(discord.ui.Button(label="I've paid", style=discord.ButtonStyle.primary, emoji="✅", custom_id="anime_plus_verify"))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        return cls()

    async def callback(self, interaction: discord.Interaction):
        try:
            await interaction.response.defer(ephemeral=True)
            clone = _clone_of(interaction)
            row = await plus.latest_payment(interaction.user.id, clone)
            if not row:
                await interaction.followup.send("I can't find a checkout for you. Tap **Upgrade** on a card to start one.", ephemeral=True)
                return
            if row["status"] == "pending":
                from payments import resolve_gateway_for_provider
                gateway, api_key = await resolve_gateway_for_provider(clone, row.get("provider"), platform="discord")
                result = await asyncio.to_thread(gateway.verify_payment, row["payment_reference"], api_key=api_key)
                if result.get("status") != "success":
                    await interaction.followup.send("Payment not confirmed yet. Complete payment, then tap **I've paid** again.", ephemeral=True)
                    return
                await plus.complete_payment(row["payment_reference"])       # None means the webhook got there first: same result
            expires = await plus.personal_expiry(interaction.user.id, clone)
            await interaction.followup.send(f"✅ Your anime upgrade is active until {_stamp(expires)}.", ephemeral=True, allowed_mentions=NO_PINGS)
        except Exception:
            logger.exception("[anime-plus] verify failed")
            await _fail(interaction)


PLUS_ITEMS = (_UpgradeButton, _PayButton, _VerifyButton)
