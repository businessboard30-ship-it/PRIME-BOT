# path: discord_bot/cogs/music.py

"""
Voice-channel music — full queue playback controlled entirely by buttons on
a persistent "Now Playing" panel (Components V2, see _views_music_panel.py).

Entry point: there is NO separate /setup music command. Per the owner's
explicit correction, this rides on the EXISTING downloadhub submission flow
instead — when someone submits a link via the 🎵 Music or 🎬 Video button
on the "Submit a Download" panel (_views_download_wizard.py), that same
link is also queued into the voice player, right after the file itself
is posted.
downloadhub is the one entry point for both "get the file" and "play it
here." See queue_track_for_submission() below, called from
DownloadLinkModal.on_submit in _views_download_wizard.py. This also means
no new top-level command AND no new /setup subcommand — the bot's 100-
command cap is untouched.

Audio: reuses modules/external_apis.py's yt-dlp conventions (player-client
workaround for YouTube's 403 issue, cookies file, etc.) but does NOT reuse
download_media() itself — that downloads to disk, which is wrong for a live
player. Instead this streams the resolved direct media URL straight into
discord.FFmpegPCMAudio, with reconnect flags for network blips. ffmpeg is
already installed via nixpacks.toml (confirmed — see nixpacks.toml at repo
root, added earlier for /download's video-merge step). PyNaCl (voice
support) is added in requirements.txt alongside this feature — voice
literally cannot function without it (deploy logs showed "PyNaCl is not
installed, voice will NOT be supported" before this).

Queue is in-memory only, per guild (GuildMusicState, keyed by guild_id) —
same convention as leveling.py's self._last_award cooldown tracker. This
does NOT survive a restart: known v1 limitation, flagged again in the
handoff summary, not silently built around. What DOES survive a restart is
the panel MESSAGE itself (discord_music_panel table + DynamicItem buttons)
— it just shows "nothing queued" until someone queues a new track.

Voice-channel following: a member's track carries their voice channel at
queue time. If they move channels while THEIR track is the one currently
playing, on_voice_state_update below follows them. Movement by anyone else
is ignored — only the current track's owner pulls the bot along. If the
bot ends up alone (everyone else left), a background poller
(voice_solo_watcher, same tasks.loop shape as bump.py's bump_worker)
disconnects after 60s of solo instead of sitting there indefinitely.
"""

import asyncio
import logging
import random
import time

import discord
from discord import app_commands
from discord.ext import commands, tasks
from discord_bot.cogs._dm_support import GuildOnlyCog
from discord_bot.cogs._views_music_panel import (
    build_panel_view,
    remember_panel_message,
    refresh_posted_panel,
)

logger = logging.getLogger(__name__)

SOLO_GRACE_SECONDS = 60
SOLO_CHECK_INTERVAL_SECONDS = 15
LOOP_ORDER = ["off", "track", "queue"]

YDL_BASE_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "socket_timeout": 30,
    "noplaylist": True,
    # Same player-client pooling workaround as modules/external_apis.py's
    # _run_download — YouTube's default "web" client frequently 403s on the
    # actual media URL even though metadata/preflight succeeds.
    "extractor_args": {
        "youtube": {
            "player_client": ["android", "ios", "web"],
        }
    },
}

# Ordered fallback chain, same reasoning as external_apis.py's
# format_candidates: a single narrow format string ("bestaudio/best") can
# raise yt-dlp's "Requested format is not available" for videos that don't
# expose a matching format under the restricted player_client set above.
# Each candidate is progressively looser.
FORMAT_CANDIDATES = ["bestaudio[ext=m4a]/bestaudio/best", "bestaudio*/best*", "best"]

