# path: discord_bot/cogs/_views_leveling_boost.py

"""
"⚡ Boost XP" button — the only surface for the per-user paid XP boost (see
leveling-boost-build-prompt.md §2). No slash command; this button is
attached in two places:

  1. discord_bot/cogs/leveling.py's _send_level_up_card, but ONLY while the
     member is still below level 3 (see that file — pitching a boost before
     someone's shown any real investment in leveling reads as "pay to skip
     a wall you just hit" rather than "speed up your climb").
  2. The same cog's `leaderboard` command, unconditionally, alongside the
     existing leader-link buttons.

Same restart-proof DynamicItem pattern as every other persistent button in
this codebase — see discord_bot/cogs/_views_music_panel.py's
MusicProUpgradeButton, which this mirrors almost exactly.
"""

import re

import discord

import config as app_config
from payments_manual import start_manual_payment


class BoostXPButton(discord.ui.DynamicItem[discord.ui.Button], template=r"^levelboost_xp:(\d+):(-?\d+|-)$"):
    """guild_id is baked into the custom_id (not read from the interaction)
    so this survives being clicked from a DM'd or forwarded message and
    still activates the boost in the RIGHT guild — same reasoning as
    MusicProUpgradeButton's guild_id param."""

    def __init__(self, guild_id: int, clone_id):
        self.guild_id = guild_id
        self.clone_id = clone_id
        clone_part = "-" if clone_id is None else str(clone_id)
        super().__init__(discord.ui.Button(
            label="Boost XP", emoji="⚡",
            style=discord.ButtonStyle.primary,
            custom_id=f"levelboost_xp:{guild_id}:{clone_part}",
        ))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        guild_id = int(match.group(1))
        clone_part = match.group(2)
        clone_id = None if clone_part == "-" else int(clone_part)
        return cls(guild_id, clone_id)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        await start_manual_payment(
            interaction, "xp_boost", f"${app_config.XP_BOOST_FEE_USD:g}",
            guild_id=self.guild_id,
        )


def build_boost_xp_view(guild_id: int, clone_id) -> discord.ui.View:
    """Small helper so both call sites (level-up card, leaderboard) build
    the button the same way instead of duplicating the View(timeout=None)
    + add_item boilerplate."""
    view = discord.ui.View(timeout=None)
    view.add_item(BoostXPButton(guild_id, clone_id))
    return view


DYNAMIC_ITEMS = (BoostXPButton,)
