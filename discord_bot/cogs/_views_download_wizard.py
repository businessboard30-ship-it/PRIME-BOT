# path: discord_bot/cogs/_views_download_wizard.py

"""
/setup downloadhub — admin wizard that either auto-creates a #downloads
channel or lets the admin pick an existing one, then posts a PERSISTENT
"Submit a Download" panel into that channel (two buttons: 🎵 Music /
🎬 Video). Anyone in the server can press one of those buttons — no
permission gate on the submit side, only on the admin setup wizard
itself (same check_wizard_access pattern as every other wizard here).

Why two buttons instead of one modal with a type dropdown: Discord modals
can only contain TextInput components — no select menus — so "paste a
link AND choose a type" can't be a single modal. Same constraint the
leveling wizard hit for role-rewards (see _views_leveling_wizard.py's
docstring), solved the same way here: the type is picked BEFORE the
modal opens (it's baked into which button was pressed / the modal's
custom_id), and the modal itself only asks for the link.

Size limits are intentionally NOT hardcoded (no fixed "8MB" anywhere) —
Discord's upload cap depends on the guild's boost tier and changes over
time, so the actual cap is read live via `interaction.guild.filesize_limit`
at submit time and used both for the check and in the error message.
"""

import io
import logging
import os
import re
from urllib.parse import urlparse

import aiohttp
import discord

from database import db
from discord_bot.cogs._views_shared import check_wizard_access
from discord_bot.cogs.media_storage import _clone_id_of
from discord_bot.cogs.external_tools import DRM_BLOCKED_DOMAINS
from modules.external_apis import download_media

logger = logging.getLogger(__name__)

MEDIA_TYPES = {
    "music": {"label": "🎵 Music", "prefix": "audio/", "filename": "download.mp3"},
    "video": {"label": "🎬 Video", "prefix": "video/", "filename": "download.mp4"},
}

FETCH_TIMEOUT = aiohttp.ClientTimeout(total=30)
# Some hosts don't just serve wrong bytes on a bad link — they serve a
# full HTML error/login page with a 200 status. This is a cheap sanity
# check (not a full parser) to catch "that's not actually a file" before
# wasting an upload attempt.
_HTML_SNIFF = re.compile(rb"^\s*<!doctype html|^\s*<html", re.IGNORECASE)


async def _check_access(interaction: discord.Interaction, invoker_id) -> bool:
    return await check_wizard_access(interaction, invoker_id, "downloadhub", "manage_channels", "Manage Channels")


def _id_pattern(field: str) -> str:
    return rf"^dlwz_{field}:(\d+):(-|\d+):(-|\d+)$"


def _encode(field: str, guild_id: int, clone_id, invoker_id) -> str:
    clone_part = "-" if clone_id is None else str(clone_id)
    inv_part = "-" if invoker_id is None else str(invoker_id)
    return f"dlwz_{field}:{guild_id}:{clone_part}:{inv_part}"


def _decode(match: "re.Match"):
    guild_id = int(match.group(1))
    clone_id = None if match.group(2) == "-" else int(match.group(2))
    invoker_id = None if match.group(3) == "-" else int(match.group(3))
    return guild_id, clone_id, invoker_id


def _simple_panel_id_pattern(field: str) -> str:
    # Like _panel_id_pattern but with no media_type segment — used by the
    # upload/browse buttons, which aren't type-specific at press time.
    return rf"^dlpanel_{field}:(\d+):(-|\d+)$"


def _simple_panel_encode(field: str, guild_id: int, clone_id) -> str:
    clone_part = "-" if clone_id is None else str(clone_id)
    return f"dlpanel_{field}:{guild_id}:{clone_part}"


def _simple_panel_decode(match: "re.Match"):
    guild_id = int(match.group(1))
    clone_id = None if match.group(2) == "-" else int(match.group(2))
    return guild_id, clone_id


def _panel_id_pattern(field: str) -> str:
    # Panel buttons carry no invoker_id (anyone can press them) but DO
    # carry media type, since that's baked in at build time, not typed.
    return rf"^dlpanel_{field}:(\d+):(-|\d+):(music|video)$"


def _panel_encode(field: str, guild_id: int, clone_id, media_type: str) -> str:
    clone_part = "-" if clone_id is None else str(clone_id)
    return f"dlpanel_{field}:{guild_id}:{clone_part}:{media_type}"


def _panel_decode(match: "re.Match"):
    guild_id = int(match.group(1))
    clone_id = None if match.group(2) == "-" else int(match.group(2))
    media_type = match.group(3)
    return guild_id, clone_id, media_type


# --- Play/Queue choice view for media submissions ----------------------