# -reconnect*: survive brief network blips on the input side without
# killing the whole stream.
#
# -nostdin: ffmpeg otherwise tries to read stdin for interactive prompts,
# which can hang the subprocess in some deploy environments — harmless to
# always set.
FFMPEG_BEFORE_OPTIONS = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -nostdin"
# -vn: drop any video stream (some "audio" links are actually video
# containers with an audio track — we only want the audio).
#
# -af aresample=async=1:min_hard_comp=0.100000:first_pts=0: this is the
# actual fix for "playback sounds sped up/slowed down/edited" — a live or
# re-muxed stream's audio timestamps can drift slightly out of sync with
# real time (adaptive bitrate switches, minor network jitter, etc.).
# Without correction ffmpeg just plays samples back-to-back regardless of
# drift, which is exactly what produces that warbly pitch-bent "chipmunk"
# effect as it silently speeds up to catch up or slows down to wait.
# aresample's async mode actively stretches/compresses silence to keep
# the output locked to real wall-clock time instead of drifting, which is
# the standard fix documented for discord.py music bots hitting this
# exact symptom.
FFMPEG_OPTIONS = "-vn -af aresample=async=1:min_hard_comp=0.100000:first_pts=0"


def _clone_id_of(bot: commands.Bot):
    return getattr(bot, "clone_id", None)


async def _connect_voice_clean(guild: discord.Guild, voice_channel) -> "discord.VoiceClient":
    """Connects to voice — the fast/normal path first, exactly like a
    plain voice_channel.connect() always worked. Only falls back to
    force-clearing the voice state if that first attempt times out,
    which is the actual "stale session left over from a restart"
    scenario this exists for.

    IMPORTANT: this must NOT unconditionally clear voice state before
    every connect — an earlier version did that, and sending a
    "disconnect" state update immediately before every fresh join added
    an extra round-trip through Discord's voice gateway that could
    itself race with the reconnect and cause the very timeout this was
    meant to prevent, on totally normal first-time joins. Clean-up is a
    fallback, not the default path."""
    try:
        return await voice_channel.connect()
    except asyncio.TimeoutError:
        logger.warning(f"[v0] Voice connect timed out in guild {guild.id}, clearing stale session and retrying once")
        try:
            await guild.change_voice_state(channel=None)
        except discord.HTTPException:
            pass
        await asyncio.sleep(1)  # give Discord's gateway a beat to register the clear
        return await voice_channel.connect()


def _voice_connect_failure_reason(e: Exception) -> str:
    if isinstance(e, asyncio.TimeoutError):
        return "voice connection timed out (likely a leftover session from a recent restart) — try again in a few seconds"
    return f"couldn't join your voice channel ({e})"


def _extract_stream_info(url: str) -> dict:
    """Blocking — must be run via asyncio.to_thread, same reasoning as
    external_apis.py's _run_download docstring (blocking network call, and
    yt-dlp's own extract_info would otherwise stall the bot's single event
    loop for every guild at once, not just this one)."""
    import yt_dlp
    import os

    ydl_opts = dict(YDL_BASE_OPTS)
    try:
        from config import YTDLP_COOKIES_FILE
        if YTDLP_COOKIES_FILE and os.path.isfile(YTDLP_COOKIES_FILE):
            ydl_opts["cookiefile"] = YTDLP_COOKIES_FILE
    except ImportError:
        pass

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = None
        last_error = None
        for candidate in FORMAT_CANDIDATES:
            ydl.params['format'] = candidate
            try:
                info = ydl.extract_info(url, download=False)
                break
            except yt_dlp.utils.DownloadError as e:
                last_error = e
                if "requested format is not available" in str(e).lower():
                    logger.warning(f"[v0] Format '{candidate}' unavailable for {url}, trying next candidate")
                    continue
                raise  # sign-in/geo-block/etc — not fixable by a looser format, surface immediately
        if info is None:
            raise last_error or RuntimeError("No matching audio format found")

        if "entries" in info:  # search result / playlist landed here despite noplaylist — take the first
            info = info["entries"][0]
        stream_url = info.get("url")
        if not stream_url:
            # Some extractors need a format picked explicitly rather than
            # a top-level "url" — fall back to the first audio-capable format.
            for f in info.get("formats", []):
                if f.get("acodec") != "none" and f.get("url"):
                    stream_url = f["url"]
                    break
        return {
            "stream_url": stream_url,
            "title": info.get("title", "Unknown track"),
            "uploader": info.get("uploader", "Unknown artist"),
            "duration_seconds": info.get("duration", 0) or 0,
            "thumbnail": info.get("thumbnail"),
        }


