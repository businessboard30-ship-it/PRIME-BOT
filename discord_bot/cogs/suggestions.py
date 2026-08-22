"""
Suggestion box — /suggest posts an embed with 👍/👎 vote reactions; staff
react with ✅/❌ on the same message to mark it approved/denied, which
recolors the embed and (if configured) logs it to an approved-suggestions
channel. Vote counts and staff decision are read via raw reaction events,
same pattern as starboard.py, so it works even on messages not in cache.
"""

import logging

import discord
from discord import app_commands
from discord.ext import commands
from discord_bot.cogs._dm_support import GuildOnlyCog

from database import db
from discord_bot.cogs._views_community_wizard import refresh_posted_wizard as refresh_community_wizard

logger = logging.getLogger(__name__)

UPVOTE = "👍"
DOWNVOTE = "👎"
APPROVE = "✅"
DENY = "❌"

STATUS_COLOR = {
    "pending": discord.Color.blurple(),
    "approved": discord.Color.green(),
    "denied": discord.Color.red(),
}


def _require_perm(interaction: discord.Interaction, perm: str) -> bool:
    if interaction.guild is None:
        return False
    return getattr(interaction.permissions, perm, False)


def _clone_id_of(interaction: discord.Interaction):
    return getattr(interaction.client, "clone_id", None)


def _build_embed(author: discord.abc.User, content: str, status: str, upvotes: int, downvotes: int) -> discord.Embed:
    embed = discord.Embed(description=content, color=STATUS_COLOR.get(status, discord.Color.blurple()))
    embed.set_author(name=f"Suggestion from {author.display_name}", icon_url=author.display_avatar.url)
    embed.add_field(name=UPVOTE, value=str(upvotes), inline=True)
    embed.add_field(name=DOWNVOTE, value=str(downvotes), inline=True)
    embed.set_footer(text=status.capitalize())
    return embed


class SuggestionCog(GuildOnlyCog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def _clone_id(self):
        return getattr(self.bot, "clone_id", None)

    @app_commands.command(name="suggest", description="Submit a suggestion for staff and members to vote on")
    @app_commands.guild_only()
    @app_commands.describe(text="Your suggestion")
    async def suggest(self, interaction: discord.Interaction, text: app_commands.Range[str, 1, 1000]):
        embed = _build_embed(interaction.user, text, "pending", 0, 0)
        await interaction.response.send_message(embed=embed)
        message = await interaction.original_response()
        # Write the row before seeding reactions — otherwise the gateway can
        # deliver the bot's own seed-reaction event before create_suggestion
        # finishes, and _handle_reaction silently drops it since the
        # suggestion isn't in the DB yet.
        await db.create_suggestion(
            interaction.guild_id, interaction.user.id, message.id, interaction.channel_id,
            text, clone_id=_clone_id_of(interaction),
        )
        await message.add_reaction(UPVOTE)
        await message.add_reaction(DOWNVOTE)

    async def _refresh_embed(self, message: discord.Message, suggestion: dict, author: discord.abc.User):
        embed = _build_embed(author, suggestion["content"], suggestion["status"], suggestion["upvotes"], suggestion["downvotes"])
        try:
            await message.edit(embed=embed)
        except discord.HTTPException:
            pass

    async def _resolve_author(self, guild: discord.Guild, author_id: int) -> discord.abc.User:
        member = guild.get_member(author_id)
        if member:
            return member
        try:
            return await self.bot.fetch_user(author_id)
        except discord.HTTPException:
            return None

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        await self._handle_reaction(payload)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        await self._handle_reaction(payload)

    async def _handle_reaction(self, payload: discord.RawReactionActionEvent):
        if payload.guild_id is None:
            return
        suggestion = await db.get_suggestion_by_message(payload.message_id)
        if suggestion is None:
            return

        emoji = str(payload.emoji)
        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return
        channel = guild.get_channel(payload.channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(payload.channel_id)
            except discord.HTTPException:
                return
        try:
            message = await channel.fetch_message(payload.message_id)
        except discord.HTTPException:
            return
        author = await self._resolve_author(guild, suggestion["author_id"])
        if author is None:
            return

        if emoji in (UPVOTE, DOWNVOTE):
            up = sum(r.count for r in message.reactions if str(r.emoji) == UPVOTE) or 0
            down = sum(r.count for r in message.reactions if str(r.emoji) == DOWNVOTE) or 0
            # Discord counts the bot's own seed reaction too — subtract 1 each
            # since the bot always adds one of each when the suggestion posts.
            up = max(0, up - 1)
            down = max(0, down - 1)
            updated = await db.set_suggestion_votes(payload.message_id, up, down)
            if updated:
                await self._refresh_embed(message, updated, author)
            return

        if emoji in (APPROVE, DENY):
            if payload.event_type != "REACTION_ADD":
                return
            member = guild.get_member(payload.user_id)
            if member is None or member.bot:
                return
            if not member.guild_permissions.manage_guild:
                try:
                    await message.remove_reaction(payload.emoji, member)
                except discord.HTTPException:
                    pass
                return
            new_status = "approved" if emoji == APPROVE else "denied"
            updated = await db.set_suggestion_status(payload.message_id, new_status)
            if not updated:
                return
            await self._refresh_embed(message, updated, author)

            if new_status == "approved":
                config = await db.get_suggestion_config(payload.guild_id, clone_id=self._clone_id())
                if config["approved_log_channel_id"]:
                    log_channel = guild.get_channel(config["approved_log_channel_id"])
                    if log_channel:
                        try:
                            await log_channel.send(embed=_build_embed(
                                author, updated["content"], "approved", updated["upvotes"], updated["downvotes"]
                            ))
                        except discord.Forbidden:
                            pass

    group = app_commands.guild_only()(app_commands.Group(name="suggestions", description="Configure the suggestion box"))

    @group.command(name="log-channel", description="Set the channel approved suggestions get logged to")
    async def log_channel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        if not _require_perm(interaction, "manage_guild"):
            await interaction.response.send_message("You need the **Manage Server** permission to do that.", ephemeral=True)
            return
        await db.set_suggestion_config(interaction.guild_id, clone_id=_clone_id_of(interaction), approved_log_channel_id=channel.id)
        await refresh_community_wizard(interaction.client, interaction.guild_id, clone_id=_clone_id_of(interaction))
        await interaction.response.send_message(f"✅ Approved suggestions will be logged to {channel.mention}.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(SuggestionCog(bot))