class PlayQueueChoiceView(discord.ui.View):
    """Ephemeral view shown when a user submits media — lets them pick
    between playing immediately or queueing to the end."""

    def __init__(self, guild_id: int, clone_id, url: str, media_title: str):
        super().__init__(timeout=120)
        self.guild_id = guild_id
        self.clone_id = clone_id
        self.url = url
        self.media_title = media_title
        self.result = None  # Will be set to "play_now" or "queue" after callback

    @discord.ui.button(label="▶️ Play Now", style=discord.ButtonStyle.success)
    async def play_now_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        self.result = "play_now"
        await self._execute_choice(interaction)

    @discord.ui.button(label="➕ Queue", style=discord.ButtonStyle.primary)
    async def queue_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        self.result = "queue"
        await self._execute_choice(interaction)

    async def _execute_choice(self, interaction: discord.Interaction):
        """Execute the chosen action and report back."""
        music_cog = interaction.client.get_cog("MusicCog")
        if music_cog is None:
            await interaction.followup.send("🎵 Playback isn't available right now — the music module isn't loaded.", ephemeral=True)
            return

        try:
            # queue_track_for_submission handles checking if user is in VC,
            # joining/creating channels as needed, and all playback logic
            reason = await music_cog.queue_track_for_submission(
                interaction.guild,
                interaction.user,
                self.url,
                self.clone_id,
                interaction.channel,  # Post channel for the Now Playing panel
                play_immediately=(self.result == "play_now"),
            )
        except Exception:
            logger.error(f"[v0] queue_track_for_submission raised unexpectedly for {self.url}", exc_info=True)
            await interaction.followup.send(f"🎵 Failed to queue **{self.media_title}** — an unexpected error occurred (logged).", ephemeral=True)
            return

        if reason:
            await interaction.followup.send(f"🎵 Couldn't queue **{self.media_title}**: {reason}.", ephemeral=True)
            return

        if self.result == "play_now":
            await interaction.followup.send(f"🎵 Playing **{self.media_title}** now — see the Now Playing panel.", ephemeral=True)
        else:
            await interaction.followup.send(f"✅ Queued **{self.media_title}** — see the Now Playing panel.", ephemeral=True)


# --- Admin setup wizard ------------------------------------------------

def render_status_lines(config: dict) -> list:
    channel_id = config.get("channel_id")
    if channel_id:
        return [f"✅ **Downloads channel** — <#{channel_id}>"]
    return ["⬜ **Downloads channel** — not set up yet. Create one or pick an existing channel below."]


def build_wizard_view(guild_id: int, clone_id, invoker_id, config: dict) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=None)
    container = discord.ui.Container(accent_colour=discord.Color.blurple())

    text = discord.ui.TextDisplay("\n".join(["### 📥 Set up the downloads channel", *render_status_lines(config)]))
    container.add_item(text)
    container.add_item(discord.ui.Separator())

    row = discord.ui.ActionRow()
    row.add_item(DownloadCreateChannelButton(guild_id, clone_id, invoker_id))
    container.add_item(row)

    select_row = discord.ui.ActionRow()
    select_row.add_item(DownloadChannelSelect(guild_id, clone_id, invoker_id))
    container.add_item(select_row)

    view.add_item(container)
    return view


async def _rerender(interaction: discord.Interaction, guild_id: int, clone_id, invoker_id):
    if not interaction.response.is_done():
        await interaction.response.defer()
    config = await db.get_download_config(guild_id, clone_id=clone_id)
    view = build_wizard_view(guild_id, clone_id, invoker_id, config)
    await interaction.edit_original_response(view=view)


async def remember_wizard_message(guild_id: int, clone_id, invoker_id, channel_id: int, message_id: int) -> None:
    await db.set_download_config(
        guild_id, clone_id=clone_id,
        wizard_channel_id=channel_id, wizard_message_id=message_id, wizard_invoker_id=invoker_id,
    )


async def refresh_posted_wizard(bot, guild_id: int, clone_id=None) -> None:
    config = await db.get_download_config(guild_id, clone_id=clone_id)
    channel_id = config.get("wizard_channel_id")
    message_id = config.get("wizard_message_id")
    if not channel_id or not message_id:
        return
    channel = bot.get_channel(int(channel_id))
    if channel is None:
        return
    try:
        message = await channel.fetch_message(int(message_id))
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return
    invoker_raw = config.get("wizard_invoker_id")
    invoker_id = int(invoker_raw) if invoker_raw is not None else None
    view = build_wizard_view(guild_id, clone_id, invoker_id, config)
    try:
        await message.edit(view=view)
    except (discord.Forbidden, discord.HTTPException):
        pass


async def _post_submit_panel(interaction: discord.Interaction, guild_id: int, clone_id, channel: discord.abc.GuildChannel):
    """Posts (or re-posts) the persistent Submit-a-Download panel into the
    configured channel, and remembers its location so it can be found again
    (not strictly needed for the panel to keep working — its buttons are
    DynamicItems, re-attached globally on bot startup — but useful if we
    ever need to edit/replace it later, e.g. changing the panel copy)."""
    view = build_submit_panel_view(guild_id, clone_id)
    try:
        sent = await channel.send(view=view)
    except discord.Forbidden:
        await interaction.followup.send(
            f"Channel <#{channel.id}> was set, but I don't have permission to post there — check my channel permissions.",
            ephemeral=True,
        )
        return
    except discord.HTTPException as e:
        logger.warning(f"[v0] Failed to post download submit panel in guild {guild_id}: {e}")
        await interaction.followup.send(
            f"Channel <#{channel.id}> was set, but I couldn't post the submit panel there — try again in a moment.",
            ephemeral=True,
        )
        return
    await db.set_download_config(guild_id, clone_id=clone_id, panel_channel_id=channel.id, panel_message_id=sent.id)


