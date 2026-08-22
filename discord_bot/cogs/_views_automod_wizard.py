# path: discord_bot/cogs/_views_automod_wizard.py

"""
Bumper-style multi-step setup wizard for /automod setup.

Same pattern as discord_bot/cogs/_views_welcome.py — one message, a
checklist that fills in with ✅ as each step is completed, everything
built from discord.ui.DynamicItem (not plain View + in-memory state):

  - timeout=None on the outer LayoutView, no on_timeout handler. Every
    component re-fetches the current automod config from the DB itself
    on click, so there's no stale in-memory snapshot that can go bad
    while the message sits untouched.
  - restart-proof: dynamic items are matched by a regex against
    custom_id and reconstructed on the fly (guild_id / clone_id /
    invoker_id are encoded straight into the custom_id), so a click on
    a wizard message posted before a restart still works identically
    after one — registered once via bot.add_dynamic_items(*DYNAMIC_ITEMS)
    in discord_bot/bot.py's setup_hook, same as the welcome wizard.

Kept in its own file (not merged into _views_moderation.py, which holds
the confirm/warn/modlog views for the existing slash commands) for the
same reason _views_welcome.py is separate from welcome.py: this is a
bespoke multi-piece flow, not a generic one-button nav aid.
"""

import re

import discord

from database import db
from discord_bot.cogs._views_shared import check_wizard_access

VALID_ACTIONS = {"delete", "warn", "timeout", "kick"}
TIMEOUT_MINUTE_CHOICES = [5, 10, 30, 60, 360, 1440]
MENTION_THRESHOLD_CHOICES = [3, 5, 10, 15, 20, 30]

FILTER_FIELD_MAP = {
    "word_filter": "word_filter_enabled",
    "anti_invite": "anti_invite_enabled",
    "anti_mention": "anti_mention_enabled",
    "spam": "spam_enabled",
}
FILTER_LABELS = {
    "word_filter": "Word filter",
    "anti_invite": "Invite-link filter",
    "anti_mention": "Mass-mention filter",
    "spam": "Spam / flood filter",
}


def _clone_id_of(interaction: discord.Interaction):
    return getattr(interaction.client, "clone_id", None)


def _status_color(config: dict) -> discord.Color:
    any_on = any(config.get(f) for f in FILTER_FIELD_MAP.values())
    return discord.Color.green() if any_on else discord.Color.blurple()


def render_status_lines(config: dict) -> list:
    log_id = config.get("log_channel_id")
    log = f"<#{log_id}>" if log_id else "*not set*"
    action = config.get("action", "delete")
    action_label = action + (f" ({config.get('timeout_minutes', 10)}min)" if action == "timeout" else "")
    threshold = config.get("anti_mention_threshold", 5)
    enabled_filters = [FILTER_LABELS[f] for f, col in FILTER_FIELD_MAP.items() if config.get(col)]
    filters_label = ", ".join(enabled_filters) if enabled_filters else "none enabled"
    word_count = len(config.get("banned_words") or [])

    step1 = "✅" if log_id else "⬜"
    step4 = "✅" if enabled_filters else "⬜"

    return [
        f"{step1} **Step 1: Mod-log channel** — {log}",
        f"✅ **Step 2: Violation action** — {action_label}",
        f"✅ **Step 3: Mass-mention threshold** — {threshold}",
        f"{step4} **Step 4: Filters** — {filters_label}",
        f"-# Banned words on file: {word_count}",
    ]


# ---------------------------------------------------------------------------
# custom_id shape: modwz_<field>:<guild_id>:<clone_id or "-">:<invoker_id or "-">
# invoker_id "-" means "anyone with Manage Server", same convention as the
# welcome wizard.
# ---------------------------------------------------------------------------

def _encode(field: str, guild_id: int, clone_id, invoker_id) -> str:
    clone_part = "-" if clone_id is None else str(clone_id)
    inv_part = "-" if invoker_id is None else str(invoker_id)
    return f"modwz_{field}:{guild_id}:{clone_part}:{inv_part}"


