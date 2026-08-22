# path: discord_bot/cogs/_views_community_wizard.py

"""
Bumper-style setup wizard for /community setup — combines starboard and
suggestions into one message, matching the community_setup_wizard.html
mockup's two-section layout. Same DynamicItem/restart-proof pattern as
_views_welcome.py, _views_automod_wizard.py, and _views_ticket_wizard.py.

Two separate config tables back this one message (discord_starboard_config,
discord_suggestion_config) — the wizard pointer (wizard_channel_id/
wizard_message_id/wizard_invoker_id) lives only on the starboard table,
since one pointer is enough to find the single combined message again.
"""

import re

import discord

from database import db
from discord_bot.cogs._views_shared import check_wizard_access


def _clone_id_of(interaction: discord.Interaction):
    return getattr(interaction.client, "clone_id", None)


def _status_color(starboard_config: dict, suggestion_config: dict) -> discord.Color:
    any_on = bool(starboard_config.get("channel_id")) or bool(suggestion_config.get("approved_log_channel_id"))
    return discord.Color.green() if any_on else discord.Color.blurple()


def render_status_lines(starboard_config: dict, suggestion_config: dict) -> list:
    sb_chan = starboard_config.get("channel_id")
    sb_threshold = starboard_config.get("threshold", 5)
    sb_emoji = starboard_config.get("emoji", "⭐")
    sug_chan = suggestion_config.get("approved_log_channel_id")

    return [
        "**⭐ Starboard**",
        f"{'✅' if sb_chan else '⬜'} Channel — {f'<#{sb_chan}>' if sb_chan else '*not set*'}",
        f"-# Threshold: {sb_threshold} {sb_emoji} · {'enabled' if sb_chan else 'disabled'}",
        "**💡 Suggestions**",
        f"{'✅' if sug_chan else '⬜'} Approved-log channel — {f'<#{sug_chan}>' if sug_chan else '*not set*'}",
    ]


# ---------------------------------------------------------------------------
# custom_id shape: commwz_<field>:<guild_id>:<clone_id or "-">:<invoker_id or "-">
# ---------------------------------------------------------------------------

def _encode(field: str, guild_id: int, clone_id, invoker_id) -> str:
    clone_part = "-" if clone_id is None else str(clone_id)
    inv_part = "-" if invoker_id is None else str(invoker_id)
    return f"commwz_{field}:{guild_id}:{clone_part}:{inv_part}"


def _decode(match: "re.Match"):
    guild_id = int(match.group(1))
    clone_part = match.group(2)
    inv_part = match.group(3)
    clone_id = None if clone_part == "-" else int(clone_part)
    invoker_id = None if inv_part == "-" else int(inv_part)
    return guild_id, clone_id, invoker_id


def _id_pattern(field: str) -> str:
    return rf"^commwz_{field}:(\d+):(-|\d+):(-|\d+)$"


async def _check_access(interaction: discord.Interaction, invoker_id) -> bool:
    return await check_wizard_access(interaction, invoker_id, "community", "manage_guild", "Manage Server")


def build_wizard_view(guild_id: int, clone_id, invoker_id, starboard_config: dict, suggestion_config: dict) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=None)
    container = discord.ui.Container(accent_colour=_status_color(starboard_config, suggestion_config))

    sb_chan_row = discord.ui.ActionRow()
    sb_chan_row.add_item(StarboardChannelSelect(guild_id, clone_id, invoker_id, starboard_config))
    sb_ctrl_row = discord.ui.ActionRow()
    sb_ctrl_row.add_item(StarboardThresholdSelect(guild_id, clone_id, invoker_id, starboard_config))
    sb_ctrl_row.add_item(StarboardToggleButton(guild_id, clone_id, invoker_id, starboard_config))

    sug_chan_row = discord.ui.ActionRow()
    sug_chan_row.add_item(SuggestionLogChannelSelect(guild_id, clone_id, invoker_id, suggestion_config))
    sug_ctrl_row = discord.ui.ActionRow()
    sug_ctrl_row.add_item(SuggestionToggleButton(guild_id, clone_id, invoker_id, suggestion_config))

    text = discord.ui.TextDisplay("\n".join(["### ⭐ Set up community features", *render_status_lines(starboard_config, suggestion_config)]))
    for item in (text, discord.ui.Separator(), sb_chan_row, sb_ctrl_row, discord.ui.Separator(), sug_chan_row, sug_ctrl_row):
        container.add_item(item)

    view.add_item(container)
    return view