def build_submit_panel_view(guild_id: int, clone_id) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=None)
    container = discord.ui.Container(accent_colour=discord.Color.dark_teal())
    container.add_item(discord.ui.TextDisplay(
        "## 📥 Submit a Download\n"
        "Got a YouTube link (or any link yt-dlp supports), or a direct link to a "
        "song/video? Drop it here and I'll fetch it and save it to this server's "
        "media storage channel.\n"
        "**🔊 You need to be in a voice channel for music/video links to start "
        "playing — join a VC first, then submit.**\n"
        "Have a file on your phone or PC instead? Use **📤 Upload a File** — it "
        "gets saved to storage and the library below so anyone can play it later "
        "with **▶️ Browse & Play** (no VC required just to upload).\n"
        "Pick an option below."
    ))
    container.add_item(discord.ui.Separator())
    row = discord.ui.ActionRow()
    row.add_item(DownloadSubmitButton(guild_id, clone_id, "music"))
    row.add_item(DownloadSubmitButton(guild_id, clone_id, "video"))
    container.add_item(row)
    row2 = discord.ui.ActionRow()
    row2.add_item(DownloadUploadButton(guild_id, clone_id))
    row2.add_item(LibraryBrowseButton(guild_id, clone_id))
    container.add_item(row2)
    view.add_item(container)
    return view


class DownloadCreateChannelButton(discord.ui.DynamicItem[discord.ui.Button], template=_id_pattern("create")):
    def __init__(self, guild_id: int, clone_id, invoker_id):
        self.guild_id = guild_id
        self.clone_id = clone_id
        self.invoker_id = invoker_id
        super().__init__(discord.ui.Button(
            label="✨ Create #downloads channel", style=discord.ButtonStyle.success,
            custom_id=_encode("create", guild_id, clone_id, invoker_id),
        ))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        guild_id, clone_id, invoker_id = _decode(match)
        return cls(guild_id, clone_id, invoker_id)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        if not await _check_access(interaction, self.invoker_id):
            return
        guild = interaction.guild
        try:
            channel = await guild.create_text_channel("downloads", reason="Set up by /setup downloadhub")
        except discord.Forbidden:
            await interaction.followup.send("I don't have permission to create channels here.", ephemeral=True)
            return
        await db.set_download_config(self.guild_id, clone_id=self.clone_id, channel_id=channel.id, channel_auto_created=True)
        await _post_submit_panel(interaction, self.guild_id, self.clone_id, channel)
        await _rerender(interaction, self.guild_id, self.clone_id, self.invoker_id)


class DownloadChannelSelect(discord.ui.DynamicItem[discord.ui.ChannelSelect], template=_id_pattern("pick")):
    def __init__(self, guild_id: int, clone_id, invoker_id):
        self.guild_id = guild_id
        self.clone_id = clone_id
        self.invoker_id = invoker_id
        super().__init__(discord.ui.ChannelSelect(
            placeholder="...or pick an existing channel",
            channel_types=[discord.ChannelType.text],
            min_values=1, max_values=1,
            custom_id=_encode("pick", guild_id, clone_id, invoker_id),
        ))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        guild_id, clone_id, invoker_id = _decode(match)
        return cls(guild_id, clone_id, invoker_id)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        if not await _check_access(interaction, self.invoker_id):
            return
        picked = self.item.values[0]
        # ChannelSelect values come back as AppCommandChannel (a thin
        # partial object with just id/name/type — no .send()), not a real
        # discord.abc.Messageable. Resolve to the actual cached channel
        # (guild.get_channel), falling back to an API fetch if it's
        # somehow not in cache, so _post_submit_panel can call .send().
        channel = interaction.guild.get_channel(picked.id)
        if channel is None:
            try:
                channel = await interaction.guild.fetch_channel(picked.id)
            except discord.HTTPException:
                await interaction.followup.send("Couldn't find that channel — try picking it again.", ephemeral=True)
                return
        await db.set_download_config(self.guild_id, clone_id=self.clone_id, channel_id=channel.id, channel_auto_created=False)
        await _post_submit_panel(interaction, self.guild_id, self.clone_id, channel)
        await _rerender(interaction, self.guild_id, self.clone_id, self.invoker_id)


# --- Persistent submit panel (posted in the downloads channel) --------

class DownloadSubmitButton(discord.ui.DynamicItem[discord.ui.Button], template=_panel_id_pattern("submit")):
    def __init__(self, guild_id: int, clone_id, media_type: str):
        self.guild_id = guild_id
        self.clone_id = clone_id
        self.media_type = media_type
        meta = MEDIA_TYPES[media_type]
        super().__init__(discord.ui.Button(
            label=meta["label"], style=discord.ButtonStyle.primary,
            custom_id=_panel_encode("submit", guild_id, clone_id, media_type),
        ))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        guild_id, clone_id, media_type = _panel_decode(match)
        return cls(guild_id, clone_id, media_type)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(DownloadLinkModal(self.guild_id, self.clone_id, self.media_type))