def _decode(match: "re.Match"):
    guild_id = int(match.group(1))
    clone_part = match.group(2)
    inv_part = match.group(3)
    clone_id = None if clone_part == "-" else int(clone_part)
    invoker_id = None if inv_part == "-" else int(inv_part)
    return guild_id, clone_id, invoker_id


def _id_pattern(field: str) -> str:
    return rf"^modwz_{field}:(\d+):(-|\d+):(-|\d+)$"


async def _check_access(interaction: discord.Interaction, invoker_id) -> bool:
    return await check_wizard_access(interaction, invoker_id, "automod", "manage_guild", "Manage Server")


def build_wizard_view(guild_id: int, clone_id, invoker_id, config: dict) -> discord.ui.LayoutView:
    """Renders a fresh wizard message from a config dict already fetched
    by the caller. Every dynamic item inside re-fetches its own current
    config on interaction rather than trusting this snapshot."""
    view = discord.ui.LayoutView(timeout=None)
    container = discord.ui.Container(accent_colour=_status_color(config))

    chan_row = discord.ui.ActionRow()
    chan_row.add_item(AutomodLogChannelSelect(guild_id, clone_id, invoker_id, config))
    action_row = discord.ui.ActionRow()
    action_row.add_item(AutomodActionSelect(guild_id, clone_id, invoker_id, config))
    timeout_row = discord.ui.ActionRow()
    timeout_row.add_item(AutomodTimeoutMinutesSelect(guild_id, clone_id, invoker_id, config))
    mention_row = discord.ui.ActionRow()
    mention_row.add_item(AutomodMentionThresholdSelect(guild_id, clone_id, invoker_id, config))
    filters_row = discord.ui.ActionRow()
    filters_row.add_item(AutomodFiltersSelect(guild_id, clone_id, invoker_id, config))

    button_row = discord.ui.ActionRow()
    button_row.add_item(AutomodAddWordButton(guild_id, clone_id, invoker_id))
    button_row.add_item(AutomodPresetWordsButton(guild_id, clone_id, invoker_id))
    button_row.add_item(AutomodListWordsButton(guild_id, clone_id, invoker_id))

    text = discord.ui.TextDisplay("\n".join(["### 🛡️ Set up moderation", *render_status_lines(config)]))
    for item in (text, discord.ui.Separator(), chan_row, action_row, timeout_row, mention_row, filters_row, discord.ui.Separator(), button_row):
        container.add_item(item)

    view.add_item(container)
    return view


async def _rerender(interaction: discord.Interaction, guild_id: int, clone_id, invoker_id):
    # is_done() guard: some callers (e.g. toggle/action buttons that do
    # async work before this) already defer()/respond before calling in —
    # calling response.defer() again would raise InteractionResponded.
    if not interaction.response.is_done():
        await interaction.response.defer()
    config = await db.get_automod_config(guild_id, clone_id=clone_id)
    view = build_wizard_view(guild_id, clone_id, invoker_id, config)
    await interaction.edit_original_response(view=view)


async def remember_wizard_message(guild_id: int, clone_id, invoker_id, channel_id: int, message_id: int) -> None:
    """Called once, right after /automod setup posts the wizard, so the
    standalone /automod commands can find it again later. Mirrors
    set_welcome_config's wizard_* bookkeeping."""
    await db.set_automod_config(
        guild_id, clone_id=clone_id,
        wizard_channel_id=channel_id, wizard_message_id=message_id, wizard_invoker_id=invoker_id,
    )


async def refresh_posted_wizard(bot, guild_id: int, clone_id=None) -> None:
    """Called by the standalone /automod commands (toggle/action/
    mentionthreshold/setlogchannel/bannedword add/remove/preset) after they
    write a change directly, bypassing the wizard entirely. Without this, a
    wizard message left open in a channel would keep showing whatever it
    last rendered until someone happened to click one of its own
    components — this pushes the DB's current state onto it immediately.

    Best-effort and silent: no pointer recorded yet, channel deleted,
    message deleted, or the bot no longer having access are all normal,
    non-error situations, so any failure here just means there was nothing
    to refresh."""
    config = await db.get_automod_config(guild_id, clone_id=clone_id)
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


