# path: discord_bot/cogs/_views_premium.py

"""Premium ($5/month per server): the "Go Premium" pitch and its Subscribe
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


def _pitch_text(fee: float, status_line: str) -> str:
    return (
        f"## 💎 Go Premium — ${fee:g}/month per server\n"
        f"{status_line}\n\n"
        "**Everything, unlocked, for the whole server:**\n"
        "🎴 **Welcome Card Pack** — every premium welcome-card theme\n"
        "✨ **Ultra Welcome Pack** — your own custom welcome background\n"
        "🎨 **Custom Role** — every member can style their own role, no per-person fee\n"
        "🎵 **Music Pro** — unlimited listening, uploads and downloads\n"
        "💀 **Hardcore Roast** — unfiltered roast battles (the target has to agree first)\n"
        "🎮 **Roblox game alerts** — post a game's updates to a channel, checked automatically\n"
        "🖼️ **Custom bot branding** — give the bot your own name, avatar and banner in your server\n"
        "🆕 **Every future feature we add** — included automatically, no extra charge\n\n"
        "**Not included:** Discord Clone activation & clone monetization (separate — they cost real hosting) "
        "and the temporary XP boosts.\n\n"
        "**How billing works:**\n"
        "🌍 Outside Ghana — Gumroad membership, renews automatically every month, cancel anytime.\n"
        "🇬🇭 Ghana — Paystack (Mobile Money / local cards, in GHS). 30 days per payment; "
        "I'll DM you a reminder before it ends, and renewing adds time on top of what's left.\n\n"
        "-# Anything you already bought separately stays yours even if Premium ever ends."
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
        member = guild.get_member(interaction.user.id)
        if member is None or not (member.guild_permissions.manage_guild or member == guild.owner):
            await interaction.response.send_message(
                "You need **Manage Server** permission in that server to subscribe.", ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        from payments_manual import start_dual_mode_payment
        fee = config.PREMIUM_FEE_USD
        await start_dual_mode_payment(
            interaction, payment_type="premium", price_usd=fee,
            product_title=f"💎 Premium — {guild.name}",
            product_description=(
                "Unlocks every package and every future feature for this whole server. "
                "Gumroad renews monthly automatically; Paystack covers 30 days per payment."
            ),
            amount_display_manual=f"${fee:g}/month", guild_id=self.guild_id,
        )


async def send_premium_pitch(interaction: discord.Interaction, guild_id: int, clone_id) -> None:
    """Call AFTER interaction.response.defer(ephemeral=True) (the join-DM
    feature button already does)."""
    view = discord.ui.View(timeout=None)
    view.add_item(PremiumSubscribeButton(guild_id, clone_id))
    await interaction.followup.send(await build_pitch(guild_id, clone_id), view=view, ephemeral=True)


DYNAMIC_ITEMS = (PremiumSubscribeButton,)