async def _resolve_storage_target(interaction: discord.Interaction, fallback_channel):
    """Every media post from this wizard (link submission or file upload)
    goes to the owner-configured media storage channel (see
    media_storage.py / /set-storage-channel), auto-creating one if the
    owner hasn't set it up yet.

    This used to fall back to fallback_channel (the downloads/request
    channel itself) when no storage channel was configured — which meant
    the raw file + a permanent "X uploaded by Y" post sat forever in the
    same visible channel people submit from. Per the owner's explicit
    instruction ("make sure delete whatever u send for music and more"),
    that visible channel should never be the permanent home for a file:
    auto-creating a dedicated (hidden-by-default) storage channel the
    first time it's needed means the real archive always lives somewhere
    separate, and nothing durable ever needs to be deleted out of the
    request channel to keep it clean."""
    if interaction.guild is None:
        return fallback_channel
    clone_id = _clone_id_of(interaction.client)
    config = await db.get_media_storage_config(interaction.guild.id, clone_id)
    storage_channel_id = config.get("storage_channel_id")
    if storage_channel_id:
        storage_channel = interaction.guild.get_channel(storage_channel_id)
        if storage_channel is not None:
            return storage_channel
        # Configured channel was deleted — fall through and auto-create
        # a fresh one rather than silently dumping files back into the
        # visible request channel.

    try:
        overwrites = {
            interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, attach_files=True),
        }
        storage_channel = await interaction.guild.create_text_channel(
            "media-storage", overwrites=overwrites, reason="Auto-created media storage archive (no /set-storage-channel run yet)",
        )
        await db.set_media_storage_channel(interaction.guild.id, storage_channel.id, interaction.client.user.id, clone_id=clone_id)
        return storage_channel
    except discord.Forbidden:
        logger.warning(f"[v0] Couldn't auto-create media-storage channel in guild {interaction.guild.id} — missing Manage Channels. Falling back to {fallback_channel.id}.")
        return fallback_channel
    except discord.HTTPException as e:
        logger.warning(f"[v0] Failed to auto-create media-storage channel in guild {interaction.guild.id}: {e}")
        return fallback_channel


