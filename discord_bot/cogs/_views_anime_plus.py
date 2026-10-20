# path: discord_bot/cogs/_views_anime_plus.py

"""
Anime feed D5: the Upgrade button on every card and its private (ephemeral) card. Persistent DynamicItems with no in-memory state.
A premium SERVER needs no upgrade (everything is already on); otherwise a person can buy a personal upgrade (30 days per payment)
that never makes the server premium.

Payment goes through the SAME system as every other paid feature (payments_manual.start_dual_mode_payment), so it follows the live
`/admin payments mode` (split = Ghana on Paystack / everyone else on Gumroad, auto, or gumroad). Gumroad confirms by itself through
its ping; the unlock handler is payments_manual.UNLOCK_HANDLERS['anime_plus'].
"""

from __future__ import annotations

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


def _price_text() -> str:
    import config
    return f"${config.ANIME_PLUS_FEE_USD:g} USD"


def upgrade_text(state: str, expires: Optional[datetime], price_text: str) -> str:
    feats = "\n".join(f"• {f}" for f in plus.FEATURES)
    if state == "server":
        body = "✅ **This server is premium**, so everything below is already on for you. Nothing to buy."
        if expires:
            body += f"\n-# Your own personal upgrade (active until {_stamp(expires)}) also works in servers that are not premium."
    elif state == "personal":
        body = f"✅ **Your upgrade is active until {_stamp(expires)}.** Pay again to add {plus.PLUS_DAYS} more days."
    else:
        body = f"Unlock more for yourself, in any server: **{price_text}** for {plus.PLUS_DAYS} days."
    return "\n".join(["## ⭐ Anime upgrade", body, "", "**What you get**", feats, "",
                      "-# It's personal: it never makes a server premium. Premium servers unlock all of this for everyone in them."])


def upgrade_view(state: str, expires: Optional[datetime], price_text: str) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=None)
    box = discord.ui.Container(accent_colour=ACCENT)
    box.add_item(discord.ui.TextDisplay(upgrade_text(state, expires, price_text)[:3900]))
    if state != "server":
        box.add_item(discord.ui.ActionRow(_PayButton("Add more days" if state == "personal" else "Upgrade")))
    view.add_item(box)
    return view


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
            state, expires = await plus.status(interaction.user.id, _gid(interaction), _clone_of(interaction))
            await interaction.followup.send(view=upgrade_view(state, expires, _price_text()), ephemeral=True, allowed_mentions=NO_PINGS)
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
            await interaction.response.defer(ephemeral=True, thinking=True)
            state, _ = await plus.status(interaction.user.id, _gid(interaction), _clone_of(interaction))
            if state == "server":                                           # never charge someone who already has it for free
                await interaction.followup.send("✅ This server is premium, so you already have everything. Nothing to buy.", ephemeral=True)
                return
            import config
            from payments_manual import start_dual_mode_payment
            await start_dual_mode_payment(
                interaction, payment_type=plus.PAYMENT_TYPE, price_usd=float(config.ANIME_PLUS_FEE_USD),
                product_title=f"⭐ {config.ANIME_PLUS_PRODUCT_NAME}", product_description=config.ANIME_PLUS_PRODUCT_DESCRIPTION,
                amount_display_manual=_price_text(), guild_id=None)         # account-level: not tied to a server
        except Exception:
            logger.exception("[anime-plus] checkout failed")
            await _fail(interaction)


class _VerifyButton(discord.ui.DynamicItem[discord.ui.Button], template=r"^anime_plus_verify$"):
    """Kept only so 'I've paid' buttons already sitting in old messages still answer (the payment system's own Verify/Check status
    buttons do this job now). It just reports the person's current status."""

    def __init__(self):
        super().__init__(discord.ui.Button(label="I've paid", style=discord.ButtonStyle.primary, emoji="✅", custom_id="anime_plus_verify"))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        return cls()

    async def callback(self, interaction: discord.Interaction):
        try:
            await interaction.response.defer(ephemeral=True)
            state, expires = await plus.status(interaction.user.id, _gid(interaction), _clone_of(interaction))
            text = (f"✅ Your anime upgrade is active until {_stamp(expires)}." if state == "personal"
                    else "✅ This server is premium, so you already have everything." if state == "server"
                    else "I can't see an active upgrade yet. If you just paid, use the **Verify** / **Check status** button on the payment message, or tap **Upgrade** on a card.")
            await interaction.followup.send(text, ephemeral=True, allowed_mentions=NO_PINGS)
        except Exception:
            logger.exception("[anime-plus] status failed")
            await _fail(interaction)


PLUS_ITEMS = (_UpgradeButton, _PayButton, _VerifyButton)
