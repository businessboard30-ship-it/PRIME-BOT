# path: discord_bot/cogs/_views_music_panel.py

"""
Now Playing panel — Components V2 (LayoutView + Container + MediaGallery +
ActionRow), same restart-proof DynamicItem pattern as every other wizard in
this repo (see _views_leveling_wizard.py's docstring for the general shape).

Unlike the wizards, this panel has NO invoker restriction — the owner's
choice (confirmed) is that pause/skip/stop/queue/loop are open to anyone in
the voice channel, not gated to whoever queued first or an admin. So the
buttons below skip check_wizard_access entirely and just re-read live state
from the cog's in-memory GuildMusicState on every press.

Layout: MediaGallery (track thumbnail, pulled from yt-dlp's `thumbnail`
metadata field) -> TextDisplay (title/artist/queued-by/progress) ->
Separator -> ActionRow 1 (pause/resume, skip, stop) -> ActionRow 2
(queue, loop). Two separate ActionRows — never mixing more than what fits,
and this repo's known bug (button + select in one ActionRow raises
"maximum number of children exceeded") doesn't apply here since every item
below is a button, but kept as two rows anyway to match the mockup's two
button rows and to leave room for a future select without needing to move
existing buttons into a new row.

The panel is a SINGLE persistent message per guild, edited in place on
every track change / button press — never a new message per song. Callers
that need to build/refresh it call build_panel_view(), which pulls current
state from the passed-in GuildMusicState (owned by music.py's cog, not
this file — this file only renders).
"""

import re

import discord

LOOP_LABELS = {"off": "loop off", "track": "loop track", "queue": "loop queue"}
LOOP_ORDER = ["off", "track", "queue"]


def _id_pattern(field: str) -> str:
    return rf"^musicpanel_{field}:(\d+):(-|\d+)$"


def _encode(field: str, guild_id: int, clone_id) -> str:
    clone_part = "-" if clone_id is None else str(clone_id)
    return f"musicpanel_{field}:{guild_id}:{clone_part}"


def _decode(match: "re.Match"):
    guild_id = int(match.group(1))
    clone_part = match.group(2)
    clone_id = None if clone_part == "-" else int(clone_part)
    return guild_id, clone_id


def _progress_bar(current_seconds: float, total_seconds: float, width: int = 14) -> str:
    if not total_seconds:
        return "░" * width
    filled = int(width * min(1.0, current_seconds / total_seconds))
    return "█" * filled + "░" * (width - filled)


def _fmt_time(seconds: float) -> str:
    seconds = max(0, int(seconds or 0))
    m, s = divmod(seconds, 60)
    return f"{m}:{s:02d}"


def build_panel_view(guild_id: int, clone_id, state: "MusicPanelState") -> discord.ui.LayoutView:
    """state is a plain data snapshot (see music.py's GuildMusicState.panel_snapshot())
    — this file has no dependency on the queue/voice logic itself, only on
    the small dict-like shape it reads below."""
    view = discord.ui.LayoutView(timeout=None)
    container = discord.ui.Container(accent_colour=discord.Color.blurple())

    items = []

    if not state.get("current"):
        items.append(discord.ui.TextDisplay("### 🎵 Now playing\nNothing queued right now. Use **/setup music** to queue a link."))
        container.add_item(items[0])
        view.add_item(container)
        return view

    current = state["current"]
    thumbnail = current.get("thumbnail")
    if thumbnail:
        gallery = discord.ui.MediaGallery()
        gallery.add_item(media=thumbnail)
        items.append(gallery)

    progress = _progress_bar(current.get("position_seconds", 0), current.get("duration_seconds", 0))
    time_line = f"`{progress}` {_fmt_time(current.get('position_seconds', 0))} / {_fmt_time(current.get('duration_seconds', 0))}"
    header = (
        f"### {current.get('title', 'Unknown track')}\n"
        f"-# {current.get('uploader', 'Unknown artist')} · queued by <@{current['queued_by']}>\n"
        f"{time_line}"
    )
    items.append(discord.ui.TextDisplay(header))

    if state.get("queue"):
        upcoming = state["queue"][0]
        items.append(discord.ui.TextDisplay(f"-# Up next: {upcoming.get('title', 'Unknown track')}"))

    items.append(discord.ui.Separator())

    controls_row = discord.ui.ActionRow()
    controls_row.add_item(MusicPauseResumeButton(guild_id, clone_id, paused=state.get("paused", False)))
    controls_row.add_item(MusicSkipButton(guild_id, clone_id))
    controls_row.add_item(MusicStopButton(guild_id, clone_id))
    items.append(controls_row)

    secondary_row = discord.ui.ActionRow()
    secondary_row.add_item(MusicQueueButton(guild_id, clone_id))
    secondary_row.add_item(MusicLoopButton(guild_id, clone_id, mode=state.get("loop_mode", "off")))
    items.append(secondary_row)

    for item in items:
        container.add_item(item)
    view.add_item(container)
    return view