class DownloadLinkModal(discord.ui.Modal):
    def __init__(self, guild_id: int, clone_id, media_type: str):
        meta = MEDIA_TYPES[media_type]
        super().__init__(title=f"Submit a {meta['label']} download")
        self.guild_id = guild_id
        self.clone_id = clone_id
        self.media_type = media_type
        self.link = discord.ui.TextInput(
            label="Link (YouTube, etc.) or direct file link",
            placeholder="https://youtube.com/watch?v=... or https://example.com/file.mp3",
            style=discord.TextStyle.short,
            max_length=500,
        )
        self.add_item(self.link)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        # Everything below is wrapped in one catch-all: without this, any
        # unexpected exception (network hiccup, unexpected None, etc.)
        # anywhere in this flow would propagate to discord.py's generic
        # error handler — logged with no context and, critically, no
        # followup ever sent, so the submitter sees nothing at all. This
        # guarantees both a [v0]-tagged log with the full traceback AND
        # a reply to the user, every time, regardless of what breaks.
        try:
            await self._handle_submit(interaction)
        except Exception:
            logger.error(f"[v0] Unhandled error in DownloadLinkModal.on_submit (media_type={self.media_type}, user={interaction.user.id})", exc_info=True)
            try:
                await interaction.followup.send(
                    "❌ Something went wrong processing that link — the error's been logged. Try again, or try a different link.",
                    ephemeral=True,
                )
            except discord.HTTPException:
                # Interaction token may have expired if we got this far
                # into a failure; nothing more we can do at that point.
                pass

    async def _handle_submit(self, interaction: discord.Interaction):
        url = str(self.link.value).strip()
        if not url.lower().startswith(("http://", "https://")):
            await interaction.followup.send("That doesn't look like a valid link — it needs to start with http(s)://.", ephemeral=True)
            return

        config = await db.get_download_config(self.guild_id, clone_id=self.clone_id)
        channel_id = config.get("channel_id")
        channel = interaction.guild.get_channel(int(channel_id)) if channel_id else None
        if channel is None:
            await interaction.followup.send("The downloads channel isn't set up (or was deleted) — ask an admin to run `/setup downloadhub` again.", ephemeral=True)
            return

        limit = interaction.guild.filesize_limit  # boost-tier-aware, read live rather than hardcoded
        meta = MEDIA_TYPES[self.media_type]

        try:
            domain = urlparse(url).netloc.lower()
        except Exception:
            domain = ""
        if domain in DRM_BLOCKED_DOMAINS:
            await interaction.followup.send("❌ That site is DRM-protected — downloading from it isn't possible.", ephemeral=True)
            return

        # Same engine as the existing /download command (yt-dlp) — this
        # is what actually handles YouTube and every other site yt-dlp
        # supports (it resolves the real media stream, not the raw HTML
        # page a plain GET would return). Only falls back to a plain
        # aiohttp fetch below for genuine direct file links (e.g. a CDN
        # link ending in .mp3/.mp4) that yt-dlp has no extractor for.
        ytdlp_media_type = "video" if self.media_type == "video" else "audio"
        result = await download_media(url, ytdlp_media_type)
        if result and "error" not in result and result.get("filepath"):
            filepath = result["filepath"]
            try:
                size_bytes = os.path.getsize(filepath)
                if size_bytes > limit:
                    await interaction.followup.send(
                        f"That download is too big — this server's upload limit is **{limit // (1024*1024)}MB**, "
                        f"and the file is **{size_bytes // (1024*1024)}MB**.",
                        ephemeral=True,
                    )
                    return
                caption = (
                    f"📥 {meta['label']} submitted by {interaction.user.mention} — "
                    f"**{result.get('title', 'Media')}** ({result.get('uploader', 'Unknown')})"
                )
                target = await _resolve_storage_target(interaction, channel)
                try:
                    sent = await target.send(content=caption, file=discord.File(filepath, filename=result.get("filename", meta["filename"])))
                except discord.Forbidden:
                    await interaction.followup.send(f"I fetched the file, but I don't have permission to post in {target.mention}.", ephemeral=True)
                    return
                except discord.HTTPException as e:
                    logger.warning(f"[v0] Upload failed for {url}: {e}")
                    await interaction.followup.send("Discord rejected the upload — the file may still be too large.", ephemeral=True)
                    return
            finally:
                # Local temp copy is never kept around — the storage
                # channel post above (target.send) is the one durable
                # copy, same "auto-delete the working file, keep the
                # storage-channel post" contract as the upload flow.
                if os.path.exists(filepath):
                    os.remove(filepath)

            # Show the save confirmation first
            media_title = result.get('title', 'Media')

            # Log to the library using the RE-UPLOADED message's own
            # attachment URL — same pattern as DownloadUploadButton's
            # _handle_upload. Without this, link submissions were posted
            # to the storage channel fine but never showed up in
            # Browse & Play, since only file uploads were being logged.
            stream_url = sent.attachments[0].url if sent.attachments else None
            if stream_url:
                try:
                    await db.add_library_entry(
                        self.guild_id, self.clone_id, interaction.user.id, self.media_type, media_title, stream_url,
                        channel_id=target.id, message_id=sent.id,
                    )
                except Exception:
                    logger.error(
                        f"[v0] add_library_entry failed for guild={self.guild_id} clone={self.clone_id} "
                        f"title={media_title!r} — file WAS posted to {target.id} but NOT added to the library.",
                        exc_info=True,
                    )

            await interaction.followup.send(f"✅ Saved to {target.mention}.", ephemeral=True)
            
            # Then check if they're in a VC and show the play/queue choice
            await _show_play_queue_choice(interaction, url, self.guild_id, self.clone_id, media_title)
            return

        # yt-dlp had no extractor for this link (or it genuinely failed).
        # "Unsupported URL" is yt-dlp's specific signal that this simply
        # isn't a site it knows how to handle — that's the one case where
        # falling back to a plain direct-file fetch makes sense. Any other
        # error (login required, throttled, format unavailable, etc.) is
        # a REAL failure on a link yt-dlp does recognize — showing the
        # generic "not a direct file" fallback message there would be
        # actively misleading, so surface the actual error instead.
        error_msg = (result or {}).get("error", "") if result else ""
        if error_msg and "unsupported url" not in error_msg.lower():
            await interaction.followup.send(f"❌ Download failed: {error_msg}", ephemeral=True)
            return

        await self._fallback_direct_fetch(interaction, url, channel, limit, meta)

    async def _fallback_direct_fetch(self, interaction: discord.Interaction, url: str, channel, limit: int, meta: dict):

        try:
            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                )
            }
            async with aiohttp.ClientSession(timeout=FETCH_TIMEOUT, headers=headers) as session:
                async with session.get(url) as resp:
                    if resp.status == 429:
                        await interaction.followup.send(
                            "That link's host is rate-limiting downloads right now — wait a bit and try again, "
                            "or use a different host/link.",
                            ephemeral=True,
                        )
                        return
                    if resp.status != 200:
                        await interaction.followup.send(f"That link returned an error (HTTP {resp.status}) — double-check it's a direct file link.", ephemeral=True)
                        return

                    content_type = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                    declared_length = resp.headers.get("Content-Length")
                    if declared_length and int(declared_length) > limit:
                        await interaction.followup.send(
                            f"That file is too big — this server's upload limit is **{limit // (1024*1024)}MB**, "
                            f"and the link reports **{int(declared_length) // (1024*1024)}MB**.",
                            ephemeral=True,
                        )
                        return

                    # Stream and hard-stop the moment we cross the limit,
                    # rather than trusting Content-Length (some hosts omit
                    # or lie about it) and rather than buffering an
                    # unbounded response fully into memory first.
                    chunks = []
                    total = 0
                    async for chunk in resp.content.iter_chunked(65536):
                        total += len(chunk)
                        if total > limit:
                            await interaction.followup.send(
                                f"That file is too big for this server (limit is **{limit // (1024*1024)}MB**).",
                                ephemeral=True,
                            )
                            return
                        chunks.append(chunk)
                    data = b"".join(chunks)
        except (aiohttp.ClientError, TimeoutError) as e:
            logger.warning(f"[v0] Download fetch failed for {url}: {e}")
            await interaction.followup.send("Couldn't fetch that link — it may be dead, slow, or blocking bots.", ephemeral=True)
            return

        if not data:
            await interaction.followup.send("That link returned an empty response.", ephemeral=True)
            return
        if _HTML_SNIFF.match(data[:200]):
            await interaction.followup.send(
                "That link served a web page, not a file — it needs to be a **direct download link** "
                "(some hosts like Google Drive/Mediafire show a preview page instead of the raw file).",
                ephemeral=True,
            )
            return
        # Soft check only: many hosts serve correct files under
        # application/octet-stream, so a mismatch is a warning sign, not
        # an automatic rejection — real bytes get through either way.
        type_looks_off = content_type and not content_type.startswith(meta["prefix"]) and content_type != "application/octet-stream"

        filename = url.rsplit("/", 1)[-1].split("?")[0] or meta["filename"]
        if "." not in filename:
            filename = meta["filename"]

        target = await _resolve_storage_target(interaction, channel)
        try:
            file = discord.File(fp=io.BytesIO(data), filename=filename)
            note = f"\n⚠️ Heads up: this looked like `{content_type or 'unknown'}`, not {self.media_type} — posting anyway." if type_looks_off else ""
            sent = await target.send(content=f"📥 {meta['label']} submitted by {interaction.user.mention}{note}", file=file)
        except discord.Forbidden:
            await interaction.followup.send(f"I fetched the file, but I don't have permission to post in {target.mention}.", ephemeral=True)
            return
        except discord.HTTPException as e:
            logger.warning(f"[v0] Upload failed for {url}: {e}")
            await interaction.followup.send("Discord rejected the upload — the file may still be too large or an unsupported type.", ephemeral=True)
            return

        # Same library-logging step as the yt-dlp path above and as
        # DownloadUploadButton — the raw bytes fetched above are never
        # written to disk in the first place (in-memory BytesIO only), so
        # there's nothing local left to clean up; target.send is the one
        # durable copy, and this is what makes it show up in Browse & Play.
        title = filename.rsplit(".", 1)[0] if "." in filename else filename
        stream_url = sent.attachments[0].url if sent.attachments else None
        if stream_url:
            try:
                await db.add_library_entry(
                    self.guild_id, self.clone_id, interaction.user.id, self.media_type, title, stream_url,
                    channel_id=target.id, message_id=sent.id,
                )
            except Exception:
                logger.error(
                    f"[v0] add_library_entry failed for guild={self.guild_id} clone={self.clone_id} "
                    f"title={title!r} — file WAS posted to {target.id} but NOT added to the library.",
                    exc_info=True,
                )

        # Show the save confirmation first
        await interaction.followup.send(f"✅ Saved to {target.mention}.", ephemeral=True)
        
        # Then check if they're in a VC and show the play/queue choice
        await _show_play_queue_choice(interaction, url, self.guild_id, self.clone_id, filename)