async def resolve_track(url: str, queued_by_id: int, voice_channel_id: int) -> dict:
    raw = await asyncio.to_thread(_extract_stream_info, url)
    raw["queued_by"] = queued_by_id
    raw["voice_channel_id"] = voice_channel_id
    raw["source_url"] = url
    return raw


class GuildMusicState:
    """All mutable playback state for one guild. Lives only in memory —
    see module docstring's "known limitation" note on restarts."""

    def __init__(self, guild_id: int):
        self.guild_id = guild_id
        self.queue: list[dict] = []
        self.current: dict | None = None
        self.current_started_at: float | None = None  # time.monotonic() at play() call
        self.paused_at: float | None = None
        self.paused_elapsed: float = 0.0
        self.loop_mode: str = "off"  # off | track | queue
        self.solo_since: float | None = None
        self.volume: int = 100  # percent, 0-200 — applied via PCMVolumeTransformer
        # Mirror of the Now Playing panel posted directly into the queuer's
        # voice-channel text chat (in addition to the downloads channel),
        # so the panel is visible without leaving the call — same idea as
        # the reference bot's in-call panel. In-memory only, same
        # restart-limitation note as the rest of this state.
        self.voice_panel_message: "discord.Message | None" = None

    def position_seconds(self) -> float:
        if self.current_started_at is None:
            return 0.0
        if self.paused_at is not None:
            return self.paused_elapsed
        return self.paused_elapsed + (time.monotonic() - self.current_started_at)

    def panel_snapshot(self) -> dict:
        current = None
        if self.current:
            current = {
                **self.current,
                "position_seconds": self.position_seconds(),
            }
        return {
            "current": current,
            "queue": list(self.queue),
            "paused": self.paused_at is not None,
            "loop_mode": self.loop_mode,
            "volume": self.volume,
        }