async def _rerender(interaction: discord.Interaction, guild_id: int, clone_id):
    """Shared re-render used by every button below. Deferring first (rather
    than editing directly off the button press) avoids the interaction
    timing out while we ask the cog for fresh state — same is_done() guard
    convention as _views_leveling_wizard.py's _rerender, since some buttons
    (Stop) do async work of their own before this runs."""
    if not interaction.response.is_done():
        await interaction.response.defer()
    music_cog = interaction.client.get_cog("MusicCog")
    if music_cog is None:
        return
    snapshot = music_cog.panel_snapshot(guild_id)
    view = build_panel_view(guild_id, clone_id, snapshot)
    await interaction.edit_original_response(view=view)


class MusicPauseResumeButton(discord.ui.DynamicItem[discord.ui.Button], template=_id_pattern("pauseresume")):
    def __init__(self, guild_id: int, clone_id, paused: bool = False):
        self.guild_id = guild_id
        self.clone_id = clone_id
        super().__init__(discord.ui.Button(
            label="resume" if paused else "pause",
            emoji="▶️" if paused else "⏸️",
            style=discord.ButtonStyle.secondary,
            custom_id=_encode("pauseresume", guild_id, clone_id),
        ))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        guild_id, clone_id = _decode(match)
        return cls(guild_id, clone_id)

    async def callback(self, interaction: discord.Interaction):
        music_cog = interaction.client.get_cog("MusicCog")
        if music_cog is None:
            await interaction.response.send_message("Music module isn't loaded.", ephemeral=True)
            return
        await music_cog.toggle_pause(self.guild_id)
        await _rerender(interaction, self.guild_id, self.clone_id)


class MusicSkipButton(discord.ui.DynamicItem[discord.ui.Button], template=_id_pattern("skip")):
    def __init__(self, guild_id: int, clone_id):
        self.guild_id = guild_id
        self.clone_id = clone_id
        super().__init__(discord.ui.Button(
            label="skip", emoji="⏭️", style=discord.ButtonStyle.secondary,
            custom_id=_encode("skip", guild_id, clone_id),
        ))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        guild_id, clone_id = _decode(match)
        return cls(guild_id, clone_id)

    async def callback(self, interaction: discord.Interaction):
        music_cog = interaction.client.get_cog("MusicCog")
        if music_cog is None:
            await interaction.response.send_message("Music module isn't loaded.", ephemeral=True)
            return
        await music_cog.skip(self.guild_id)
        await _rerender(interaction, self.guild_id, self.clone_id)