class AutomodBannedWordModal(discord.ui.Modal, title="Add a blocked word/phrase"):
    def __init__(self, guild_id: int, clone_id, invoker_id):
        super().__init__()
        self.guild_id = guild_id
        self.clone_id = clone_id
        self.invoker_id = invoker_id
        self.word = discord.ui.TextInput(
            label="Word or phrase to block",
            style=discord.TextStyle.short,
            max_length=100,
            required=True,
        )
        self.add_item(self.word)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        ok = await db.add_automod_banned_word(self.guild_id, str(self.word.value), clone_id=self.clone_id)
        # Turning word_filter on isn't automatic here on purpose — an admin
        # adding one word shouldn't silently flip enforcement on; Step 4's
        # filter select is the explicit switch for that.
        msg = "✅ Added." if ok else "❌ Already on the list (or invalid)."
        await interaction.edit_original_response(view=await _rendered_view(self.guild_id, self.clone_id, self.invoker_id))
        await interaction.followup.send(msg, ephemeral=True)


async def _rendered_view(guild_id: int, clone_id, invoker_id) -> discord.ui.LayoutView:
    config = await db.get_automod_config(guild_id, clone_id=clone_id)
    return build_wizard_view(guild_id, clone_id, invoker_id, config)


class AutomodLogChannelSelect(discord.ui.DynamicItem[discord.ui.ChannelSelect], template=_id_pattern("chan")):
    def __init__(self, guild_id: int, clone_id, invoker_id, config: dict):
        self.guild_id = guild_id
        self.clone_id = clone_id
        self.invoker_id = invoker_id
        super().__init__(discord.ui.ChannelSelect(
            placeholder="Step 1 — pick the mod-log channel",
            channel_types=[discord.ChannelType.text],
            min_values=1, max_values=1,
            custom_id=_encode("chan", guild_id, clone_id, invoker_id),
        ))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        guild_id, clone_id, invoker_id = _decode(match)
        return cls(guild_id, clone_id, invoker_id, {})

    async def callback(self, interaction: discord.Interaction):
        if not await _check_access(interaction, self.invoker_id):
            return
        channel = self.item.values[0]
        await db.set_automod_config(
            self.guild_id, clone_id=self.clone_id,
            log_channel_id=channel.id, log_channel_auto_created=False,
            log_channel_notice_count=0, log_channel_last_notice_at=None,
        )
        await _rerender(interaction, self.guild_id, self.clone_id, self.invoker_id)


class AutomodActionSelect(discord.ui.DynamicItem[discord.ui.Select], template=_id_pattern("action")):
    def __init__(self, guild_id: int, clone_id, invoker_id, config: dict):
        self.guild_id = guild_id
        self.clone_id = clone_id
        self.invoker_id = invoker_id
        current = config.get("action", "delete")
        descriptions = {
            "delete": "Just remove the offending message",
            "warn": "Delete and log a formal warning",
            "timeout": "Delete and time the member out",
            "kick": "Delete and kick the member",
        }
        options = [
            discord.SelectOption(label=a.capitalize(), value=a, description=descriptions[a], default=(a == current))
            for a in sorted(VALID_ACTIONS)
        ]
        super().__init__(discord.ui.Select(
            placeholder="Step 2 — violation action", options=options,
            custom_id=_encode("action", guild_id, clone_id, invoker_id),
        ))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        guild_id, clone_id, invoker_id = _decode(match)
        return cls(guild_id, clone_id, invoker_id, {})

    async def callback(self, interaction: discord.Interaction):
        if not await _check_access(interaction, self.invoker_id):
            return
        await db.set_automod_config(self.guild_id, clone_id=self.clone_id, action=self.item.values[0])
        await _rerender(interaction, self.guild_id, self.clone_id, self.invoker_id)


