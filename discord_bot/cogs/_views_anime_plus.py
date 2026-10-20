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


async def _price_text(clone_id: int) -> str:
    from database import db
    price = await db.get_clone_price(clone_id, plus.PRICE_KEY)
    return f"GHS {price:g}"


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
            view = upgrade_view(state, expires, await _price_text(clone), await _pending(interaction.user.id, clone))
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
            from database import db
            from payments import resolve_gateway, gateway_charge_amount, charge_error_message
            price = await db.get_clone_price(clone, plus.PRICE_KEY)
            gateway, api_key, provider = await resolve_gateway(clone, platform="discord")
            charge = gateway_charge_amount(provider, price)
            if charge.get("error"):
                await interaction.followup.send(charge_error_message(charge), ephemeral=True)
                return
            result = await asyncio.to_thread(
                gateway.initialize_payment, f"user_{interaction.user.id}@discord.user", charge["amount_minor_units"], interaction.user.id,
                f"AnimePlus_{interaction.user.id}", payment_type="anime_plus", extra_metadata={"clone_id": clone, "provider": "discord"},
                api_key=api_key, currency=charge["currency"])
            if not result or result.get("status") != "success":
                logger.error("[anime-plus] payment init failed for user %s", interaction.user.id)
                await interaction.followup.send("❌ Couldn't start checkout right now. Try again shortly.", ephemeral=True)
                return
            await plus.start_payment(interaction.user.id, clone, result.get("reference"), provider)    # persisted BEFORE the link is shown
            shown = (f"GHS {price:g}.00" if charge["currency"] == "GHS"
                     else f"{charge['amount_minor_units'] / 100:.2f} {charge['currency'].upper()} (converted from GHS {price:g})")
            view = discord.ui.LayoutView(timeout=None)
            box = discord.ui.Container(accent_colour=ACCENT)
            box.add_item(discord.ui.TextDisplay(f"## 💳 Anime upgrade\nPay **{shown}** for {plus.PLUS_DAYS} days, then tap **I've paid**.\n[Complete payment]({result.get('authorization_url')})"))
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