class MusicCog(GuildOnlyCog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.states: dict[int, GuildMusicState] = {}

    async def cog_load(self):
        self.voice_solo_watcher.start()

    def cog_unload(self):
        self.voice_solo_watcher.cancel()

    def _state(self, guild_id: int) -> GuildMusicState:
        if guild_id not in self.states:
            self.states[guild_id] = GuildMusicState(guild_id)
        return self.states[guild_id]

    def panel_snapshot(self, guild_id: int) -> dict:
        """Read-only accessor used by _views_music_panel.py — the panel
        file never touches self.states directly, only through this."""
        return self._state(guild_id).panel_snapshot()

    # --- entry point: called by the downloadhub submission flow ----------

    async def queue_track_for_submission(
        self, guild: discord.Guild, member: discord.Member, url: str, clone_id, post_channel,
        play_immediately: bool = False,
    ) -> str | None:
        """Called from _views_download_wizard.py right after a 🎵 Music
        submission successfully posts its file — queues the SAME link into
        the voice player too, so downloadhub is the one entry point for
        both "get the file" and "play it here" (no separate /setup music
        command). Returns None on success, or a short human-readable
        reason string to surface to the submitter if queueing didn't
        happen (never raises — a voice failure must never affect whether
        the file itself got posted, that already succeeded by this point).
        
        If play_immediately=True, the track is inserted at the front of the
        queue and will play next (skipping any currently playing track).
        """
        if member.voice is None or member.voice.channel is None:
            return "you're not in a voice channel"
        voice_channel = member.voice.channel

        try:
            track = await resolve_track(url, member.id, voice_channel.id)
        except Exception as e:
            logger.error(f"[v0] Failed to resolve music track for {url}: {e}")
            return "couldn't resolve an audio stream for that link"
        if not track.get("stream_url"):
            return "no playable audio stream found for that link"

        state = self._state(guild.id)
        
        # Insert at beginning for immediate play, or at end for queueing
        if play_immediately:
            state.queue.insert(0, track)
        else:
            state.queue.append(track)

        voice_client = guild.voice_client
        if voice_client is None:
            try:
                voice_client = await _connect_voice_clean(guild, voice_channel)
            except (discord.ClientException, asyncio.TimeoutError) as e:
                logger.error(f"[v0] Voice connect failed in guild {guild.id} (queue_track_for_submission, channel={voice_channel.id}): {e!r}", exc_info=True)
                state.queue.remove(track)
                return _voice_connect_failure_reason(e)
        elif voice_client.channel.id != voice_channel.id and not voice_client.is_playing():
            await voice_client.move_to(voice_channel)

        if not voice_client.is_playing() and not voice_client.is_paused():
            await self._play_next(guild.id)
        elif play_immediately and voice_client.is_playing():
            # Skip to the newly inserted track at the front
            await self.skip(guild.id)

        await self._post_or_refresh_panel(guild.id, clone_id, post_channel)
        await self._post_or_refresh_voice_panel(guild.id, clone_id, voice_channel)
        return None

    async def queue_direct_url_for_playback(
        self, guild: discord.Guild, member: discord.Member, stream_url: str, title: str, clone_id, post_channel,
    ) -> str | None:
        """Like queue_track_for_submission, but for a URL that's already a
        playable media file — a raw Discord CDN attachment link (phone/PC
        upload) or a library replay — so it skips resolve_track/yt-dlp
        entirely and queues the URL as-is. Same never-raises contract:
        returns None on success, or a short reason string on failure."""
        if member.voice is None or member.voice.channel is None:
            return "you're not in a voice channel"
        voice_channel = member.voice.channel

        track = {
            "stream_url": stream_url,
            "title": title,
            "uploader": member.display_name,
            "duration_seconds": 0,
            "thumbnail": None,
            "queued_by": member.id,
            "voice_channel_id": voice_channel.id,
            "source_url": stream_url,
        }
        state = self._state(guild.id)
        state.queue.append(track)

        voice_client = guild.voice_client
        if voice_client is None:
            try:
                voice_client = await _connect_voice_clean(guild, voice_channel)
            except (discord.ClientException, asyncio.TimeoutError) as e:
                logger.error(f"[v0] Voice connect failed in guild {guild.id} (queue_direct_url_for_playback, channel={voice_channel.id}): {e!r}", exc_info=True)
                state.queue.remove(track)
                return _voice_connect_failure_reason(e)
        elif voice_client.channel.id != voice_channel.id and not voice_client.is_playing():
            await voice_client.move_to(voice_channel)

        if not voice_client.is_playing() and not voice_client.is_paused():
            await self._play_next(guild.id)

        await self._post_or_refresh_panel(guild.id, clone_id, post_channel)
        await self._post_or_refresh_voice_panel(guild.id, clone_id, voice_channel)
        return None

    async def _post_or_refresh_panel(self, guild_id: int, clone_id, fallback_channel):
        """The panel is ONE persistent message per guild — if it already
        exists (tracked in discord_music_panel) this edits it in place,
        same as a track naturally advancing; only posts a fresh message
        the first time a guild ever queues something."""
        from database import db
        state = self._state(guild_id)
        view = build_panel_view(guild_id, clone_id, state.panel_snapshot())
        panel = await db.get_music_panel(guild_id, clone_id=clone_id)
        channel_id = panel.get("panel_channel_id")
        message_id = panel.get("panel_message_id")
        if channel_id and message_id:
            channel = self.bot.get_channel(int(channel_id))
            if channel is not None:
                try:
                    message = await channel.fetch_message(int(message_id))
                    await message.edit(view=view)
                    return
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass  # panel message gone — fall through and post a fresh one
        sent = await fallback_channel.send(view=view)
        await remember_panel_message(guild_id, clone_id, sent.channel.id, sent.id)

    async def _post_or_refresh_voice_panel(self, guild_id: int, clone_id, voice_channel):
        """Mirrors the Now Playing panel directly into the voice channel's
        own text chat, so it's visible in-call the same way the reference
        panel is — separate from the downloads-channel copy handled by
        _post_or_refresh_panel above. Best-effort only: this is NOT
        persisted to the DB (voice_panel_message lives on GuildMusicState,
        in-memory), so it won't survive a restart, same known limitation
        as the rest of this module."""
        if voice_channel is None:
            return
        state = self._state(guild_id)
        view = build_panel_view(guild_id, clone_id, state.panel_snapshot())
        if state.voice_panel_message is not None:
            try:
                await state.voice_panel_message.edit(view=view)
                return
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                state.voice_panel_message = None  # gone — fall through and repost
        try:
            state.voice_panel_message = await voice_channel.send(view=view)
        except (discord.Forbidden, discord.HTTPException, AttributeError):
            # AttributeError guards voice channel types without a linked
            # text chat / send() in older discord.py — never fatal, the
            # downloads-channel panel already succeeded by this point.
            state.voice_panel_message = None

    async def _refresh_voice_panel_if_any(self, guild_id: int, clone_id):
        """Called alongside refresh_posted_panel() wherever the panel needs
        to re-render outside a fresh queue (track advance, button press,
        solo-disconnect) — no-ops if this guild never got a voice-mirrored
        panel."""
        state = self._state(guild_id)
        if state.voice_panel_message is None:
            return
        view = build_panel_view(guild_id, clone_id, state.panel_snapshot())
        try:
            await state.voice_panel_message.edit(view=view)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            state.voice_panel_message = None

    # --- queue engine ------------------------------------------------------

    async def _play_next(self, guild_id: int):
        state = self._state(guild_id)
        guild = self.bot.get_guild(guild_id)
        if guild is None or guild.voice_client is None:
            return

        if state.loop_mode == "track" and state.current is not None:
            next_track = state.current
        elif state.queue:
            next_track = state.queue.pop(0)
            if state.loop_mode == "queue" and state.current is not None:
                state.queue.append(state.current)
        else:
            state.current = None
            state.current_started_at = None
            return

        state.current = next_track
        state.current_started_at = time.monotonic()
        state.paused_at = None
        state.paused_elapsed = 0.0

        source = discord.FFmpegPCMAudio(
            next_track["stream_url"],
            before_options=FFMPEG_BEFORE_OPTIONS,
            options=FFMPEG_OPTIONS,
        )
        # Wrapped so the Vol -10/+10 buttons can adjust playback live via
        # source.volume, instead of needing to restart the stream.
        source = discord.PCMVolumeTransformer(source, volume=state.volume / 100)

        def _after_play(error: Exception | None):
            if error:
                logger.error(f"[v0] Playback error in guild {guild_id}: {error}")
            # after= runs in a background thread, not the event loop — per
            # discord.py's docs, async work here must be scheduled back onto
            # the loop rather than awaited directly.
            fut = asyncio.run_coroutine_threadsafe(self._on_track_finished(guild_id), self.bot.loop)
            try:
                fut.result()
            except Exception as e:
                logger.error(f"[v0] Error scheduling next track in guild {guild_id}: {e}")

        guild.voice_client.play(source, after=_after_play)

    async def _on_track_finished(self, guild_id: int):
        await self._play_next(guild_id)
        clone_id = _clone_id_of(self.bot)
        await refresh_posted_panel(self.bot, guild_id, clone_id=clone_id)
        await self._refresh_voice_panel_if_any(guild_id, clone_id)

    # --- button-driven actions (called from _views_music_panel.py) ---------

    async def toggle_pause(self, guild_id: int):
        state = self._state(guild_id)
        guild = self.bot.get_guild(guild_id)
        if guild is None or guild.voice_client is None:
            return
        vc = guild.voice_client
        if vc.is_playing():
            vc.pause()
            state.paused_at = time.monotonic()
            state.paused_elapsed = state.position_seconds()
        elif vc.is_paused():
            vc.resume()
            state.current_started_at = time.monotonic()
            state.paused_at = None

    async def skip(self, guild_id: int):
        guild = self.bot.get_guild(guild_id)
        if guild is None or guild.voice_client is None:
            return
        state = self._state(guild_id)
        # Force-skip past loop-track mode too — an explicit skip press
        # always means "move on", not "replay this one again".
        if state.loop_mode == "track":
            state.loop_mode = "off"
        if guild.voice_client.is_playing() or guild.voice_client.is_paused():
            guild.voice_client.stop()  # triggers _after_play -> _on_track_finished

    async def stop(self, guild_id: int):
        state = self._state(guild_id)
        state.queue.clear()
        state.current = None
        state.current_started_at = None
        state.loop_mode = "off"
        guild = self.bot.get_guild(guild_id)
        if guild is not None and guild.voice_client is not None:
            guild.voice_client.stop()
            await guild.voice_client.disconnect(force=False)

    async def cycle_loop_mode(self, guild_id: int):
        state = self._state(guild_id)
        idx = LOOP_ORDER.index(state.loop_mode) if state.loop_mode in LOOP_ORDER else 0
        state.loop_mode = LOOP_ORDER[(idx + 1) % len(LOOP_ORDER)]

    async def replay(self, guild_id: int):
        """Restarts the CURRENT track from the top — distinct from Skip
        (moves on) and from loop-track (auto-repeats forever). Re-inserts
        the current track at the front of the queue (unless loop-track is
        already doing that job) then stops playback so the existing
        after-hook naturally picks it back up as the "next" track."""
        state = self._state(guild_id)
        if state.current is None:
            return
        guild = self.bot.get_guild(guild_id)
        if guild is None or guild.voice_client is None:
            return
        if state.loop_mode != "track":
            state.queue.insert(0, state.current)
        voice_client = guild.voice_client
        if voice_client.is_playing() or voice_client.is_paused():
            voice_client.stop()  # triggers _after_play -> _on_track_finished -> _play_next
        else:
            await self._play_next(guild_id)

    async def shuffle(self, guild_id: int):
        state = self._state(guild_id)
        random.shuffle(state.queue)

    async def adjust_volume(self, guild_id: int, delta: int):
        """delta is +10/-10 from the panel buttons. Clamped 0-200%.
        Applies live via PCMVolumeTransformer if something's currently
        playing/paused, and always persists on state so the NEXT track
        picks it up too."""
        state = self._state(guild_id)
        state.volume = max(0, min(200, state.volume + delta))
        guild = self.bot.get_guild(guild_id)
        if guild is not None and guild.voice_client is not None:
            source = guild.voice_client.source
            if isinstance(source, discord.PCMVolumeTransformer):
                source.volume = state.volume / 100

    # --- voice-channel following ------------------------------------------

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        if member.bot:
            return
        guild = member.guild
        voice_client = guild.voice_client
        if voice_client is None:
            return
        state = self._state(guild.id)

        # Follow only the current track's owner, and only when they moved
        # to a real channel (not disconnecting entirely).
        if (
            state.current is not None
            and member.id == state.current.get("queued_by")
            and after.channel is not None
            and after.channel.id != voice_client.channel.id
        ):
            try:
                await voice_client.move_to(after.channel)
            except discord.HTTPException as e:
                logger.warning(f"[v0] Couldn't follow track owner to new channel in guild {guild.id}: {e}")

    @tasks.loop(seconds=SOLO_CHECK_INTERVAL_SECONDS)
    async def voice_solo_watcher(self):
        for guild in self.bot.guilds:
            voice_client = guild.voice_client
            if voice_client is None or voice_client.channel is None:
                continue
            state = self._state(guild.id)
            non_bot_members = [m for m in voice_client.channel.members if not m.bot]
            if non_bot_members:
                state.solo_since = None
                continue
            if state.solo_since is None:
                state.solo_since = time.monotonic()
                continue
            if time.monotonic() - state.solo_since >= SOLO_GRACE_SECONDS:
                state.queue.clear()
                state.current = None
                state.current_started_at = None
                state.solo_since = None
                try:
                    voice_client.stop()
                    await voice_client.disconnect(force=False)
                except discord.HTTPException:
                    pass
                clone_id = _clone_id_of(self.bot)
                await refresh_posted_panel(self.bot, guild.id, clone_id=clone_id)
                await self._refresh_voice_panel_if_any(guild.id, clone_id)

    @voice_solo_watcher.before_loop
    async def _before_solo_watcher(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(MusicCog(bot))
