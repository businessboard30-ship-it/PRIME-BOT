"""/mydata: see what PRIME BOT holds about you and delete it (typed confirmation). Works in servers and in DMs.
Loaded last in bot.py so that, if the 100-command cap is ever hit, only this command is skipped; /help has a
"My data" button as a fallback."""
import logging

import discord
from discord import app_commands
from discord.ext import commands

from database import db
from modules import my_data

logger = logging.getLogger(__name__)


def _summary_text(counts: dict) -> str:
    lines = [f"- {k}: {v}" for k, v in counts.items()] or ["- Nothing stored beyond what's kept below."]
    return ("**What we hold about you**\n" + "\n".join(lines)
            + "\n\n**Kept even if you delete:** " + ", ".join(my_data.KEPT) + "."
            + "\n-# Your records for a server are also deleted automatically when you leave it, and a server's data "
              "is deleted 14 days after the bot is removed from it.")


class ConfirmModal(discord.ui.Modal, title="Delete my data"):
    phrase = discord.ui.TextInput(label=f"Type {my_data.PHRASE} to confirm", max_length=20)

    async def on_submit(self, interaction: discord.Interaction):
        if (self.phrase.value or "").strip() != my_data.PHRASE:
            await interaction.response.send_message(f"That didn't match. Type {my_data.PHRASE} exactly.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            res = await my_data.delete_all(interaction.user.id, db)
        except Exception:
            logger.exception("[mydata] delete failed")
            await interaction.followup.send("Something went wrong. Nothing more was changed; try again in a moment.", ephemeral=True)
            return
        if not res["ok"]:
            await interaction.followup.send(res["message"], ephemeral=True)
            return
        gone = ", ".join(f"{k} ({v})" for k, v in res["removed"].items()) or "nothing was stored"
        await interaction.followup.send(f"Deleted: {gone}.\nKept: {', '.join(my_data.KEPT)}.", ephemeral=True)


class MyDataView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)

    @discord.ui.button(label="Delete my data", style=discord.ButtonStyle.danger)
    async def delete(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ConfirmModal())


async def show(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        counts = await my_data.summary(interaction.user.id)
    except Exception:
        logger.exception("[mydata] summary failed")
        counts = {}
    await interaction.followup.send(_summary_text(counts), view=MyDataView(), ephemeral=True)


class MyDataButton(discord.ui.Button):
    def __init__(self, row=None):
        super().__init__(label="My data", style=discord.ButtonStyle.secondary, emoji="🔒", row=row)

    async def callback(self, interaction: discord.Interaction):
        await show(interaction)


class MyDataCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="mydata", description="See what data the bot holds about you, or delete it")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @app_commands.checks.cooldown(1, 30.0, key=lambda i: i.user.id)
    async def mydata(self, interaction: discord.Interaction):
        await show(interaction)

    @mydata.error
    async def _err(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.CommandOnCooldown):
            await interaction.response.send_message(f"Please wait {int(error.retry_after)}s and try again.", ephemeral=True)
        else:
            raise error


async def setup(bot: commands.Bot):
    try:
        await bot.add_cog(MyDataCog(bot))
    except app_commands.CommandLimitReached:
        logger.warning("[mydata] command cap reached; /mydata skipped (use the My data button in /help)")