class AutomodTimeoutMinutesSelect(discord.ui.DynamicItem[discord.ui.Select], template=_id_pattern("tmin")):
    """Only matters when action=timeout, but always shown/settable so an
    admin can pre-configure it before switching the action over."""

    def __init__(self, guild_id: int, clone_id, invoker_id, config: dict):
        self.guild_id = guild_id
        self.clone_id = clone_id
        self.invoker_id = invoker_id
        current = config.get("timeout_minutes", 10)
        options = [
            discord.SelectOption(label=f"{m} minutes" if m < 1440 else f"{m // 1440} day(s)", value=str(m), default=(m == current))
            for m in TIMEOUT_MINUTE_CHOICES
        ]
        super().__init__(discord.ui.Select(
            placeholder="Timeout duration (used when action = timeout)", options=options,
            custom_id=_encode("tmin", guild_id, clone_id, invoker_id),
        ))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        guild_id, clone_id, invoker_id = _decode(match)
        return cls(guild_id, clone_id, invoker_id, {})

    async def callback(self, interaction: discord.Interaction):
        if not await _check_access(interaction, self.invoker_id):
            return
        await db.set_automod_config(self.guild_id, clone_id=self.clone_id, timeout_minutes=int(self.item.values[0]))
        await _rerender(interaction, self.guild_id, self.clone_id, self.invoker_id)


class AutomodMentionThresholdSelect(discord.ui.DynamicItem[discord.ui.Select], template=_id_pattern("mention")):
    def __init__(self, guild_id: int, clone_id, invoker_id, config: dict):
        self.guild_id = guild_id
        self.clone_id = clone_id
        self.invoker_id = invoker_id
        current = config.get("anti_mention_threshold", 5)
        options = [
            discord.SelectOption(label=f"{n} mentions", value=str(n), default=(n == current))
            for n in MENTION_THRESHOLD_CHOICES
        ]
        super().__init__(discord.ui.Select(
            placeholder="Step 3 — mass-mention threshold", options=options,
            custom_id=_encode("mention", guild_id, clone_id, invoker_id),
        ))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        guild_id, clone_id, invoker_id = _decode(match)
        return cls(guild_id, clone_id, invoker_id, {})

    async def callback(self, interaction: discord.Interaction):
        if not await _check_access(interaction, self.invoker_id):
            return
        await db.set_automod_config(self.guild_id, clone_id=self.clone_id, anti_mention_threshold=int(self.item.values[0]))
        await _rerender(interaction, self.guild_id, self.clone_id, self.invoker_id)


class AutomodFiltersSelect(discord.ui.DynamicItem[discord.ui.Select], template=_id_pattern("filters")):
    """Multi-select toggle for all four filters at once — replaces having
    to run /automod toggle four separate times."""

    def __init__(self, guild_id: int, clone_id, invoker_id, config: dict):
        self.guild_id = guild_id
        self.clone_id = clone_id
        self.invoker_id = invoker_id
        options = [
            discord.SelectOption(label=FILTER_LABELS[f], value=f, default=bool(config.get(col)))
            for f, col in FILTER_FIELD_MAP.items()
        ]
        super().__init__(discord.ui.Select(
            placeholder="Step 4 — enable filters (word / invite / mention / spam)",
            options=options, min_values=0, max_values=len(options),
            custom_id=_encode("filters", guild_id, clone_id, invoker_id),
        ))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        guild_id, clone_id, invoker_id = _decode(match)
        return cls(guild_id, clone_id, invoker_id, {})

    async def callback(self, interaction: discord.Interaction):
        if not await _check_access(interaction, self.invoker_id):
            return
        selected = set(self.item.values)
        updates = {col: (f in selected) for f, col in FILTER_FIELD_MAP.items()}
        await db.set_automod_config(self.guild_id, clone_id=self.clone_id, **updates)
        await _rerender(interaction, self.guild_id, self.clone_id, self.invoker_id)


