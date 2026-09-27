# path: discord_bot/cogs/style.py

"""/style — single command entry point for the channel-name/font wizard
(_views_style_wizard.py). No subcommands on purpose: everything (convert
text, ask AI for name ideas, apply to a channel) lives as buttons/selects
on the one wizard message, so there's nothing to memorize beyond the one
command name. The same wizard is also reachable from the combined owner
join DM's "styles" feature button (_views_join_dm.py FEATURE_TOGGLES).
"""

import logging

import discord
from discord import app_commands
from discord.ext import commands

from discord_bot.cogs._views_style_wizard import StyleWizardLayoutView

logger = logging.getLogger(__name__)


class StyleCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="style",
        description="Style channel names/text into fancy fonts, or ask AI for name ideas",
    )
    async def style(self, interaction: discord.Interaction):
        guild_id = interaction.guild.id if interaction.guild else None
        clone_id = getattr(self.bot, "clone_id", None)
        view = StyleWizardLayoutView(guild_id, clone_id) if guild_id else None
        if view is None:
            await interaction.response.send_message(
                "Run this in a server \u2014 renaming channels only makes sense there, and text "
                "conversion needs a channel to reply in.", ephemeral=True,
            )
            return
        await interaction.response.send_message(view=view, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(StyleCog(bot))
