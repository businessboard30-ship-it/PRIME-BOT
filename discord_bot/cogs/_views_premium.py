# path: discord_bot/cogs/_views_premium.py

"""Premium (config.PREMIUM_FEE_USD per month, per server): the "Go Premium" pitch and its Subscribe
button. Used by the join-DM's Go Premium button (_views_join_dm.py) and by
the renewal reminder DM (guild_premium.py).

The Subscribe button is a DynamicItem (guild_id/clone_id live in the
custom_id) so it keeps working across restarts, same as every other button
in this codebase. It hands off to payments_manual.start_dual_mode_payment
with payment_type="premium" — Paystack (Ghana, 30 days per payment) and
Gumroad (international, auto-renewing membership) are picked there exactly
like every other paid feature; the unlock itself is
payments_manual.UNLOCK_HANDLERS["premium"].
"""

import logging
import re

import discord

import config
from database import db

logger = logging.getLogger(__name__)

_SUB_RE = re.compile(r"^premium_sub:(\d+):(-|\d+)$")


_PERKS = (
    "🎉 **Giveaways** — bonus entries, auto-reroll, scheduling & custom colors\n"
    "🎫 **Tickets** — categories, custom buttons, transcripts & auto-close\n"
    "🎵 **Music Pro** — unlimited plays, uploads & downloads\n"
    "🎴 **Welcome Cards** — premium themes + your own background\n"
    "🔤 **Fonts & Designs** — 20+ fonts, frames & emoji tags\n"
    "🎨 **Custom Roles** — every member styles their own\n"
    "💀 **Hardcore Roast** — unfiltered roast battles\n"
    "🤖 **AI Chat** — 3x daily limit (30/day)\n"
    "🖼️ **Bot Branding** — your own bot name, avatar & banner\n"
    "🎮 **Roblox Alerts** — auto game update posts\n"
    "🆕 **Every future feature** — free, automatically"
)
_FOOTER = "-# Not included: clones & temporary XP boosts. Anything you bought separately stays yours."


def _pitch_text(fee: float, status_line: str) -> str:
    return (
        f"## 💎 Go Premium — ${fee:g}/month for your whole server\n"
        f"{status_line}\n\n{_PERKS}\n\n"
        "🌍 Global: Gumroad, cancel anytime · 🇬🇭 Ghana: Paystack, 30 days per payment\n"
        + _FOOTER
    )


async def build_pitch(guild_id: int, clone_id) -> str:
    row = await db.get_guild_premium(guild_id, clone_id=clone_id)
    status = "Not active on this server yet."
    if row and await db.is_guild_premium_active(guild_id, clone_id):
        ts = int(row["expires_at"].timestamp())
        status = f"✅ **Premium is active** on this server until <t:{ts}:D> (<t:{ts}:R>). Renew below to add more time."
    return _pitch_text(config.PREMIUM_FEE_USD, status)


class PremiumSubscribeButton(discord.ui.DynamicItem[discord.ui.Button], template=_SUB_RE.pattern):
    def __init__(self, guild_id: int, clone_id):
        self.guild_id = guild_id
        self.clone_id = clone_id
        super().__init__(discord.ui.Button(
            label=f"Get Premium — ${config.PREMIUM_FEE_USD:g}/month", style=discord.ButtonStyle.primary, emoji="💎",
            custom_id=f"premium_sub:{guild_id}:{'-' if clone_id is None else clone_id}",
        ))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        return cls(int(match.group(1)), None if match.group(2) == "-" else int(match.group(2)))

    async def callback(self, interaction: discord.Interaction):
        guild = interaction.client.get_guild(self.guild_id)
        if guild is None:
            await interaction.response.send_message("I'm not in that server anymore.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await _start_checkout(interaction, guild, self.clone_id)


async def _start_checkout(interaction: discord.Interaction, guild, clone_id) -> None:
    """Straight to the pay buttons — perks and checkout in ONE message.
    Anyone in the server can pay for it."""
    from payments_manual import start_dual_mode_payment
    fee = config.PREMIUM_FEE_USD
    row = await db.get_guild_premium(guild.id, clone_id=clone_id)
    status = ""
    if row and await db.is_guild_premium_active(guild.id, clone_id):
        ts = int(row["expires_at"].timestamp())
        status = f"✅ **Active** until <t:{ts}:D> — paying again adds 30 more days.\n\n"
    await start_dual_mode_payment(
        interaction, payment_type="premium", price_usd=fee,
        product_title=f"💎 Go Premium — {guild.name}",
        product_description=f"{status}{_PERKS}\n\n{_FOOTER}",
        amount_display_manual=f"${fee:g}/month for the whole server", guild_id=guild.id,
    )


async def send_premium_pitch(interaction: discord.Interaction, guild_id: int, clone_id) -> None:
    """Call AFTER interaction.response.defer(ephemeral=True) (the join-DM
    feature button already does). One tap from 'Go Premium' to the pay buttons."""
    guild = interaction.client.get_guild(guild_id)
    if guild:
        await _start_checkout(interaction, guild, clone_id)
        return
    await interaction.followup.send(await build_pitch(guild_id, clone_id), ephemeral=True)


DYNAMIC_ITEMS = (PremiumSubscribeButton,)
