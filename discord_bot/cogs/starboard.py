"""
Starboard — messages that hit N star-emoji reactions get reposted to a
configured #starboard channel. Uses raw reaction events (not the cached
on_reaction_add) so it still works for messages that aren't in the bot's
message cache, same reasoning classic starboard bots use.

discord_starboard_posts is the source-message -> starboard-message mapping,
so repeat reactions (or someone unstarring) edit the existing post's count
live instead of creating duplicates.
"""

import asyncio
import logging
import re

import discord
from discord import app_commands
from discord.ext import commands
from discord_bot.cogs._dm_support import GuildOnlyCog

from database import db
from discord_bot.cogs._views_community_wizard import (
    build_wizard_view as build_community_wizard_view,
    remember_wizard_message as remember_community_wizard_message,
    refresh_posted_wizard as refresh_community_wizard,
)

logger = logging.getLogger(__name__)


_CUSTOM_EMOJI_RE = re.compile(r"^<a?:\w+:(\d+)>$")


def _emoji_key(value) -> tuple:
    """Comparable key for an emoji. Custom emoji match by ID regardless of
    the animated flag — the admin-typed configured string and the emoji
    that actually arrives on a reaction can disagree on `a:` (e.g. admin
    stored the static form, users react with the animated one), and plain
    string equality silently fails in that case. Unicode emoji still match
    by exact string."""
    s = str(value)
    m = _CUSTOM_EMOJI_RE.match(s)
    if m:
        return ("id", int(m.group(1)))
    return ("unicode", s)


def _require_perm(interaction: discord.Interaction, perm: str) -> bool:
    if interaction.guild is None:
        return False
    return getattr(interaction.permissions, perm, False)


def _clone_id_of(interaction: discord.Interaction):
    return getattr(interaction.client, "clone_id", None)


def _star_color(count: int, threshold: int) -> discord.Color:
    if count >= threshold * 4:
        return discord.Color.red()
    if count >= threshold * 2:
        return discord.Color.orange()
    return discord.Color.gold()


def _build_embed(message: discord.Message, count: int, emoji: str, threshold: int) -> discord.Embed:
    embed = discord.Embed(
        description=message.content or "*(no text content)*",
        color=_star_color(count, threshold),
        timestamp=message.created_at,
    )
    embed.set_author(name=message.author.display_name, icon_url=message.author.display_avatar.url)
    embed.add_field(name="Source", value=f"[Jump to message]({message.jump_url}) in {message.channel.mention}")
    for attachment in message.attachments:
        if attachment.content_type and attachment.content_type.startswith("image/"):
            embed.set_image(url=attachment.url)
            break
    embed.set_footer(text=f"{emoji} {count}")
    return embed


