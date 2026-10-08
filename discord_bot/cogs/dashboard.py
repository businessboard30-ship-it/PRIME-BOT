"""/dashboard: link to the web dashboard for this server.

Replaces the old token-link flow (/automod dashboard on the legacy Next.js site). The new
dashboard signs people in with Discord, so the link is safe to show and share; it only ever
opens servers the viewer can manage. Clone bots are supported: their link carries the clone id.
"""
import discord
from discord import app_commands
from discord.ext import commands

from utils.dash_links import dashboard_url


def _can_manage(interaction: discord.Interaction) -> bool:
    perms = interaction.permissions
    return bool(perms and (perms.administrator or perms.manage_guild))


class DashboardCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="dashboard", description="Open the web dashboard for this server")
    @app_commands.guild_only()
    async def dashboard(self, interaction: discord.Interaction):
        if not _can_manage(interaction):
            await interaction.response.send_message(
                "You need the **Manage Server** permission to use the dashboard.", ephemeral=True)
            return
        clone_id = getattr(interaction.client, "clone_id", None)
        url = dashboard_url(interaction.guild_id, clone_id)
        text = ("🖥️ **Your server dashboard**\nSign in with Discord. You'll only see servers you manage, "
                "and every change is recorded in the audit log.")
        view = discord.ui.View(timeout=None)
        view.add_item(discord.ui.Button(label="Open dashboard", emoji="🖥️", style=discord.ButtonStyle.link, url=url))
        await interaction.response.send_message(text, view=view, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(DashboardCog(bot))