def _guess_media_type(attachment: discord.Attachment) -> str:
    content_type = (attachment.content_type or "").lower()
    if content_type.startswith("audio/"):
        return "music"
    if content_type.startswith("video/"):
        return "video"
    ext = attachment.filename.rsplit(".", 1)[-1].lower() if "." in attachment.filename else ""
    if ext in {"mp3", "wav", "ogg", "flac", "m4a", "aac"}:
        return "music"
    return "video"  # default guess; mp4/mov/webm and anything unrecognized


async def _show_play_queue_choice(
    interaction: discord.Interaction,
    url: str,
    guild_id: int,
    clone_id,
    title: str = "Media",
) -> None:
    """Helper function to show the Play Now | Queue choice dialog.
    Only shows if the user is in a voice channel."""
    
    # Check if user is in a VC at all — only show the choice if they are
    user_voice = interaction.user.voice
    if not user_voice or not user_voice.channel:
        # Not in a VC — skip the playback prompt entirely
        return

    music_cog = interaction.client.get_cog("MusicCog")
    if music_cog is None:
        logger.warning("[v0] _show_play_queue_choice: MusicCog not loaded, skipping voice queue.")
        return

    # Show the Play Now | Queue choice view
    view = PlayQueueChoiceView(guild_id, clone_id, url, title)
    await interaction.followup.send(
        f"🎵 **{title}** is ready — how do you want to play it?",
        view=view,
        ephemeral=True,
    )


