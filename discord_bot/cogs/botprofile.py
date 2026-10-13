"""
/botprofile -- opens the free Bot profile wizard (rename, avatar, banner, bio).

The wizard itself lives in _views_bot_profile.py (same one as the Edit profile buttons on the join DM and the
server panel). This command only opens it; the wizard checks Manage Server and does all the editing.
"""

import discord
from discord import app_commands
from discord.ext import commands

from discord_bot.cogs._dm_support import GuildOnlyCog


class BotProfileCog(GuildOnlyCog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="botprofile", description="Edit the bot's name, avatar, banner and bio in this server")
    @app_commands.guild_only()
    async def botprofile(self, interaction: discord.Interaction):
        from discord_bot.cogs._views_bot_profile import _check_access, open_profile_wizard
        if not await _check_access(interaction, interaction.guild_id):
            return                                    # it already told the person why
        await interaction.response.defer(ephemeral=True)
        await open_profile_wizard(interaction, interaction.guild_id, getattr(interaction.client, "clone_id", None))


async def setup(bot: commands.Bot):
    await bot.add_cog(BotProfileCog(bot))
