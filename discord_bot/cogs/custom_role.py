"""Custom Role perk: /customrole launches the styling wizard for a buyer
who already paid (see payments_manual.py's "custom_role" UNLOCK_HANDLERS
entry), or shows a Selar "buy" button for one who hasn't yet — same
one-time, self-serve-forever shape as the welcome/ultra packs, just scoped
per-user instead of per-guild. /customroleadmin lets a server owner turn
the whole feature off. The persistent #custom-roles panel (posted via the
join-DM "Custom Role" quickstart button, see _views_join_dm.py) hits the
exact same launch_custom_role() entry point as this slash command.

See discord_bot/cogs/_views_custom_role.py for the wizard, the panel
button, and config.py's CUSTOM_ROLE_FEE_USD / CUSTOM_ROLE_FONT_STYLES /
CUSTOM_ROLE_COLOR_PALETTE for pricing and styling data.
"""

import logging

import discord
from discord import app_commands
from discord.ext import commands

from database import db
from discord_bot.cogs._views_custom_role import launch_custom_role

logger = logging.getLogger(__name__)


class CustomRoleCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="customrole", description="Create or restyle your personal custom role")
    @app_commands.guild_only()
    async def customrole(self, interaction: discord.Interaction):
        clone_id = getattr(self.bot, "clone_id", None)
        await launch_custom_role(interaction, clone_id)

    @app_commands.command(name="customroleadmin", description="Enable or disable the Custom Role feature for this server")
    @app_commands.describe(disabled="True to turn the feature off, False to turn it back on")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def customroleadmin(self, interaction: discord.Interaction, disabled: bool):
        clone_id = getattr(self.bot, "clone_id", None)
        await db.set_custom_role_feature_disabled(interaction.guild.id, disabled, clone_id=clone_id)
        state = "disabled" if disabled else "enabled"
        await interaction.response.send_message(f"Custom Role feature is now **{state}** for this server.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(CustomRoleCog(bot))