async def _rerender(interaction: discord.Interaction, guild_id: int, clone_id, invoker_id):
    # is_done() guard: some callers (e.g. toggle/action buttons that do
    # async work before this) already defer()/respond before calling in —
    # calling response.defer() again would raise InteractionResponded.
    if not interaction.response.is_done():
        await interaction.response.defer()
    starboard_config = await db.get_starboard_config(guild_id, clone_id=clone_id)
    suggestion_config = await db.get_suggestion_config(guild_id, clone_id=clone_id)
    view = build_wizard_view(guild_id, clone_id, invoker_id, starboard_config, suggestion_config)
    await interaction.edit_original_response(view=view)


async def remember_wizard_message(guild_id: int, clone_id, invoker_id, channel_id: int, message_id: int) -> None:
    await db.set_starboard_config(
        guild_id, clone_id=clone_id,
        wizard_channel_id=channel_id, wizard_message_id=message_id, wizard_invoker_id=invoker_id,
    )


async def refresh_posted_wizard(bot, guild_id: int, clone_id=None) -> None:
    """Best-effort, silent — mirrors the other wizards' version. Called by
    the standalone /starboard and /suggestions commands after they write."""
    starboard_config = await db.get_starboard_config(guild_id, clone_id=clone_id)
    channel_id = starboard_config.get("wizard_channel_id")
    message_id = starboard_config.get("wizard_message_id")
    if not channel_id or not message_id:
        return
    channel = bot.get_channel(int(channel_id))
    if channel is None:
        return
    try:
        message = await channel.fetch_message(int(message_id))
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return
    invoker_raw = starboard_config.get("wizard_invoker_id")
    invoker_id = int(invoker_raw) if invoker_raw is not None else None
    suggestion_config = await db.get_suggestion_config(guild_id, clone_id=clone_id)
    view = build_wizard_view(guild_id, clone_id, invoker_id, starboard_config, suggestion_config)
    try:
        await message.edit(view=view)
    except (discord.Forbidden, discord.HTTPException):
        pass


class StarboardChannelSelect(discord.ui.DynamicItem[discord.ui.ChannelSelect], template=_id_pattern("sbchan")):
    def __init__(self, guild_id: int, clone_id, invoker_id, config: dict):
        self.guild_id = guild_id
        self.clone_id = clone_id
        self.invoker_id = invoker_id
        super().__init__(discord.ui.ChannelSelect(
            placeholder="Starboard — pick the starboard channel",
            channel_types=[discord.ChannelType.text],
            min_values=1, max_values=1,
            custom_id=_encode("sbchan", guild_id, clone_id, invoker_id),
        ))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        guild_id, clone_id, invoker_id = _decode(match)
        return cls(guild_id, clone_id, invoker_id, {})

    async def callback(self, interaction: discord.Interaction):
        if not await _check_access(interaction, self.invoker_id):
            return
        channel = self.item.values[0]
        await db.set_starboard_config(self.guild_id, clone_id=self.clone_id, channel_id=channel.id)
        await _rerender(interaction, self.guild_id, self.clone_id, self.invoker_id)


class StarboardThresholdSelect(discord.ui.DynamicItem[discord.ui.Select], template=_id_pattern("sbthresh")):
    CHOICES = [3, 5, 8, 12]

    def __init__(self, guild_id: int, clone_id, invoker_id, config: dict):
        self.guild_id = guild_id
        self.clone_id = clone_id
        self.invoker_id = invoker_id
        current = config.get("threshold", 5)
        options = [
            discord.SelectOption(label=f"{n} stars", value=str(n), default=(n == current))
            for n in self.CHOICES
        ]
        super().__init__(discord.ui.Select(
            placeholder="Star threshold", options=options,
            custom_id=_encode("sbthresh", guild_id, clone_id, invoker_id),
        ))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        guild_id, clone_id, invoker_id = _decode(match)
        return cls(guild_id, clone_id, invoker_id, {})

    async def callback(self, interaction: discord.Interaction):
        if not await _check_access(interaction, self.invoker_id):
            return
        await db.set_starboard_config(self.guild_id, clone_id=self.clone_id, threshold=int(self.item.values[0]))
        await _rerender(interaction, self.guild_id, self.clone_id, self.invoker_id)


