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


def _clone_id_of(interaction: discord.Interaction):
    return getattr(interaction.client, "clone_id", None)


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
        "song/video? Drop it here and I'll fetch it and post it right in this "
        "channel for everyone — music and video links also start playing in voice "
        "if you're in a voice channel.\n"
        "Pick the type below to open the link box."
    ))
    container.add_item(discord.ui.Separator())
    row = discord.ui.ActionRow()
    row.add_item(DownloadSubmitButton(guild_id, clone_id, "music"))
    row.add_item(DownloadSubmitButton(guild_id, clone_id, "video"))
    container.add_item(row)
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
                try:
                    await channel.send(content=caption, file=discord.File(filepath, filename=result.get("filename", meta["filename"])))
                except discord.Forbidden:
                    await interaction.followup.send("I fetched the file, but I don't have permission to post in the downloads channel.", ephemeral=True)
                    return
                except discord.HTTPException as e:
                    logger.warning(f"[v0] Upload failed for {url}: {e}")
                    await interaction.followup.send("Discord rejected the upload — the file may still be too large.", ephemeral=True)
                    return
            finally:
                if os.path.exists(filepath):
                    os.remove(filepath)
            note = await self._maybe_queue_to_voice(interaction, url, channel)
            await interaction.followup.send(f"✅ Posted in <#{channel.id}>.{note}", ephemeral=True)
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

        try:
            file = discord.File(fp=io.BytesIO(data), filename=filename)
            note = f"\n⚠️ Heads up: this looked like `{content_type or 'unknown'}`, not {self.media_type} — posting anyway." if type_looks_off else ""
            await channel.send(content=f"📥 {meta['label']} submitted by {interaction.user.mention}{note}", file=file)
        except discord.Forbidden:
            await interaction.followup.send("I fetched the file, but I don't have permission to post in the downloads channel.", ephemeral=True)
            return
        except discord.HTTPException as e:
            logger.warning(f"[v0] Upload failed for {url}: {e}")
            await interaction.followup.send("Discord rejected the upload — the file may still be too large or an unsupported type.", ephemeral=True)
            return

        note = await self._maybe_queue_to_voice(interaction, url, channel)
        await interaction.followup.send(f"✅ Posted in <#{channel.id}>.{note}", ephemeral=True)

    async def _maybe_queue_to_voice(self, interaction: discord.Interaction, url: str, channel) -> str:
        """For both 🎵 Music and 🎬 Video submissions — Discord voice only
        ever transmits audio regardless of source, so a video submission
        queues the same way: yt-dlp resolves the best available audio
        stream from the link and it plays into the voice channel. Never
        raises and never blocks the "file posted" confirmation on a voice
        failure; returns a short note to append to that confirmation
        either way (empty string on success, since the panel message
        itself is the visible confirmation at that point)."""
        music_cog = interaction.client.get_cog("MusicCog")
        if music_cog is None:
            logger.warning("[v0] _maybe_queue_to_voice: MusicCog not loaded, skipping voice queue.")
            return ""
        try:
            reason = await music_cog.queue_track_for_submission(
                interaction.guild, interaction.user, url, self.clone_id, channel
            )
        except Exception:
            logger.error(f"[v0] queue_track_for_submission raised unexpectedly for {url}", exc_info=True)
            return "\n🎵 Not queued to voice: an unexpected error occurred (logged)."
        if reason:
            return f"\n🎵 Not queued to voice: {reason}."
        return "\n🎵 Queued to voice — see the Now Playing panel."


DYNAMIC_ITEMS = (
    DownloadCreateChannelButton, DownloadChannelSelect, DownloadSubmitButton,
)
