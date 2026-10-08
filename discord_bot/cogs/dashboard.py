"""/dashboard: link to the web dashboard for this server.

Replaces the old token-link flow (/automod dashboard on the legacy Next.js site). The new
dashboard signs people in with Discord, so the link is safe to show and share; it only ever
opens servers the viewer can manage. Clone bots aren't supported there yet, so on a clone
this keeps handing out the legacy link.
"""
import discord
from discord import app_commands
from discord.ext import commands

from config import DASHBOARD_BASE_URL
from database import db
from utils.dash_links import dashboard_url, dashboard_supported


def _can_manage(interaction: discord.Interaction) -> bool:
    perms = interaction.permissions
    return bool(perms and (perms.administrator or perms.manage_guild))


async def legacy_dashboard_url(guild_id: int, clone_id) -> str:
    """Old token link, kept only for clone bots."""
    token = await db.get_or_create_dashboard_token(guild_id, clone_id=clone_id)
    return f"{DASHBOARD_BASE_URL}/dashboard/{guild_id}?token={token}&clone_id={clone_id}"


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
        if dashboard_supported(clone_id):
            url = dashboard_url(interaction.guild_id)
            text = ("🖥️ **Your server dashboard**\nSign in with Discord. You'll only see servers you manage, "
                    "and every change is recorded in the audit log.")
        else:
            await interaction.response.defer(ephemeral=True)
            url = await legacy_dashboard_url(interaction.guild_id, clone_id)
            text = ("🔧 Dashboard link (keep this private, it grants config access like a password). "
                    "The new dashboard doesn't support custom bots yet.")
            view = discord.ui.View(timeout=None)
            view.add_item(discord.ui.Button(label="Open dashboard", style=discord.ButtonStyle.link, url=url))
            await interaction.followup.send(text, view=view, ephemeral=True)
            return
        view = discord.ui.View(timeout=None)
        view.add_item(discord.ui.Button(label="Open dashboard", emoji="🖥️", style=discord.ButtonStyle.link, url=url))
        await interaction.response.send_message(text, view=view, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(DashboardCog(bot))