class DownloadUploadButton(discord.ui.DynamicItem[discord.ui.Button], template=_simple_panel_id_pattern("upload")):
    """No modal here on purpose — Discord modals can't carry a file input,
    so this button instead asks the user to send the file as their next
    message right in this channel, then the bot picks up that message's
    attachment, forwards it to the media storage channel (falling back to
    the downloads channel if no storage channel is configured — see
    _resolve_storage_target), and logs it to the media library. No VC
    requirement to upload — per the panel copy, uploads just get saved for
    later playback via the library."""

    def __init__(self, guild_id: int, clone_id):
        self.guild_id = guild_id
        self.clone_id = clone_id
        super().__init__(discord.ui.Button(
            label="📤 Upload a File", style=discord.ButtonStyle.secondary,
            custom_id=_simple_panel_encode("upload", guild_id, clone_id),
        ))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        guild_id, clone_id = _simple_panel_decode(match)
        return cls(guild_id, clone_id)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            "📤 Send the music or video file as your **next message in this channel** — you have 2 minutes. "
            "(Discord's own file-size limit for this server still applies.)",
            ephemeral=True,
        )

        def check(m: discord.Message) -> bool:
            return (
                m.author.id == interaction.user.id
                and m.channel.id == interaction.channel.id
                and len(m.attachments) > 0
            )

        try:
            message = await interaction.client.wait_for("message", check=check, timeout=120)
        except TimeoutError:
            await interaction.followup.send("⌛ No file received in time — press **📤 Upload a File** again when you're ready.", ephemeral=True)
            return

        # Everything below is wrapped in one catch-all, same pattern as
        # DownloadLinkModal.on_submit — without this, any unexpected
        # exception (DB hiccup, unexpected None, etc.) would propagate to
        # discord.py's generic DynamicItem error handler, which just logs
        # a traceback to stderr and sends NOTHING back to the user. That's
        # exactly the silent-failure mode that makes "I uploaded a file
        # but Browse says nothing's there" so hard to diagnose — this
        # guarantees both a [v0]-tagged log AND a reply, every time.
        try:
            await self._handle_upload(interaction, message)
        except Exception:
            logger.error(f"[v0] Unhandled error in DownloadUploadButton.callback (user={interaction.user.id})", exc_info=True)
            try:
                await interaction.followup.send(
                    "❌ Something went wrong saving that upload — the error's been logged. Try again, or ask an admin to check the bot's logs.",
                    ephemeral=True,
                )
            except discord.HTTPException:
                pass

    async def _handle_upload(self, interaction: discord.Interaction, message: discord.Message):
        config = await db.get_download_config(self.guild_id, clone_id=self.clone_id)
        channel_id = config.get("channel_id")
        channel = interaction.guild.get_channel(int(channel_id)) if channel_id else None
        if channel is None:
            await interaction.followup.send("The downloads channel isn't set up (or was deleted) — ask an admin to run `/setup downloadhub` again.", ephemeral=True)
            return

        attachment = message.attachments[0]
        media_type = _guess_media_type(attachment)
        meta = MEDIA_TYPES[media_type]
        title = attachment.filename.rsplit(".", 1)[0] if "." in attachment.filename else attachment.filename

        target = await _resolve_storage_target(interaction, channel)
        try:
            file = await attachment.to_file()
            caption = f"{meta['label']} uploaded by {interaction.user.mention} — **{title}**"
            sent = await target.send(content=caption, file=file)
        except discord.Forbidden:
            await interaction.followup.send(f"I don't have permission to post in {target.mention}.", ephemeral=True)
            return
        except discord.HTTPException as e:
            logger.warning(f"[v0] Upload forward failed: {e}")
            await interaction.followup.send("Discord rejected that upload — it may be too large for this server.", ephemeral=True)
            return

        # Log to the library using the RE-UPLOADED message's own attachment
        # URL (not the original message's), since that's the copy that will
        # actually still exist/be reachable long-term in wherever it landed.
        # NOTE: this is the step that was previously unguarded — if the DB
        # write failed here, the whole callback died silently and the file
        # ended up posted in the channel but NEVER logged to the library,
        # which is exactly the "uploaded but Browse says empty" symptom.
        stream_url = sent.attachments[0].url if sent.attachments else attachment.url
        try:
            await db.add_library_entry(
                self.guild_id, self.clone_id, interaction.user.id, media_type, title, stream_url,
                channel_id=target.id, message_id=sent.id,
            )
        except Exception:
            logger.error(
                f"[v0] add_library_entry failed for guild={self.guild_id} clone={self.clone_id} "
                f"title={title!r} — file WAS posted to {target.id} but NOT added to the library.",
                exc_info=True,
            )
            await interaction.followup.send(
                f"⚠️ The file posted to {target.mention}, but I couldn't save it to the library "
                f"(database error, logged) — **Browse & Play** won't show it. Try again in a moment.",
                ephemeral=True,
            )
            return

        try:
            await message.delete()
        except (discord.Forbidden, discord.HTTPException):
            pass  # not critical if we can't clean up their original message

        # Show the save confirmation first
        await interaction.followup.send(
            f"✅ Uploaded to {target.mention} and saved to the library.",
            ephemeral=True,
        )

        # Then check if they're in a VC and show the play/queue choice
        # Use the Discord CDN URL (stream_url) for playback
        await _show_play_queue_choice(
            interaction, stream_url, self.guild_id, self.clone_id, title
        )


