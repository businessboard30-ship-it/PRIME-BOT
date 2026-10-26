# path: discord_bot/cogs/_views_anime_nudge.py

"""The anime setup reminder card (Components V2) posted in a server's mod-log channel, and its persistent Set up button."""

from __future__ import annotations

import logging
import re

import discord

from modules import anime_feed_nudge as nudge

logger = logging.getLogger(__name__)

ACCENT = discord.Color.from_rgb(88, 101, 242)


class NudgeSetupButton(discord.ui.DynamicItem[discord.ui.Button], template=r"^anime_nudge:(\d+)$"):
    """Opens the anime setup wizard, privately, for a person with Manage Server."""

    def __init__(self, guild_id: int):
        self.guild_id = int(guild_id)
        super().__init__(discord.ui.Button(label="Set up anime", style=discord.ButtonStyle.primary, emoji="🎌",
                                           custom_id=f"anime_nudge:{self.guild_id}"))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        return cls(int(match.group(1)))

    async def callback(self, interaction: discord.Interaction):
        guild = interaction.guild
        perms = getattr(interaction.user, "guild_permissions", None)
        if guild is None or guild.id != self.guild_id or not (perms and (perms.manage_guild or perms.administrator)):
            await interaction.response.send_message("🎌 Only people with **Manage Server** can set this up.", ephemeral=True)
            return
        try:
            from discord_bot.cogs._views_anime_feed import open_setup_ephemeral
            await open_setup_ephemeral(interaction, guild, getattr(interaction.client, "clone_id", None))
        except Exception:
            logger.exception("[anime-nudge] couldn't open the setup in guild %s", self.guild_id)
            if not interaction.response.is_done():
                await interaction.response.send_message("😿 Something went wrong. Try again in a moment.", ephemeral=True)


def nudge_view(bot_name: str, guild_id: int) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=None)
    box = discord.ui.Container(accent_colour=ACCENT)
    box.add_item(discord.ui.TextDisplay(nudge.nudge_text(bot_name)))
    box.add_item(discord.ui.Separator())
    box.add_item(discord.ui.ActionRow(NudgeSetupButton(guild_id)))
    view.add_item(box)
    return view


NUDGE_ITEMS = (NudgeSetupButton,)
