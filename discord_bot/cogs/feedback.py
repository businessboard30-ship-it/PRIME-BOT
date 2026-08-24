"""
/feedback — lets any user send free-text feedback to the bot owner(s).

Works in DM or guild (no guild-only restriction — this is exactly the kind
of "only needs interaction.user" command called out in the Phase 1 DM-support
pass). Stored in discord_user_feedback for a durable record, and also DMed
live to every ID in DISCORD_OWNER_BROADCAST_IDS so an owner sees it
immediately without having to query the DB. Uses DISCORD_OWNER_BROADCAST_IDS
rather than DISCORD_CLONE_ADMIN_IDS — the latter is specifically the
clone-payment-bypass list (not auto-populated from anything, per its own
comment in config.py) and was silently notifying nobody on deployments that
never set it separately. DISCORD_OWNER_BROADCAST_IDS is the list already
meant for "who should hear about this as an owner" and falls back to
DISCORD_CLONE_ADMIN_IDS itself if unset, so this is a strict improvement.
If an owner's DMs are closed (discord.Forbidden) we just log and move on to
the next owner rather than failing the command — the user's feedback is
already safely stored either way.
"""

import logging

import discord
from discord import app_commands
from discord.ext import commands

from config import DISCORD_OWNER_BROADCAST_IDS
from database import db

logger = logging.getLogger(__name__)

MAX_FEEDBACK_LENGTH = 1000


class Feedback(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="feedback", description="Send feedback or a suggestion to the bot owner")
    @app_commands.describe(message="What would you like to tell us?")
    async def feedback(self, interaction: discord.Interaction, message: str):
        message = message.strip()
        if not message:
            await interaction.response.send_message("Feedback message can't be empty.", ephemeral=True)
            return
        if len(message) > MAX_FEEDBACK_LENGTH:
            await interaction.response.send_message(
                f"Feedback is limited to {MAX_FEEDBACK_LENGTH} characters "
                f"(yours was {len(message)}). Please shorten it and try again.",
                ephemeral=True,
            )
            return

        guild_id = interaction.guild_id  # None when sent from a DM — column is nullable
        await db.add_discord_user_feedback(interaction.user.id, guild_id, message)

        await interaction.response.send_message(
            "✅ Thanks — your feedback has been sent to the team.", ephemeral=True
        )

        await self._notify_admins(interaction, message)

    @app_commands.command(name="viewfeedback", description="View recent feedback (owner only)")
    @app_commands.describe(limit="How many recent entries to show (max 25)")
    async def viewfeedback(self, interaction: discord.Interaction, limit: app_commands.Range[int, 1, 25] = 10):
        """Owner-only readback of discord_user_feedback — mainly to catch
        up on anything submitted before an admin-DM-list misconfiguration
        (or a closed-DMs owner) meant the live notify never landed, since
        every submission is stored here regardless of whether the DM
        succeeded."""
        if interaction.user.id not in DISCORD_OWNER_BROADCAST_IDS:
            await interaction.response.send_message(
                "This command is restricted to bot owners.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        rows = await db.get_discord_user_feedback(limit=limit)
        if not rows:
            await interaction.followup.send("No feedback recorded yet.", ephemeral=True)
            return

        lines = []
        for row in rows:
            where = f"guild {row['guild_id']}" if row.get("guild_id") else "DM"
            created = row["created_at"].strftime("%Y-%m-%d %H:%M UTC") if row.get("created_at") else "unknown time"
            msg = row["message"]
            if len(msg) > 200:
                msg = msg[:197] + "..."
            lines.append(f"**<@{row['user_id']}>** ({where}, {created})\n{msg}")

        embed = discord.Embed(
            title=f"📋 Recent feedback ({len(rows)})",
            description="\n\n".join(lines)[:4000],
            color=discord.Color.blurple(),
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    async def _notify_admins(self, interaction: discord.Interaction, message: str):
        source = f"#{interaction.channel}" if interaction.guild else "a DM"
        guild_name = interaction.guild.name if interaction.guild else "Direct Message"

        embed = discord.Embed(
            title="📬 New Feedback",
            description=message,
            color=discord.Color.blurple(),
        )
        embed.add_field(name="From", value=f"{interaction.user} ({interaction.user.id})", inline=False)
        embed.add_field(name="Where", value=f"{guild_name} ({source})", inline=False)

        for admin_id in DISCORD_OWNER_BROADCAST_IDS:
            try:
                user = self.bot.get_user(admin_id) or await self.bot.fetch_user(admin_id)
                await user.send(embed=embed)
            except discord.Forbidden:
                logger.info("Could not DM feedback to admin %s — DMs closed", admin_id)
            except discord.HTTPException:
                logger.exception("Failed to DM feedback to admin %s", admin_id)


async def setup(bot: commands.Bot):
    await bot.add_cog(Feedback(bot))