class MusicStopButton(discord.ui.DynamicItem[discord.ui.Button], template=_id_pattern("stop")):
    def __init__(self, guild_id: int, clone_id):
        self.guild_id = guild_id
        self.clone_id = clone_id
        super().__init__(discord.ui.Button(
            label="stop", emoji="⏹️", style=discord.ButtonStyle.danger,
            custom_id=_encode("stop", guild_id, clone_id),
        ))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        guild_id, clone_id = _decode(match)
        return cls(guild_id, clone_id)

    async def callback(self, interaction: discord.Interaction):
        music_cog = interaction.client.get_cog("MusicCog")
        if music_cog is None:
            await interaction.response.send_message("Music module isn't loaded.", ephemeral=True)
            return
        await music_cog.stop(self.guild_id)
        await _rerender(interaction, self.guild_id, self.clone_id)


class MusicQueueButton(discord.ui.DynamicItem[discord.ui.Button], template=_id_pattern("queue")):
    def __init__(self, guild_id: int, clone_id):
        self.guild_id = guild_id
        self.clone_id = clone_id
        super().__init__(discord.ui.Button(
            label="queue", emoji="📃", style=discord.ButtonStyle.secondary,
            custom_id=_encode("queue", guild_id, clone_id),
        ))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        guild_id, clone_id = _decode(match)
        return cls(guild_id, clone_id)

    async def callback(self, interaction: discord.Interaction):
        music_cog = interaction.client.get_cog("MusicCog")
        if music_cog is None:
            await interaction.response.send_message("Music module isn't loaded.", ephemeral=True)
            return
        snapshot = music_cog.panel_snapshot(self.guild_id)
        queue = snapshot.get("queue") or []
        if not queue:
            await interaction.response.send_message("Queue is empty.", ephemeral=True)
            return
        lines = [f"{i}. {t.get('title', 'Unknown track')} — queued by <@{t['queued_by']}>" for i, t in enumerate(queue[:15], start=1)]
        await interaction.response.send_message("**Up next:**\n" + "\n".join(lines), ephemeral=True)


class MusicLoopButton(discord.ui.DynamicItem[discord.ui.Button], template=_id_pattern("loop")):
    def __init__(self, guild_id: int, clone_id, mode: str = "off"):
        self.guild_id = guild_id
        self.clone_id = clone_id
        super().__init__(discord.ui.Button(
            label=LOOP_LABELS.get(mode, "loop off"), emoji="🔁", style=discord.ButtonStyle.secondary,
            custom_id=_encode("loop", guild_id, clone_id),
        ))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        guild_id, clone_id = _decode(match)
        return cls(guild_id, clone_id)

    async def callback(self, interaction: discord.Interaction):
        music_cog = interaction.client.get_cog("MusicCog")
        if music_cog is None:
            await interaction.response.send_message("Music module isn't loaded.", ephemeral=True)
            return
        await music_cog.cycle_loop_mode(self.guild_id)
        await _rerender(interaction, self.guild_id, self.clone_id)


async def remember_panel_message(guild_id: int, clone_id, channel_id: int, message_id: int) -> None:
    from database import db
    await db.set_music_panel(guild_id, clone_id=clone_id, panel_channel_id=channel_id, panel_message_id=message_id)


async def refresh_posted_panel(bot, guild_id: int, clone_id=None) -> None:
    """Re-renders the panel message in place, e.g. right after a track
    naturally advances (not from a button press). Silently no-ops if no
    panel has been posted yet, or if it's since been deleted."""
    from database import db
    panel = await db.get_music_panel(guild_id, clone_id=clone_id)
    channel_id = panel.get("panel_channel_id")
    message_id = panel.get("panel_message_id")
    if not channel_id or not message_id:
        return
    channel = bot.get_channel(int(channel_id))
    if channel is None:
        return
    try:
        message = await channel.fetch_message(int(message_id))
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return
    music_cog = bot.get_cog("MusicCog")
    if music_cog is None:
        return
    snapshot = music_cog.panel_snapshot(guild_id)
    view = build_panel_view(guild_id, clone_id, snapshot)
    try:
        await message.edit(view=view)
    except (discord.Forbidden, discord.HTTPException):
        pass


DYNAMIC_ITEMS = (
    MusicPauseResumeButton, MusicSkipButton, MusicStopButton,
    MusicQueueButton, MusicLoopButton,
)