class StarboardCog(GuildOnlyCog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Per-source-message lock so two reactions arriving close together
        # can't both read "no existing post" and both post — the read
        # (get_starboard_post) and the write (channel.send + upsert) are
        # separate awaits, so without this the check-and-act isn't atomic.
        # Not cleaned up proactively; footprint is one small Lock per
        # message that has ever been starred, which is negligible.
        self._starboard_locks: dict[tuple, asyncio.Lock] = {}

    def _clone_id(self):
        return getattr(self.bot, "clone_id", None)

    def _lock_for(self, guild_id: int, clone_id, message_id: int) -> asyncio.Lock:
        key = (guild_id, clone_id, message_id)
        lock = self._starboard_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._starboard_locks[key] = lock
        return lock

    def _emoji_matches(self, payload_emoji: discord.PartialEmoji, configured: str) -> bool:
        return _emoji_key(payload_emoji) == _emoji_key(configured)

    async def _count_stars(self, message: discord.Message, configured_emoji: str) -> int:
        target_key = _emoji_key(configured_emoji)
        for reaction in message.reactions:
            if _emoji_key(reaction.emoji) == target_key:
                # Exclude the message author from the count — otherwise an
                # author reacting to their own message can self-promote it
                # to the starboard with no one else's involvement.
                count = 0
                async for user in reaction.users():
                    if user.id != message.author.id:
                        count += 1
                return count
        return 0

    async def _handle_reaction_change(self, payload: discord.RawReactionActionEvent):
        if payload.guild_id is None:
            return
        clone_id = self._clone_id()
        config = await db.get_starboard_config(payload.guild_id, clone_id=clone_id)
        if not config["channel_id"]:
            return
        if not self._emoji_matches(payload.emoji, config["emoji"]):
            return
        if payload.channel_id == config["channel_id"]:
            # Don't let stars on a starboard repost itself trigger another
            # repost — that's an unbounded loop (repost -> gets starred ->
            # reposts again -> ...).
            return

        channel = self.bot.get_channel(payload.channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(payload.channel_id)
            except discord.HTTPException:
                return
        try:
            message = await channel.fetch_message(payload.message_id)
        except discord.HTTPException:
            return
        if message.author.bot:
            return

        async with self._lock_for(payload.guild_id, clone_id, message.id):
            count = await self._count_stars(message, config["emoji"])
            existing = await db.get_starboard_post(payload.guild_id, message.id, clone_id=clone_id)

            if count < config["threshold"] and existing is None:
                return

            starboard_channel = self.bot.get_channel(config["channel_id"])
            if starboard_channel is None:
                try:
                    starboard_channel = await self.bot.fetch_channel(config["channel_id"])
                except discord.HTTPException:
                    return

            embed = _build_embed(message, count, config["emoji"], config["threshold"])

            if existing:
                try:
                    starboard_msg = await starboard_channel.fetch_message(existing["starboard_message_id"])
                except discord.NotFound:
                    # The repost itself was deleted out from under us. Clear
                    # the stale row instead of swallowing this forever — fall
                    # through so a still-qualifying message gets reposted
                    # below rather than staying permanently wedged.
                    await db.delete_starboard_post(payload.guild_id, message.id, clone_id=clone_id)
                    existing = None
                except discord.HTTPException:
                    return
                else:
                    if count < config["threshold"]:
                        # Stars dropped below threshold — un-post rather than
                        # leaving a repost on the board showing a count under
                        # its own threshold (previously this just edited the
                        # embed down to e.g. "⭐ 1" and left it there forever).
                        try:
                            await starboard_msg.delete()
                        except discord.HTTPException:
                            pass
                        await db.delete_starboard_post(payload.guild_id, message.id, clone_id=clone_id)
                        return
                    try:
                        await starboard_msg.edit(content=f"{config['emoji']} **{count}** | {message.channel.mention}", embed=embed)
                        await db.upsert_starboard_post(
                            payload.guild_id, message.id, message.channel.id,
                            existing["starboard_message_id"], count, clone_id=clone_id,
                        )
                    except discord.HTTPException:
                        pass
                    return

            if existing is None and count >= config["threshold"]:
                try:
                    starboard_msg = await starboard_channel.send(
                        content=f"{config['emoji']} **{count}** | {message.channel.mention}", embed=embed
                    )
                    await db.upsert_starboard_post(
                        payload.guild_id, message.id, message.channel.id,
                        starboard_msg.id, count, clone_id=clone_id,
                    )
                except discord.Forbidden:
                    logger.warning(f"[v0] Missing permissions to post to starboard channel in guild {payload.guild_id}")

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        await self._handle_reaction_change(payload)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        await self._handle_reaction_change(payload)

    @commands.Cog.listener()
    async def on_raw_reaction_clear(self, payload: discord.RawReactionClearEvent):
        # All reactions on the source message were cleared in bulk — the
        # configured emoji's count is now 0, so un-post rather than leaving
        # a stale repost with a frozen count (previously not handled at all).
        await self._unpost_if_starred(payload.guild_id, payload.message_id)

    @commands.Cog.listener()
    async def on_raw_reaction_clear_emoji(self, payload: discord.RawReactionClearEmojiEvent):
        if payload.guild_id is None:
            return
        config = await db.get_starboard_config(payload.guild_id, clone_id=self._clone_id())
        if not config["channel_id"] or not self._emoji_matches(payload.emoji, config["emoji"]):
            return
        await self._unpost_if_starred(payload.guild_id, payload.message_id)

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent):
        # Source message deleted — the repost would otherwise link to a
        # dead jump URL forever.
        await self._unpost_if_starred(payload.guild_id, payload.message_id)

    @commands.Cog.listener()
    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent):
        # Source message edited — refresh the repost's embed so it doesn't
        # keep showing stale text indefinitely.
        if payload.guild_id is None:
            return
        clone_id = self._clone_id()
        config = await db.get_starboard_config(payload.guild_id, clone_id=clone_id)
        if not config["channel_id"]:
            return
        existing = await db.get_starboard_post(payload.guild_id, payload.message_id, clone_id=clone_id)
        if not existing:
            return

        channel = self.bot.get_channel(payload.channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(payload.channel_id)
            except discord.HTTPException:
                return
        try:
            message = await channel.fetch_message(payload.message_id)
        except discord.HTTPException:
            return

        starboard_channel = self.bot.get_channel(config["channel_id"])
        if starboard_channel is None:
            try:
                starboard_channel = await self.bot.fetch_channel(config["channel_id"])
            except discord.HTTPException:
                return

        count = await self._count_stars(message, config["emoji"])
        embed = _build_embed(message, count, config["emoji"], config["threshold"])
        try:
            starboard_msg = await starboard_channel.fetch_message(existing["starboard_message_id"])
            await starboard_msg.edit(content=f"{config['emoji']} **{count}** | {message.channel.mention}", embed=embed)
        except discord.NotFound:
            await db.delete_starboard_post(payload.guild_id, payload.message_id, clone_id=clone_id)
        except discord.HTTPException:
            pass

    async def _unpost_if_starred(self, guild_id, message_id: int) -> None:
        """Shared cleanup for message-delete / reaction-clear: if this
        message has a starboard repost, delete it and clear the mapping row."""
        if guild_id is None:
            return
        clone_id = self._clone_id()
        config = await db.get_starboard_config(guild_id, clone_id=clone_id)
        if not config["channel_id"]:
            return
        existing = await db.get_starboard_post(guild_id, message_id, clone_id=clone_id)
        if not existing:
            return

        starboard_channel = self.bot.get_channel(config["channel_id"])
        if starboard_channel is None:
            try:
                starboard_channel = await self.bot.fetch_channel(config["channel_id"])
            except discord.HTTPException:
                starboard_channel = None
        if starboard_channel is not None:
            try:
                starboard_msg = await starboard_channel.fetch_message(existing["starboard_message_id"])
                await starboard_msg.delete()
            except discord.HTTPException:
                pass
        await db.delete_starboard_post(guild_id, message_id, clone_id=clone_id)

    group = app_commands.guild_only()(app_commands.Group(name="starboard", description="Configure the starboard"))

    @group.command(name="community", description="Set up starboard and suggestions with a guided step-by-step wizard")
    async def community_setup(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        if not _require_perm(interaction, "manage_guild"):
            await interaction.followup.send("You need the **Manage Server** permission to do that.", ephemeral=True)
            return
        clone_id = _clone_id_of(interaction)
        starboard_config = await db.get_starboard_config(interaction.guild_id, clone_id=clone_id)
        suggestion_config = await db.get_suggestion_config(interaction.guild_id, clone_id=clone_id)
        view = build_community_wizard_view(interaction.guild_id, clone_id, interaction.user.id, starboard_config, suggestion_config)
        await interaction.followup.send(view=view)
        sent = await interaction.original_response()
        await remember_community_wizard_message(interaction.guild_id, clone_id, interaction.user.id, sent.channel.id, sent.id)

    @group.command(name="setup", description="Set the starboard channel and star threshold")
    async def setup_cmd(self, interaction: discord.Interaction, channel: discord.TextChannel,
                         threshold: app_commands.Range[int, 1, 100] = 5):
        await interaction.response.defer(ephemeral=True)
        if not _require_perm(interaction, "manage_guild"):
            await interaction.followup.send("You need the **Manage Server** permission to do that.", ephemeral=True)
            return
        config = await db.set_starboard_config(
            interaction.guild_id, clone_id=_clone_id_of(interaction), channel_id=channel.id, threshold=threshold
        )
        await refresh_community_wizard(interaction.client, interaction.guild_id, clone_id=_clone_id_of(interaction))
        await interaction.followup.send(
            f"✅ Starboard set to {channel.mention} with a threshold of **{config['threshold']}** {config['emoji']}.",
            ephemeral=True,
        )

    @group.command(name="emoji", description="Set which emoji counts toward the starboard")
    async def emoji_cmd(self, interaction: discord.Interaction, emoji: str):
        await interaction.response.defer(ephemeral=True)
        if not _require_perm(interaction, "manage_guild"):
            await interaction.followup.send("You need the **Manage Server** permission to do that.", ephemeral=True)
            return
        config = await db.set_starboard_config(interaction.guild_id, clone_id=_clone_id_of(interaction), emoji=emoji)
        await refresh_community_wizard(interaction.client, interaction.guild_id, clone_id=_clone_id_of(interaction))
        await interaction.followup.send(f"✅ Starboard emoji set to {config['emoji']}.", ephemeral=True)

    @group.command(name="settings", description="Show current starboard settings")
    async def settings_cmd(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        config = await db.get_starboard_config(interaction.guild_id, clone_id=_clone_id_of(interaction))
        if not config["channel_id"]:
            await interaction.followup.send("Starboard isn't set up yet — use `/starboard setup`.", ephemeral=True)
            return
        await interaction.followup.send(
            f"**Starboard channel:** <#{config['channel_id']}>\n"
            f"**Threshold:** {config['threshold']} {config['emoji']}",
            ephemeral=True,
        )

    @group.command(name="disable", description="Turn off the starboard for this server")
    async def disable_cmd(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        if not _require_perm(interaction, "manage_guild"):
            await interaction.followup.send("You need the **Manage Server** permission to do that.", ephemeral=True)
            return
        await db.set_starboard_config(interaction.guild_id, clone_id=_clone_id_of(interaction), channel_id=None)
        await refresh_community_wizard(interaction.client, interaction.guild_id, clone_id=_clone_id_of(interaction))
        await interaction.followup.send("✅ Starboard disabled.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(StarboardCog(bot))
