"""
/admin panel — the owner control wizard (buttons, selects and forms only).

Mounted under the existing global /admin group, so it costs no new global
slash-command slot. All the work happens in _views_admin_panel.py; this cog
only opens it and hands the views a handle to the cogs whose methods they call.
The original /admin ... slash commands remain available as a fallback.
"""

import logging

import discord
from discord.ext import commands

from discord_bot.cogs._admin_mount import mount_admin_command
from discord_bot.cogs._views_admin_panel import can_open_panel, open_home, refresh_access

logger = logging.getLogger(__name__)


class AdminPanelCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @property
    def clone_admin(self):
        """Looked up lazily so load order between the two cogs doesn't matter."""
        return self.bot.get_cog("CloneAdminCog")

    @property
    def admin_cog(self):
        """AdminCog: owns /admin commissions, subscribers and clones."""
        return self.bot.get_cog("AdminCog")

    @property
    def lookup(self):
        """LookupCog: owns /admin find."""
        return self.bot.get_cog("LookupCog")

    @property
    def bump(self):
        """BumpCog: owns /admin bump (cooldown, list, review, cleanup)."""
        return self.bot.get_cog("BumpCog")

    @property
    def feedback_cog(self):
        """Feedback cog: owns /admin feedback."""
        return self.bot.get_cog("Feedback")

    @property
    def welcome(self):
        """WelcomeCog: owns /admin hostingchannel."""
        return self.bot.get_cog("WelcomeCog")

    async def cog_load(self):
        mount_admin_command(
            self.bot, self.panel, name="panel",
            description="[Owner] Open the owner control panel (buttons, no typed commands)",
        )

    async def panel(self, interaction: discord.Interaction):
        await refresh_access(force=True)   # so a freshly added helper can open the panel
        if not can_open_panel(interaction.user.id):
            await interaction.response.send_message("You're not authorized to use the owner panel.", ephemeral=True)
            return
        await open_home(self, interaction)


async def setup(bot: commands.Bot):
    await bot.add_cog(AdminPanelCog(bot))
