# path: discord_bot/cogs/media_storage.py

"""
Media Storage — one owner-designated channel per guild that every piece of
music/media (uploaded as an attachment, or pulled in via /download) gets
archived to.

/set-storage-channel is guild-owner-only (DISCORD_CLONE_ADMIN_IDS bypass,
same convention as media_connect.py's _require_subscription). Once set:
  - Attachments matching MEDIA_EXTENSIONS posted in the downloadhub request
    channel (discord_download_config.channel_id) are re-uploaded into the
    storage channel by the on_message listener below. Attachments posted
    anywhere else (e.g. a casual image in #chatroom) are left alone — this
    channel is a music/media archive, not a guild-wide attachment dragnet.
  - external_tools.py's /download command sends its result straight to the
    storage channel instead of the invoking channel (see that file).

If no storage channel is configured yet, both paths fall back to the old
behavior (post/stay in the original channel) — see _get_channel below —
so nothing silently breaks for guilds that haven't run the setup command.
If no downloadhub channel is configured either, auto-archiving falls back
to guild-wide (old behavior) rather than never triggering.
"""

import logging

import discord
from discord import app_commands
from discord.ext import commands

from database import db
from config import DISCORD_CLONE_ADMIN_IDS
from discord_bot.cogs._dm_support import GuildOnlyCog

logger = logging.getLogger(__name__)

# Extensions this cog treats as "media" for auto-archiving uploads.
# Deliberately excludes generic files (pdf, zip, etc.) — this channel is
# for music/media storage, not a general file dump.
MEDIA_EXTENSIONS = {
    # audio
    ".mp3", ".wav", ".flac", ".m4a", ".ogg", ".opus", ".aac", ".wma",
    # video
    ".mp4", ".mov", ".mkv", ".webm", ".avi",
    # image (covers, art, etc. that ride along with media posts)
    ".png", ".jpg", ".jpeg", ".gif", ".webp",
}

# Discord's safe re-upload ceiling — same conservative floor external_tools.py
# uses for the /download path, kept in sync here so both paths behave the
# same way on a large file.
_SAFE_UPLOAD_BYTES = 8 * 1024 * 1024


def _clone_id_of(client) -> int | None:
    return getattr(client, "clone_id", None)


def _is_media_attachment(att: discord.Attachment) -> bool:
    name = (att.filename or "").lower()
    return any(name.endswith(ext) for ext in MEDIA_EXTENSIONS)


class MediaStorageCog(GuildOnlyCog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="set-storage-channel", description="[Owner] Set the channel media uploads/downloads are archived to")
    @app_commands.describe(channel="The channel to store all uploaded/downloaded media in")
    async def set_storage_channel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        if interaction.guild is None:
            await interaction.response.send_message("This only works in a server.", ephemeral=True)
            return
        if interaction.user.id not in DISCORD_CLONE_ADMIN_IDS and interaction.user.id != interaction.guild.owner_id:
            await interaction.response.send_message(
                "Only the server owner can set the media storage channel.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        clone_id = _clone_id_of(self.bot)
        await db.set_media_storage_channel(interaction.guild.id, channel.id, interaction.user.id, clone_id=clone_id)
        await interaction.followup.send(
            f"✅ Media storage channel set to {channel.mention}. "
            f"Uploaded and downloaded music/media will be archived there from now on.",
            ephemeral=True,
        )

    @commands.Cog.listener("on_message")
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None:
            return
        if not message.attachments:
            return

        media_atts = [a for a in message.attachments if _is_media_attachment(a)]
        if not media_atts:
            return

        clone_id = _clone_id_of(self.bot)
        config = await db.get_media_storage_config(message.guild.id, clone_id)
        storage_channel_id = config.get("storage_channel_id")
        if not storage_channel_id:
            # No storage channel configured yet — leave the upload where it
            # is (old behavior) rather than losing it.
            return
        if message.channel.id == storage_channel_id:
            # Already posted directly in the storage channel — nothing to move.
            return

        # Scope auto-archiving to the designated downloadhub request
        # channel only — otherwise ANY image/media attachment posted
        # anywhere in the guild (a casual photo in #chatroom, a meme, etc.)
        # gets swept into the storage channel, which is surprising and not
        # what "media storage" is meant to do. If no downloadhub channel is
        # configured yet, fall back to the old guild-wide behavior so
        # nothing silently breaks for servers that haven't set one up.
        download_config = await db.get_download_config(message.guild.id, clone_id=clone_id)
        downloadhub_channel_id = download_config.get("channel_id")
        if downloadhub_channel_id and message.channel.id != downloadhub_channel_id:
            return

        # Cross-clone dedupe: if multiple clone bots are members of this
        # SAME guild, every one of them independently receives this exact
        # on_message event (each clone is its own bot process — see
        # clone_manager.py). Without this claim, every clone would
        # re-archive the same attachment, producing one duplicate post per
        # clone in the server. Only the clone that successfully "claims"
        # this message_id proceeds; everyone else skips it here.
        try:
            claimed = await db.claim_media_message_for_archiving(message.id, message.guild.id, clone_id)
        except Exception:
            logger.error("[media_storage] dedupe claim failed for message %s — archiving anyway to avoid losing the upload", message.id, exc_info=True)
            claimed = True  # fail open: better an occasional duplicate than a lost upload
        if not claimed:
            return

        storage_channel = message.guild.get_channel(storage_channel_id)
        if storage_channel is None:
            logger.warning("[media_storage] configured storage channel %s missing in guild %s", storage_channel_id, message.guild.id)
            return

        files = []
        for att in media_atts:
            if att.size and att.size > _SAFE_UPLOAD_BYTES:
                continue
            try:
                files.append(await att.to_file())
            except discord.HTTPException:
                logger.warning("[media_storage] failed to fetch attachment %s", att.filename)

        if not files:
            return

        try:
            await storage_channel.send(
                content=f"📥 Uploaded by {message.author.mention} in {message.channel.mention}",
                files=files,
            )
        except discord.HTTPException as e:
            logger.warning("[media_storage] failed to archive upload: %s", e)


async def setup(bot: commands.Bot):
    await bot.add_cog(MediaStorageCog(bot))