class StarboardToggleButton(discord.ui.DynamicItem[discord.ui.Button], template=_id_pattern("sbtoggle")):
    def __init__(self, guild_id: int, clone_id, invoker_id, config: dict):
        self.guild_id = guild_id
        self.clone_id = clone_id
        self.invoker_id = invoker_id
        enabled = bool(config.get("channel_id"))
        self._last_channel_id = config.get("channel_id")
        super().__init__(discord.ui.Button(
            label="Disable" if enabled else "Enable",
            style=discord.ButtonStyle.danger if enabled else discord.ButtonStyle.success,
            custom_id=_encode("sbtoggle", guild_id, clone_id, invoker_id),
        ))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        guild_id, clone_id, invoker_id = _decode(match)
        return cls(guild_id, clone_id, invoker_id, {})

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        if not await _check_access(interaction, self.invoker_id):
            return
        config = await db.get_starboard_config(self.guild_id, clone_id=self.clone_id)
        if config.get("channel_id"):
            await db.set_starboard_config(self.guild_id, clone_id=self.clone_id, channel_id=None)
        else:
            await interaction.followup.send("Pick a starboard channel first.", ephemeral=True)
            return
        await _rerender(interaction, self.guild_id, self.clone_id, self.invoker_id)


class SuggestionLogChannelSelect(discord.ui.DynamicItem[discord.ui.ChannelSelect], template=_id_pattern("sugchan")):
    def __init__(self, guild_id: int, clone_id, invoker_id, config: dict):
        self.guild_id = guild_id
        self.clone_id = clone_id
        self.invoker_id = invoker_id
        super().__init__(discord.ui.ChannelSelect(
            placeholder="Suggestions — pick the approved-log channel",
            channel_types=[discord.ChannelType.text],
            min_values=1, max_values=1,
            custom_id=_encode("sugchan", guild_id, clone_id, invoker_id),
        ))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        guild_id, clone_id, invoker_id = _decode(match)
        return cls(guild_id, clone_id, invoker_id, {})

    async def callback(self, interaction: discord.Interaction):
        if not await _check_access(interaction, self.invoker_id):
            return
        channel = self.item.values[0]
        await db.set_suggestion_config(self.guild_id, clone_id=self.clone_id, approved_log_channel_id=channel.id)
        await _rerender(interaction, self.guild_id, self.clone_id, self.invoker_id)


class SuggestionToggleButton(discord.ui.DynamicItem[discord.ui.Button], template=_id_pattern("sugtoggle")):
    """Suggestions themselves (/suggest) work regardless of this config —
    approved_log_channel_id only controls whether approvals ALSO get
    logged somewhere. So "Disable" here means "stop logging", not "turn
    off /suggest" — labeled accordingly rather than reusing the starboard
    button's generic Enable/Disable wording."""

    def __init__(self, guild_id: int, clone_id, invoker_id, config: dict):
        self.guild_id = guild_id
        self.clone_id = clone_id
        self.invoker_id = invoker_id
        enabled = bool(config.get("approved_log_channel_id"))
        super().__init__(discord.ui.Button(
            label="Stop logging" if enabled else "Enable logging",
            style=discord.ButtonStyle.danger if enabled else discord.ButtonStyle.success,
            custom_id=_encode("sugtoggle", guild_id, clone_id, invoker_id),
        ))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        guild_id, clone_id, invoker_id = _decode(match)
        return cls(guild_id, clone_id, invoker_id, {})

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        if not await _check_access(interaction, self.invoker_id):
            return
        config = await db.get_suggestion_config(self.guild_id, clone_id=self.clone_id)
        if config.get("approved_log_channel_id"):
            await db.set_suggestion_config(self.guild_id, clone_id=self.clone_id, approved_log_channel_id=None)
        else:
            await interaction.followup.send("Pick an approved-log channel first.", ephemeral=True)
            return
        await _rerender(interaction, self.guild_id, self.clone_id, self.invoker_id)


DYNAMIC_ITEMS = (
    StarboardChannelSelect, StarboardThresholdSelect, StarboardToggleButton,
    SuggestionLogChannelSelect, SuggestionToggleButton,
)