class AutomodAddWordButton(discord.ui.DynamicItem[discord.ui.Button], template=_id_pattern("addword")):
    def __init__(self, guild_id: int, clone_id, invoker_id):
        self.guild_id = guild_id
        self.clone_id = clone_id
        self.invoker_id = invoker_id
        super().__init__(discord.ui.Button(
            label="✏️ Edit banned words", style=discord.ButtonStyle.secondary,
            custom_id=_encode("addword", guild_id, clone_id, invoker_id),
        ))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        guild_id, clone_id, invoker_id = _decode(match)
        return cls(guild_id, clone_id, invoker_id)

    async def callback(self, interaction: discord.Interaction):
        if not await _check_access(interaction, self.invoker_id):
            return
        await interaction.response.send_modal(AutomodBannedWordModal(self.guild_id, self.clone_id, self.invoker_id))


class AutomodPresetWordsButton(discord.ui.DynamicItem[discord.ui.Button], template=_id_pattern("preset")):
    def __init__(self, guild_id: int, clone_id, invoker_id):
        self.guild_id = guild_id
        self.clone_id = clone_id
        self.invoker_id = invoker_id
        super().__init__(discord.ui.Button(
            label="📥 Load preset list", style=discord.ButtonStyle.secondary,
            custom_id=_encode("preset", guild_id, clone_id, invoker_id),
        ))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        guild_id, clone_id, invoker_id = _decode(match)
        return cls(guild_id, clone_id, invoker_id)

    async def callback(self, interaction: discord.Interaction):
        import os
        if not await _check_access(interaction, self.invoker_id):
            return
        await interaction.response.defer()
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            "data", "preset_banned_words.txt",
        )
        try:
            with open(path, encoding="utf-8") as f:
                preset_words = [line.strip() for line in f if line.strip()]
        except OSError:
            await interaction.followup.send("❌ Preset list file is missing on the bot host.", ephemeral=True)
            return
        added = await db.add_automod_banned_words_bulk(self.guild_id, preset_words, clone_id=self.clone_id)
        config = await db.get_automod_config(self.guild_id, clone_id=self.clone_id)
        view = build_wizard_view(self.guild_id, self.clone_id, self.invoker_id, config)
        await interaction.edit_original_response(view=view)
        await interaction.followup.send(f"✅ Added {added} word(s)/phrase(s) from the preset list.", ephemeral=True)


class AutomodListWordsButton(discord.ui.DynamicItem[discord.ui.Button], template=_id_pattern("listwords")):
    def __init__(self, guild_id: int, clone_id, invoker_id):
        self.guild_id = guild_id
        self.clone_id = clone_id
        self.invoker_id = invoker_id
        super().__init__(discord.ui.Button(
            label="🚫 View blocked words", style=discord.ButtonStyle.primary,
            custom_id=_encode("listwords", guild_id, clone_id, invoker_id),
        ))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        guild_id, clone_id, invoker_id = _decode(match)
        return cls(guild_id, clone_id, invoker_id)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        if not await _check_access(interaction, self.invoker_id):
            return
        config = await db.get_automod_config(self.guild_id, clone_id=self.clone_id)
        words = config.get("banned_words") or []
        text = ", ".join(f"`{w}`" for w in words) if words else "No blocked words configured."
        await interaction.followup.send(f"**Blocked words:**\n{text}", ephemeral=True)


# Registered once in discord_bot/bot.py's setup_hook via
# bot.add_dynamic_items(*DYNAMIC_ITEMS) — same mechanism as the welcome
# wizard's DYNAMIC_ITEMS, so these keep working after a restart regardless
# of which process originally sent the message.
DYNAMIC_ITEMS = (
    AutomodLogChannelSelect, AutomodActionSelect, AutomodTimeoutMinutesSelect,
    AutomodMentionThresholdSelect, AutomodFiltersSelect,
    AutomodAddWordButton, AutomodPresetWordsButton, AutomodListWordsButton,
)