class LibraryBrowseButton(discord.ui.DynamicItem[discord.ui.Button], template=_simple_panel_id_pattern("browse")):
    def __init__(self, guild_id: int, clone_id):
        self.guild_id = guild_id
        self.clone_id = clone_id
        super().__init__(discord.ui.Button(
            label="▶️ Browse & Play", style=discord.ButtonStyle.success,
            custom_id=_simple_panel_encode("browse", guild_id, clone_id),
        ))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        guild_id, clone_id = _simple_panel_decode(match)
        return cls(guild_id, clone_id)

    async def callback(self, interaction: discord.Interaction):
        # Check VC membership BEFORE showing the picker at all — without
        # this, a user not in a VC gets handed a dropdown that's a dead
        # end: they pick a track, get "not in a voice channel", then have
        # to press Browse & Play again from scratch even after joining a
        # VC (the old dropdown doesn't get retried automatically). Failing
        # fast here means they only ever see a picker that can actually
        # play something.
        user_voice = interaction.user.voice
        if not user_voice or not user_voice.channel:
            await interaction.response.send_message(
                "🔊 Join a voice channel first, then press **▶️ Browse & Play** again to pick something to play.",
                ephemeral=True,
            )
            return

        entries = await db.list_library_entries(self.guild_id, clone_id=self.clone_id, limit=25)
        if not entries:
            await interaction.response.send_message("The library's empty — upload something first with **📤 Upload a File**.", ephemeral=True)
            return
        view = discord.ui.View(timeout=120)
        view.add_item(LibraryPlaySelect(self.guild_id, self.clone_id, entries))
        await interaction.response.send_message("Pick something to play in your voice channel:", view=view, ephemeral=True)


class LibraryPlaySelect(discord.ui.Select):
    """A plain (non-persistent) Select, since it's attached to a fresh
    ephemeral reply each time the browse button is pressed — no need for
    a DynamicItem custom_id that survives a bot restart here."""

    def __init__(self, guild_id: int, clone_id, entries: list):
        self.guild_id = guild_id
        self.clone_id = clone_id
        options = [
            discord.SelectOption(
                label=(e["title"] or "Untitled")[:100],
                description=f"{MEDIA_TYPES.get(e['media_type'], {}).get('label', e['media_type'])} • uploaded by <@{e['uploader_id']}>"[:100],
                value=str(e["id"]),
            )
            for e in entries
        ]
        super().__init__(placeholder="Choose an upload to play...", options=options, min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await self._handle_pick(interaction)
        except Exception:
            import traceback
            tb = traceback.format_exc()
            logger.error(f"[v0] LibraryPlaySelect.callback raised unexpectedly (guild={self.guild_id}, entry_id={self.values[0]}):\n{tb}")
            print(f"[v0][BROWSE_PLAY_ERROR] guild={self.guild_id} entry_id={self.values[0]}\n{tb}", flush=True)
            try:
                await interaction.followup.send("❌ Something went wrong queueing that (logged).", ephemeral=True)
            except discord.HTTPException:
                pass

    async def _handle_pick(self, interaction: discord.Interaction):
        entry = await db.get_library_entry(int(self.values[0]))
        if entry is None:
            await interaction.followup.send("That upload's no longer available.", ephemeral=True)
            return

        music_cog = interaction.client.get_cog("MusicCog")
        if music_cog is None:
            await interaction.followup.send("Playback isn't available right now — the music module isn't loaded.", ephemeral=True)
            return

        # Discord CDN attachment URLs are signed and expire (~24h) — the
        # stream_url saved at upload time may be stale by now even though
        # the file itself still exists. Re-fetching the original message
        # hands back a freshly re-signed URL for the same attachment, so
        # do that instead of trusting the stored URL for anything but a
        # brand-new upload. Falls back to the stored URL only if the
        # message/channel/attachment can no longer be found at all.
        stream_url = entry["stream_url"]
        if entry.get("channel_id") and entry.get("message_id"):
            src_channel = interaction.guild.get_channel(int(entry["channel_id"]))
            if src_channel is not None:
                try:
                    msg = await src_channel.fetch_message(int(entry["message_id"]))
                    if msg.attachments:
                        stream_url = msg.attachments[0].url
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass  # message/channel gone — fall back to the (possibly stale) stored URL

        config = await db.get_download_config(self.guild_id, clone_id=self.clone_id)
        channel_id = config.get("channel_id")
        post_channel = interaction.guild.get_channel(int(channel_id)) if channel_id else interaction.channel

        reason = await music_cog.queue_direct_url_for_playback(
            interaction.guild, interaction.user, stream_url, entry["title"], self.clone_id, post_channel,
        )

        if reason:
            if "voice channel" in reason.lower() or "voice connection" in reason.lower():
                # Same dropdown, same selected entry — this select isn't a
                # one-shot ephemeral, it stays interactive for the message's
                # 120s lifetime, so re-picking the SAME item after joining a
                # VC works without needing to press Browse & Play again.
                await interaction.followup.send(f"🔊 Not queued: {reason} — join one, then pick it from the dropdown above again.", ephemeral=True)
            else:
                await interaction.followup.send(f"🎵 Not queued: {reason}.", ephemeral=True)
            return
        await interaction.followup.send(f"✅ Queued **{entry['title']}** — see the Now Playing panel.", ephemeral=True)


DYNAMIC_ITEMS = (
    DownloadCreateChannelButton, DownloadChannelSelect, DownloadSubmitButton,
    DownloadUploadButton, LibraryBrowseButton,
)
