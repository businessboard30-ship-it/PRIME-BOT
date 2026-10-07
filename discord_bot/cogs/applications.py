# path: discord_bot/cogs/applications.py

"""`/application` — one command, a full wizard. Admins design an application form (title,
emojis, colour, questions, where it's posted, where applications arrive); members press Apply."""

import logging

import discord
from discord import app_commands
from discord.ext import commands

from discord_bot.cogs._dm_support import GuildOnlyCog
from discord_bot.cogs._views_applications import build_wizard_view, handle_modal_submit
from discord_bot.cogs._views_shared import user_can_manage_guild
from modules import applications as apps

logger = logging.getLogger(__name__)


async def open_wizard(guild: discord.Guild, clone_id, creator_id: int, channel=None):
    """Creates a draft form and returns the wizard view. If `channel` is given the wizard is
    posted there (join-DM button); otherwise the caller sends it (slash command, ephemeral)."""
    form = await apps.create_form(guild.id, clone_id, creator_id)
    premium = await apps.is_premium(guild.id, clone_id)
    view = build_wizard_view(form, premium)
    if channel is not None:
        return await channel.send(view=view, allowed_mentions=discord.AllowedMentions.none())
    return view


class ApplicationsCog(GuildOnlyCog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="application", description="Create an application form (for a role, staff, anything) with a guided wizard")
    @app_commands.guild_only()
    async def application_cmd(self, interaction: discord.Interaction):
        if not await user_can_manage_guild(interaction.guild, interaction.user.id):
            await interaction.response.send_message("You need the **Manage Server** permission to create application forms.", ephemeral=True)
            return
        view = await open_wizard(interaction.guild, getattr(self.bot, "clone_id", None), interaction.user.id)
        await interaction.response.send_message(view=view, ephemeral=True)

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        """Persistent modals: handle application-form submits even after a restart."""
        if interaction.type is not discord.InteractionType.modal_submit:
            return
        try:
            await handle_modal_submit(interaction)
        except Exception:
            logger.exception("application modal submit failed")
            try:
                if interaction.response.is_done():
                    await interaction.followup.send("Something went wrong — please try again.", ephemeral=True)
                else:
                    await interaction.response.send_message("Something went wrong — please try again.", ephemeral=True)
            except discord.HTTPException:
                pass


async def setup(bot: commands.Bot):
    await bot.add_cog(ApplicationsCog(bot))
