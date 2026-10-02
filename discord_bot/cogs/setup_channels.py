# path: discord_bot/cogs/setup_channels.py

"""
/setup channels — scans a guild for a handful of commonly-useful channels
(welcome, automod logs, bump, level-ups, plus 5 "soft" community channels)
that aren't set up yet, and offers to create them in one category with a
seed message + config wiring, individually or all at once.

Detection is split into two kinds:
  - The 4 "core" features (welcome/mod-logs/bump/level-ups) already have a
    feature-config table with a channel_id column — "missing" means that
    column is NULL, same signal each feature's own cog already uses.
  - The 5 "soft" community channels (chatroom, music-room, genz-corner,
    announcements, rules) have no config table of their own. "Missing" is
    a keyword match against existing channel names instead — see
    SOFT_CHANNELS below for the exact keyword lists and the tradeoff
    (false negatives over false-positive duplicate spam) this implies.

Buttons use custom_id-encoded state + a shared on_interaction listener,
not decorator callbacks — same pattern as WelcomeCog's nudge view (see
welcome.py) — because this view must survive bot restarts: a
decorator-bound callback dies when the process restarts, only fixed
custom_ids can be re-attached via bot.add_view().
"""

import logging

import discord
from discord import app_commands
from discord.ext import commands
from discord_bot.cogs._dm_support import GuildOnlyCog
from discord_bot.cogs._views_auto_listing_offer import offer_auto_listing
from discord_bot.cogs._views_download_wizard import (
    build_wizard_view as build_download_wizard_view,
    remember_wizard_message as remember_download_wizard_message,
)

from database import db, get_pool
from modules.text_styles import (
    STYLES, BRACKETS, FREE_FONTS, channel_name, free_style_choices, premium_style_choices,
    free_bracket_choices, premium_bracket_choices, to_plain, strip_brackets, wrap_brackets, plain_name,
)

logger = logging.getLogger(__name__)

# Default look of a suggested channel: its own emoji + a fancy font + brackets,
# e.g. 👋【𝐰𝐞𝐥𝐜𝐨𝐦𝐞】. "bold" and "cjk" are both free-tier, so nothing here needs
# Premium. The owner can still restyle any name from the font panel.
DEFAULT_EMOJI = "☑️"  # fallback only, for a key with no entry below
DEFAULT_FONT = "bold"
DEFAULT_BRACKET = "cjk"
CHANNEL_EMOJIS = {
    "welcome": "👋",
    "mod-logs": "🛡️",
    "bump": "📣",
    "level-ups": "📈",
    "chatroom": "💬",
    "music-room": "🎵",
    "genz-corner": "✨",
    "announcements": "📢",
    "rules": "📜",
}
from modules.text_styles import plain_name as _plain_name

SETUP_CATEGORY_NAME = "📋 Server Setup"

# key -> (default channel name w/o emoji, short description shown in the
# approval embed, seed message sent right after creation)
CORE_CHANNELS = {
    "welcome": (
        "welcome",
        "Where new-member welcome cards get posted.",
        None,  # seeded with the real welcome card/template, not static text — see _seed_channel
    ),
    "mod-logs": (
        "mod-logs",
        "Where automod actions (deletes, timeouts, filtered words) get logged.",
        "🛡️ Automod action logs will appear here. Configure filters with `/automod setup`.",
    ),
    "bump": (
        "bump",
        "Where bump reminders and your server listing get posted.",
        None,  # seeded with the real bump embed intro — see _seed_channel
    ),
    "level-ups": (
        "level-ups",
        "Where level-up announcements get posted.",
        "📈 Level-up announcements will appear here.",
    ),
}

# key -> (default channel name, description, seed message, keyword list)
SOFT_CHANNELS = {
    "chatroom": (
        "chatroom",
        "A general chat channel for members.",
        "💬 Welcome to the chatroom — talk about anything here.",
        ("chat", "general", "talk"),
    ),
    "music-room": (
        "music-room",
        "A channel for music bot commands / requests.",
        "🎵 Music bot commands and song requests go here.",
        ("music", "tunes", "jukebox"),
    ),
    "genz-corner": (
        "genz-corner",
        "A casual, slang-friendly hangout channel.",
        "✨ genz-corner — casual chat, memes, and vibes only.",
        ("genz", "gen-z", "vibes", "rant"),
    ),
    "announcements": (
        "announcements",
        "Where important server announcements get posted.",
        "📣 Server announcements will be posted here.",
        ("announce", "news", "updates"),
    ),
    "rules": (
        "rules",
        "Where your server rules live.",
        "📜 Add your server rules here.",
        ("rules", "guidelines", "tos"),
    ),
}

ALL_KEYS = list(CORE_CHANNELS.keys()) + list(SOFT_CHANNELS.keys())


def _require_perm(interaction: discord.Interaction, perm: str) -> bool:
    if interaction.guild is None:
        return False
    return getattr(interaction.permissions, perm, False)


async def _deny(interaction: discord.Interaction, perm_name: str):
    msg = f"You need the **{perm_name}** permission to do that."
    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)


def _clone_id_of(client) -> int | None:
    return getattr(client, "clone_id", None)


def _default_name(key: str, custom_names: dict) -> str:
    """Returns the name to actually create the channel with — a stored
    custom override if the owner has already renamed this one before, else
    the default: a per-channel emoji, then the name in DEFAULT_FONT wrapped in
    DEFAULT_BRACKET (e.g. 📣【𝐛𝐮𝐦𝐩】)."""
    if key in custom_names:
        return custom_names[key]
    base = CORE_CHANNELS.get(key, SOFT_CHANNELS.get(key))[0]
    emoji = CHANNEL_EMOJIS.get(key, DEFAULT_EMOJI)
    return f"{emoji}{channel_name(base, DEFAULT_FONT, True, DEFAULT_BRACKET)}"


def _create_button_label(entry_name: str) -> str:
    """Discord caps a button label at 80 characters. The rename modal
    allows up to 90 characters for the channel name itself (a real
    Discord channel-name constraint, unrelated to this), but that name
    gets prefixed with "Create " (7 chars) for this button — so a name
    at or near 90 chars overflows the button's own 80-char cap and gets
    rejected by the API with an HTTP 400 on render, even though
    discord.py itself never validates or warns about it locally.
    Truncating here (not in the modal) keeps the modal free to store the
    owner's full chosen name — only this specific button's label needs
    shortening, and the channel is still created with the full name."""
    label = f"Create {entry_name}"
    if len(label) > 80:
        label = label[:79] + "…"
    return label


async def scan_missing_channels(guild: discord.Guild, clone_id: int | None) -> list[dict]:
    """Returns a list of {key, name, description} for every suggested
    channel that isn't set up yet, skipping anything the owner previously
    dismissed. Always re-checks live state — never trusts a stale cache —
    since this is called both from the join DM and every /setup channels
    invocation, potentially long after the guild's channels changed."""
    import asyncio
    suggestions, welcome, automod, bump_cfg, leveling_cfg = await asyncio.gather(
        db.get_setup_suggestions(guild.id, clone_id),
        db.get_welcome_config(guild.id, clone_id),
        db.get_automod_config(guild.id, clone_id),
        db.bump_get_guild_config(guild.id, clone_id),
        db.get_leveling_config(guild.id, clone_id),
    )
    bump_cfg = bump_cfg or {}
    dismissed = set(suggestions["dismissed"])
    custom_names = suggestions["custom_names"]
    missing = []

    # --- core (DB-backed) ---
    if not welcome.get("channel_id") and "welcome" not in dismissed:
        missing.append(_entry("welcome", CORE_CHANNELS, custom_names))

    if not automod.get("log_channel_id") and "mod-logs" not in dismissed:
        missing.append(_entry("mod-logs", CORE_CHANNELS, custom_names))

    if not bump_cfg.get("bump_channel_id") and "bump" not in dismissed:
        missing.append(_entry("bump", CORE_CHANNELS, custom_names))

    if not leveling_cfg.get("announce_channel_id") and "level-ups" not in dismissed:
        missing.append(_entry("level-ups", CORE_CHANNELS, custom_names))

    # --- soft (keyword-heuristic) ---
    existing_names = [plain_name(c.name) for c in guild.text_channels]
    soft_ids = suggestions.get("soft_channel_ids") or {}
    for key, (base_name, desc, seed, keywords) in SOFT_CHANNELS.items():
        if key in dismissed:
            continue
        # A channel we created ourselves (possibly with a fancy-font name that
        # no keyword can match) counts as present for as long as it exists.
        _cid = soft_ids.get(key)
        if _cid and guild.get_channel(int(_cid)) is not None:
            continue
        if any(kw in name for name in existing_names for kw in keywords):
            continue  # something that looks like this already exists
        missing.append(_entry(key, SOFT_CHANNELS, custom_names))

    return missing


def _entry(key: str, table: dict, custom_names: dict) -> dict:
    base_name, desc, *_rest = table[key]
    return {"key": key, "name": _default_name(key, custom_names), "description": desc}


class RenameChannelModal(discord.ui.Modal, title="Rename suggested channel"):
    name = discord.ui.TextInput(label="Channel name", max_length=90)

    def __init__(self, guild_id: int, key: str, current_name: str, page: int = 0):
        super().__init__()
        self.guild_id = guild_id
        self.key = key
        self.page = page
        self.name.default = current_name

    async def on_submit(self, interaction: discord.Interaction):
        clone_id = _clone_id_of(interaction.client)
        new_name = str(self.name.value).strip()
        await db.set_custom_channel_name(self.guild_id, self.key, new_name, clone_id=clone_id)
        guild = interaction.client.get_guild(self.guild_id)
        missing = await scan_missing_channels(guild, clone_id) if guild else []
        # Stay on the same page the rename was triggered from — renaming
        # never changes which channels are missing or their order, so the
        # page that was on screen is still valid (SetupSuggestView clamps
        # it anyway if the count ever did shrink below it).
        view = SetupSuggestView(self.guild_id, missing, page=self.page)
        if is_v2_message(interaction):
            layout = build_suggestions_layout_view(guild, missing, page=self.page)
            await interaction.response.edit_message(view=layout)
        else:
            embed = build_suggestions_embed(guild, missing, page=self.page)
            await interaction.response.edit_message(embed=embed, view=view)


# ── font panel for a suggested channel's name ──────────────────────────────
# Opened from the "Rename a suggestion" dropdown. Replaces the suggestions
# message in place (so it works on the ephemeral /setup channels message AND
# the join-DM one), lets the owner pick a font + brackets with a live preview,
# and Save stores the styled name exactly like the old rename box did.

_PANEL_PLAIN = "__plain__"


async def _resolve_member(interaction: discord.Interaction, guild_id: int):
    """(guild, member) for whoever tapped. In a server both come off the
    interaction; from the join DM there is no interaction.guild, so look the
    guild up by id and fetch the user as a member of it. Kept local so this
    file doesn't depend on any other cog's private helpers."""
    guild = interaction.guild or interaction.client.get_guild(guild_id)
    if guild is None:
        return None, None
    if interaction.guild is not None and isinstance(interaction.user, discord.Member):
        return guild, interaction.user
    member = guild.get_member(interaction.user.id)
    if member is None:
        try:
            member = await guild.fetch_member(interaction.user.id)
        except discord.HTTPException:
            member = None
    return guild, member



_BRACKET_CHARS = set("".join(l + r for _k, (_lbl, l, r) in BRACKETS.items()))


def _split_prefix(name: str) -> tuple:
    """('👋', '【𝐰𝐞𝐥𝐜𝐨𝐦𝐞】') — the leading emoji, kept OUTSIDE any brackets, and the
    rest. Without this, restyling a default name wrapped the emoji inside the
    new brackets and left the old left bracket behind (【👋【𝐰𝐞𝐥𝐜𝐨𝐦𝐞】)."""
    name = name or ""
    i = 0
    while i < len(name):
        ch = name[i]
        if to_plain(ch).isalnum() or ch in _BRACKET_CHARS or ch in "-_ ":
            break
        i += 1
    return name[:i], name[i:]


def _styled_name(base: str, font, bracket) -> str:
    """base -> final channel name. font/bracket None = leave that part alone.
    Text channels can't hold spaces or capitals, so this hyphenates/lowercases
    the same way /style does. A leading emoji stays in front of the brackets."""
    if font is None and bracket is None:
        return base
    prefix, rest = _split_prefix(base)
    if font is None:
        plain = strip_brackets(to_plain(rest))
        return (prefix + wrap_brackets("-".join(plain.lower().split()), bracket))[:100]
    return (prefix + channel_name(rest, font, True, bracket))[:100]


class _NamePanel:
    """State for one open font panel (in memory, expires after 10 minutes)."""

    def __init__(self, guild_id: int, key: str, page: int, base: str, v2: bool, names: dict | None = None):
        self.guild_id, self.key, self.page, self.v2 = guild_id, key, page, v2
        # names = {key: current name} when this panel styles EVERY suggestion
        # at once ("Style all"); None for the one-channel panel.
        self.names = names
        self.base = base
        self.font = None
        self.bracket = None
        self.premium_active = False
        self.can_manage = False
        self.status = None

    @property
    def result(self) -> str:
        return _styled_name(self.base, self.font, self.bracket)

    def block(self):
        from discord_bot.cogs.style import _premium_block_message
        return _premium_block_message(self.font, self.bracket, self.premium_active, self.can_manage)

    # -- rendering --------------------------------------------------------
    def _lines(self) -> list:
        font = f"**{STYLES[self.font][0]}**" if self.font else "*plain*"
        brackets = BRACKETS[self.bracket][0] if self.bracket else "*none*"
        if self.names:
            lines = [
                f"\U0001f3a8 **Style all {len(self.names)} suggested channels**",
                "Pick a font and brackets once \u2014 every name below is restyled together "
                "(each channel keeps its own emoji).",
                f"**Font** \u2014 {font}",
                f"**Brackets** \u2014 {brackets}",
                "", "**Preview**",
                *[_styled_name(n, self.font, self.bracket) for n in self.names.values()],
            ]
        else:
            lines = [
                "\U0001f524 **Style this channel name**",
                f"**Name** \u2014 `{self.base.replace(chr(96), chr(39))}`",
                f"**Font** \u2014 {font}",
                f"**Brackets** \u2014 {brackets}",
                "", "**Preview**", self.result,
            ]
        lock = self.block()
        if lock and not self.status:
            lines += ["", lock]
        if self.status:
            lines += ["", self.status]
        return lines

    def _items(self):
        """Rows of fresh components: [[select], [select], [select], [select], [buttons]]"""
        buttons = [_PSaveButton(self), _PBackButton(self)] if self.names else [
            _PEditButton(self), _PSaveButton(self), _PBackButton(self)]
        return [
            [_PFontSelect(self, False)], [_PFontSelect(self, True)],
            [_PBracketSelect(self, False)], [_PBracketSelect(self, True)],
            buttons,
        ]

    def build(self):
        """(kwargs for edit_message) matching the message type we're editing."""
        if self.v2:
            view = discord.ui.LayoutView(timeout=600)
            box = discord.ui.Container(accent_colour=discord.Color.blurple())
            box.add_item(discord.ui.TextDisplay("\n".join(self._lines())))
            box.add_item(discord.ui.Separator())
            for row in self._items():
                box.add_item(discord.ui.ActionRow(*row))
            view.add_item(box)
            return {"view": view}
        view = discord.ui.View(timeout=600)
        for r, row in enumerate(self._items()):
            for item in row:
                item.row = r
                view.add_item(item)
        embed = discord.Embed(description="\n".join(self._lines()), color=discord.Color.blurple())
        embed.set_footer(text="Discord channel names are lowercase \u2014 your text is lowercased for you.")
        return {"embed": embed, "view": view}

    async def refresh(self, interaction: discord.Interaction):
        await interaction.response.edit_message(**self.build())


class _PFontSelect(discord.ui.Select):
    def __init__(self, panel: _NamePanel, premium: bool):
        choices = premium_style_choices() if premium else free_style_choices()
        options = []
        if not premium:
            options.append(discord.SelectOption(
                label="Plain (no font)", value=_PANEL_PLAIN, default=(panel.font is None)))
        for key, label, _sample in choices:
            options.append(discord.SelectOption(
                label=label, value=key, default=(key == panel.font),
                description=(_styled_name(panel.base, key, panel.bracket)[:100] or None),
            ))
        super().__init__(
            placeholder="1 \u00b7 \U0001f48e Premium fonts\u2026" if premium else "1 \u00b7 Pick a font\u2026",
            options=options[:25], min_values=1, max_values=1,
        )
        self.panel = panel

    async def callback(self, interaction: discord.Interaction):
        v = self.values[0]
        self.panel.font = None if v == _PANEL_PLAIN else v
        self.panel.status = None
        await self.panel.refresh(interaction)


class _PBracketSelect(discord.ui.Select):
    def __init__(self, panel: _NamePanel, premium: bool):
        choices = premium_bracket_choices() if premium else free_bracket_choices()
        options = [
            discord.SelectOption(
                label=label, value=key, default=(key == (panel.bracket or "none")),
                description=(_styled_name(panel.base, panel.font or "bold", key)[:100] or None),
            )
            for key, label in choices
        ]
        super().__init__(
            placeholder="2 \u00b7 \U0001f48e Premium brackets\u2026" if premium else "2 \u00b7 Brackets (optional)\u2026",
            options=options[:25], min_values=1, max_values=1,
        )
        self.panel = panel

    async def callback(self, interaction: discord.Interaction):
        v = self.values[0]
        self.panel.bracket = None if v == "none" else v
        self.panel.status = None
        await self.panel.refresh(interaction)


class _PNameModal(discord.ui.Modal, title="Edit the channel name"):
    def __init__(self, panel: _NamePanel):
        super().__init__()
        self.panel = panel
        self.field = discord.ui.TextInput(
            label="Base name (plain text)", max_length=80, required=True,
            default=plain_name(panel.base)[:80],
        )
        self.add_item(self.field)

    async def on_submit(self, interaction: discord.Interaction):
        typed = str(self.field.value).strip()
        if typed:
            self.panel.base = _split_prefix(self.panel.base)[0] + typed
        self.panel.status = None
        await self.panel.refresh(interaction)


class _PEditButton(discord.ui.Button):
    def __init__(self, panel):
        super().__init__(label="Edit name", emoji="\u270f\ufe0f", style=discord.ButtonStyle.secondary)
        self.panel = panel

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(_PNameModal(self.panel))


class _PSaveButton(discord.ui.Button):
    def __init__(self, panel):
        super().__init__(label="Save", emoji="\u2705", style=discord.ButtonStyle.success,
                         disabled=panel.block() is not None)
        self.panel = panel

    async def callback(self, interaction: discord.Interaction):
        panel = self.panel
        from discord_bot.cogs.style import _premium_flags
        clone_id = _clone_id_of(interaction.client)
        guild, member = await _resolve_member(interaction, panel.guild_id)
        if guild is not None and member is not None:
            # Re-check at click time: premium status may have changed.
            panel.premium_active, panel.can_manage = await _premium_flags(member, guild, clone_id)
        if panel.block():
            panel.status = None
            await panel.refresh(interaction)
            return
        if panel.names:
            for k, n in panel.names.items():
                styled = _styled_name(n, panel.font, panel.bracket).strip()[:90]
                if styled:
                    await db.set_custom_channel_name(panel.guild_id, k, styled, clone_id=clone_id)
            await _show_suggestions(
                interaction, panel.guild_id, panel.page,
                f"\n\n\u2705 Styled {len(panel.names)} channel names \u2014 tap Create when you're ready.")
            return
        name = panel.result.strip()[:90]
        if not name:
            panel.status = "\u274c Nothing to save."
            await panel.refresh(interaction)
            return
        await db.set_custom_channel_name(panel.guild_id, panel.key, name, clone_id=clone_id)
        await _show_suggestions(interaction, panel.guild_id, panel.page, f"\n\n\u2705 Saved name: **{name}**")


class _PBackButton(discord.ui.Button):
    def __init__(self, panel):
        super().__init__(label="Back", emoji="\u2b05\ufe0f", style=discord.ButtonStyle.secondary)
        self.panel = panel

    async def callback(self, interaction: discord.Interaction):
        await _show_suggestions(interaction, self.panel.guild_id, self.panel.page, "")


async def _show_suggestions(interaction: discord.Interaction, guild_id: int, page: int, note: str):
    """Put the suggestions list back on the message the panel replaced."""
    clone_id = _clone_id_of(interaction.client)
    guild = interaction.client.get_guild(guild_id)
    missing = await scan_missing_channels(guild, clone_id) if guild else []
    panel_v2 = bool(getattr(interaction.message, "flags", None) and interaction.message.flags.components_v2)
    if panel_v2:
        await interaction.response.edit_message(
            view=build_suggestions_layout_view(guild, missing, page=page, extra_note=note))
    else:
        embed = build_suggestions_embed(guild, missing, page=page)
        if note:
            embed.description = (embed.description or "") + note
        await interaction.response.edit_message(embed=embed, view=SetupSuggestView(guild_id, missing, page=page))


async def open_style_all_panel(interaction: discord.Interaction, guild_id: int, page: int):
    """Same font + bracket pickers as the one-channel panel, applied to every
    pending suggestion at once (uses the shared modules.text_styles engine,
    the same one /style and the join-DM font wizard run on)."""
    from discord_bot.cogs.style import _premium_flags
    clone_id = _clone_id_of(interaction.client)
    guild = interaction.client.get_guild(guild_id)
    missing = await scan_missing_channels(guild, clone_id) if guild else []
    if not missing:
        await interaction.response.send_message(
            "Nothing left to style \u2014 every suggestion is already handled.", ephemeral=True)
        return
    panel = _NamePanel(guild_id, "*", page, missing[0]["name"], v2=is_v2_message(interaction),
                       names={m["key"]: m["name"] for m in missing})
    g, member = await _resolve_member(interaction, guild_id)
    if g is not None and member is not None:
        panel.premium_active, panel.can_manage = await _premium_flags(member, g, clone_id)
    await interaction.response.edit_message(**panel.build())


async def open_name_panel(interaction: discord.Interaction, guild_id: int, key: str, page: int, current_name: str):
    from discord_bot.cogs.style import _premium_flags
    panel = _NamePanel(guild_id, key, page, current_name, v2=is_v2_message(interaction))
    guild, member = await _resolve_member(interaction, panel.guild_id)
    if guild is not None and member is not None:
        panel.premium_active, panel.can_manage = await _premium_flags(
            member, guild, _clone_id_of(interaction.client))
    await interaction.response.edit_message(**panel.build())


class SetupSuggestView(discord.ui.View):
    """One row of Create/Skip per missing channel, laid out the same way
    the quickstart wizard's per-feature rows are: each channel gets its
    own row (row=idx) so its buttons sit directly under that channel's
    field in the embed, instead of the old flat auto-flow layout where
    buttons packed 5-to-a-row with no relationship to field order.
    Paginated at PAGE_SIZE per screen for the same reason quickstart is —
    Discord caps a view at 5 rows, and each channel here now costs a
    whole row instead of sharing one, so more than a handful of missing
    channels no longer fits on one screen."""

    PAGE_SIZE = 3

    def __init__(self, guild_id: int, missing: list[dict], page: int = 0):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.missing = missing
        total_pages = max(1, -(-len(missing) // self.PAGE_SIZE)) if missing else 1
        self.page = max(0, min(page, total_pages - 1))
        self.total_pages = total_pages
        page_entries = missing[self.page * self.PAGE_SIZE:(self.page + 1) * self.PAGE_SIZE]

        for idx, entry in enumerate(page_entries):
            key = entry["key"]
            self.add_item(discord.ui.Button(
                label=_create_button_label(entry['name']), style=discord.ButtonStyle.success,
                custom_id=f"setupch_create:{guild_id}:{key}:{self.page}", row=idx,
            ))
            self.add_item(discord.ui.Button(
                label="Skip", style=discord.ButtonStyle.secondary,
                custom_id=f"setupch_skip:{guild_id}:{key}:{self.page}", row=idx,
            ))

        # Rename select scoped to just this page's entries (row 3) — a
        # dropdown listing channels not currently visible would be
        # confusing, and keeping it 1-to-1 with what's on screen avoids
        # that entirely.
        if page_entries:
            select = discord.ui.Select(
                placeholder="🔤 Style / rename a suggestion on this page...",
                custom_id=f"setupch_renamesel:{guild_id}:{self.page}",
                options=[
                    discord.SelectOption(label=entry["name"][:100], value=entry["key"])
                    for entry in page_entries
                ],
                row=3,
            )
            self.add_item(select)

        # Row 4: Create All (every missing channel, not just this page)
        # plus Prev/Next — same bottom-row pattern as the quickstart
        # wizard's nav buttons, so the two flows read as one design.
        if missing:
            self.add_item(discord.ui.Button(
                label="✅ Create All Suggested", style=discord.ButtonStyle.primary,
                custom_id=f"setupch_createall:{guild_id}:{self.page}", row=4,
            ))
        if missing:
            self.add_item(discord.ui.Button(
                label="🎨 Style all", style=discord.ButtonStyle.secondary,
                custom_id=f"setupch_styleall:{guild_id}:{self.page}", row=4,
            ))
        if self.page > 0:
            self.add_item(discord.ui.Button(
                label="← Prev", style=discord.ButtonStyle.secondary,
                custom_id=f"setupch_page:{guild_id}:prev:{self.page}", row=4,
            ))
        if self.page < total_pages - 1:
            self.add_item(discord.ui.Button(
                label="Next →", style=discord.ButtonStyle.secondary,
                custom_id=f"setupch_page:{guild_id}:next:{self.page}", row=4,
            ))


def build_suggestions_embed(guild: discord.Guild, missing: list[dict], page: int = 0) -> discord.Embed:
    if not missing:
        return discord.Embed(
            title="✅ Nothing to suggest",
            description="Every suggested channel already exists or was previously skipped.",
            color=discord.Color.green(),
        )
    page_size = SetupSuggestView.PAGE_SIZE
    total_pages = max(1, -(-len(missing) // page_size))
    page = max(0, min(page, total_pages - 1))
    page_entries = missing[page * page_size:(page + 1) * page_size]

    embed = discord.Embed(
        title=f"📋 Suggested channels for {guild.name}",
        description="Create any of these individually, or use **Create All Suggested** for the fast path. "
                    "Pick a font or edit the name before creating with the style dropdown below, "
            "or tap **🎨 Style all** to restyle every name at once.",
        color=discord.Color.blurple(),
    )
    for entry in page_entries:
        embed.add_field(name=entry["name"], value=entry["description"], inline=False)
    footer = "Run /setup channels anytime to come back here."
    if total_pages > 1:
        footer = f"Page {page + 1}/{total_pages} — {footer}"
    embed.set_footer(text=footer)
    return embed


def is_v2_message(interaction: discord.Interaction) -> bool:
    """True if the message this interaction's component lives on was sent
    as Components V2 — e.g. the join-DM quickstart wizard's "Create
    suggested channels" button. Discord permanently forbids embeds on a
    V2 message, even on edit, so every place that re-renders
    build_suggestions_embed/SetupSuggestView on top of a live interaction
    has to check this and switch to build_suggestions_layout_view
    instead, or the edit gets rejected outright. The classic
    embed+SetupSuggestView path stays the default for /setup channels,
    which always sends a fresh, non-V2 message."""
    message = getattr(interaction, "message", None)
    return bool(message and message.flags.components_v2)


class SetupSuggestLayoutView(discord.ui.LayoutView):
    """Components V2 twin of build_suggestions_embed + SetupSuggestView,
    for rendering this same picker on top of an already-V2 message (the
    join-DM's "Create suggested channels" button) where embeds are
    permanently disallowed. Reuses the EXACT same custom_id scheme
    (setupch_create:/setupch_skip:/setupch_renamesel:/setupch_createall:/
    setupch_page:) as the classic view, so SetupChannelsCog.on_interaction
    — which dispatches by parsing custom_id strings directly rather than
    via discord.ui.DynamicItem — handles clicks from this layout with no
    changes at all. That listener-based dispatch (not tied to any
    specific view/message being cached in memory) is also what already
    makes this restart-proof, same as the classic view."""

    PAGE_SIZE = SetupSuggestView.PAGE_SIZE

    def __init__(self, guild_id: int, guild_name: str, missing: list[dict], page: int = 0, extra_note: str = ""):
        super().__init__(timeout=None)
        container = discord.ui.Container(accent_colour=discord.Color.blurple())

        if not missing:
            container.add_item(discord.ui.TextDisplay("## ✅ Nothing to suggest\nEvery suggested channel already exists or was previously skipped."))
            if extra_note:
                container.add_item(discord.ui.TextDisplay(extra_note.strip()))
            container.add_item(discord.ui.ActionRow(discord.ui.Button(
                label="⬅️ Back to menu", style=discord.ButtonStyle.secondary,
                custom_id=f"setupch_back:{guild_id}",
            )))
            self.add_item(container)
            return

        total_pages = max(1, -(-len(missing) // self.PAGE_SIZE))
        page = max(0, min(page, total_pages - 1))
        page_entries = missing[page * self.PAGE_SIZE:(page + 1) * self.PAGE_SIZE]

        intro = (
            f"## 📋 Suggested channels for {guild_name}\n"
            "Create any of these individually, or use **Create All Suggested** for the fast path. "
            "Pick a font or edit the name before creating with the style dropdown below."
        )
        if extra_note:
            intro += "\n" + extra_note.strip()
        container.add_item(discord.ui.TextDisplay(intro))

        for entry in page_entries:
            key = entry["key"]
            container.add_item(discord.ui.TextDisplay(f"**{entry['name']}**\n{entry['description']}"))
            container.add_item(discord.ui.ActionRow(
                discord.ui.Button(
                    label=_create_button_label(entry['name']), style=discord.ButtonStyle.success,
                    custom_id=f"setupch_create:{guild_id}:{key}:{page}",
                ),
                discord.ui.Button(
                    label="Skip", style=discord.ButtonStyle.secondary,
                    custom_id=f"setupch_skip:{guild_id}:{key}:{page}",
                ),
            ))

        if page_entries:
            container.add_item(discord.ui.ActionRow(discord.ui.Select(
                placeholder="🔤 Style / rename a suggestion on this page...",
                custom_id=f"setupch_renamesel:{guild_id}:{page}",
                options=[
                    discord.SelectOption(label=entry["name"][:100], value=entry["key"])
                    for entry in page_entries
                ],
            )))

        footer = "Run /setup channels anytime to come back here."
        if total_pages > 1:
            footer = f"Page {page + 1}/{total_pages} — {footer}"
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay(f"-# {footer}"))

        bottom = []
        bottom.append(discord.ui.Button(
            label="⬅️ Back to menu", style=discord.ButtonStyle.secondary,
            custom_id=f"setupch_back:{guild_id}",
        ))
        bottom.append(discord.ui.Button(
            label="✅ Create All Suggested", style=discord.ButtonStyle.primary,
            custom_id=f"setupch_createall:{guild_id}:{page}",
        ))
        bottom.append(discord.ui.Button(
            label="🎨 Style all", style=discord.ButtonStyle.secondary,
            custom_id=f"setupch_styleall:{guild_id}:{page}",
        ))
        if page > 0:
            bottom.append(discord.ui.Button(
                label="← Prev", style=discord.ButtonStyle.secondary,
                custom_id=f"setupch_page:{guild_id}:prev:{page}",
            ))
        if page < total_pages - 1:
            bottom.append(discord.ui.Button(
                label="Next →", style=discord.ButtonStyle.secondary,
                custom_id=f"setupch_page:{guild_id}:next:{page}",
            ))
        container.add_item(discord.ui.ActionRow(*bottom))

        self.add_item(container)


def build_suggestions_layout_view(guild: discord.Guild, missing: list[dict], page: int = 0, extra_note: str = "") -> SetupSuggestLayoutView:
    return SetupSuggestLayoutView(guild.id, guild.name, missing, page=page, extra_note=extra_note)


class SetupChannelsCog(GuildOnlyCog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def _get_or_create_category(self, guild: discord.Guild, clone_id: int | None) -> discord.CategoryChannel:
        """Created once and reused — every subsequent /setup channels run
        (or join-DM approval) drops new channels into the same category
        instead of creating a duplicate '📋 Server Setup' each time."""
        suggestions = await db.get_setup_suggestions(guild.id, clone_id)
        category_id = suggestions.get("category_id")
        if category_id:
            existing = guild.get_channel(int(category_id))
            if isinstance(existing, discord.CategoryChannel):
                return existing
        for cat in guild.categories:
            if _plain_name(cat.name) == _plain_name(SETUP_CATEGORY_NAME):
                await db.set_setup_suggestions(guild.id, clone_id=clone_id, category_id=cat.id)
                return cat
        category = await guild.create_category(SETUP_CATEGORY_NAME, reason="Server Setup wizard")
        await db.set_setup_suggestions(guild.id, clone_id=clone_id, category_id=category.id)
        return category

    async def _create_one(self, guild: discord.Guild, clone_id: int | None, key: str, invoker_id: int | None = None) -> discord.TextChannel | None:
        """Re-verifies the channel is still missing right before creating
        (in case /setup channels was run twice, or the owner made a
        matching channel manually in between), then creates it, seeds it,
        and writes the owning feature's config."""
        missing_now = await scan_missing_channels(guild, clone_id)
        entry = next((m for m in missing_now if m["key"] == key), None)
        if entry is None:
            return None  # no longer missing — already handled

        category = await self._get_or_create_category(guild, clone_id)
        if key == "bump":
            from discord_bot.cogs.bump_setup import get_or_create_bump_channel
            # One shared, locked get-or-create: reuses a bump channel the server
            # already has (whatever it's called — this wizard's own name is
            # styled (e.g. 📣【𝐛𝐮𝐦𝐩】), which /bumpsetup and the restore pass used to not
            # recognise, so each made a second one) and is bot-posting-only when
            # it does have to create one. It seeds nothing itself; _seed_channel below does.
            channel, created = await get_or_create_bump_channel(
                guild, None, name=entry["name"], category=category, reason="Server Setup wizard", intro=False,
            )
        else:
            channel = await guild.create_text_channel(entry["name"], category=category, reason="Server Setup wizard")
            created = True
        if created:
            await self._seed_channel(guild, channel, key)
            await self._write_config(guild, clone_id, key, channel)
        else:
            # Adopted an existing bump channel — point the config at it, but
            # don't mark it channel_auto_created (we didn't make it).
            await db.bump_set_guild_config(
                guild_id=guild.id, clone_id=clone_id, configured_by=guild.owner_id or 0,
                bump_channel_id=channel.id, receives_bumps=True,
            )
        if key == "bump":
            # No dead end: right after creating #bump, post the /bumpsetup wizard
            # in it (also heals the listing + this bot's permissions), so the
            # owner can finish and Save — which sends the first bump.
            try:
                from discord_bot.cogs.bump_setup import post_setup_wizard
                await post_setup_wizard(
                    self.bot, guild, clone_id, channel,
                    invoker_id=invoker_id or guild.owner_id or 0, created=True,
                )
            except Exception:
                logger.exception("setup_channels: couldn't post bump wizard for guild %s", guild.id)
        return channel

    async def _seed_channel(self, guild: discord.Guild, channel: discord.TextChannel, key: str):
        if key == "welcome":
            template = "Welcome {member} to {guild}! You are member #{count}."
            preview = template.replace("{guild}", guild.name).replace("{count}", "1").replace("{member}", "@you")
            try:
                await channel.send(f"👋 Welcome cards will appear here, e.g.:\n\n{preview}")
            except discord.Forbidden:
                pass
            return
        if key == "bump":
            try:
                await channel.send(
                    "📣 Bump reminders and your server's listing will appear here. "
                    "Finish the setup in the wizard below, or run `/bumpsetup` anytime."
                )
            except discord.Forbidden:
                pass
            return
        table = CORE_CHANNELS.get(key, SOFT_CHANNELS.get(key))
        seed = table[2] if key in CORE_CHANNELS else table[2]
        if seed:
            try:
                await channel.send(seed)
            except discord.Forbidden:
                pass

    async def _write_config(self, guild: discord.Guild, clone_id: int | None, key: str, channel: discord.TextChannel):
        """Points the owning feature at the newly-created channel so it
        starts using it immediately, following the same
        *_channel_auto_created = TRUE pattern discord_automod_config
        already uses for its log channel."""
        if key == "welcome":
            await db.set_welcome_config(guild.id, clone_id=clone_id, channel_id=channel.id, channel_auto_created=True)
        elif key == "mod-logs":
            await db.set_automod_config(guild.id, clone_id=clone_id, log_channel_id=channel.id, log_channel_auto_created=True)
        elif key == "bump":
            await db.bump_set_guild_config(
                guild_id=guild.id, clone_id=clone_id, configured_by=guild.owner_id or 0,
                bump_channel_id=channel.id, receives_bumps=True,
            )
            # bump_set_guild_config's column list (see its own signature)
            # doesn't include channel_auto_created — write it directly so a
            # re-run of this flow still recognizes this channel as ours.
            pool = await get_pool()
            async with pool.acquire() as conn:
                await conn.execute(
                    "UPDATE bump_guild_config SET channel_auto_created = TRUE "
                    "WHERE guild_id = $1 AND COALESCE(clone_id, -1) = COALESCE($2, -1)",
                    guild.id, clone_id,
                )
        elif key == "level-ups":
            await db.set_leveling_config(guild.id, clone_id=clone_id, announce_channel_id=channel.id, announce_auto_created=True)
        else:
            # soft channel — record the created channel id so re-scans
            # don't try to create a second one, since these have no
            # feature-config table of their own to check.
            suggestions = await db.get_setup_suggestions(guild.id, clone_id)
            soft_ids = dict(suggestions["soft_channel_ids"])
            soft_ids[key] = channel.id
            await db.set_setup_suggestions(guild.id, clone_id=clone_id, soft_channel_ids=soft_ids)

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        if interaction.type != discord.InteractionType.component:
            return
        custom_id = interaction.data.get("custom_id", "")
        # NOTE: every branch below now carries a trailing :<page> segment
        # (added alongside the row-per-channel/pagination rework) so the
        # rebuilt view/embed can stay on the page the tap happened on
        # instead of always snapping back to page 0. int(...) on that
        # segment is safe without a try/except: these custom_ids are only
        # ever produced by SetupSuggestView/_PageNavButton-style buttons
        # this cog builds itself, never user-suppliable input.
        if custom_id.startswith("setupch_renamesel:"):
            _, guild_id_s, page_s = custom_id.split(":", 2)
            guild_id, page = int(guild_id_s), int(page_s)
            key = interaction.data.get("values", [None])[0]
            if key is None:
                return
            clone_id = _clone_id_of(interaction.client)
            guild = interaction.client.get_guild(guild_id)
            missing = await scan_missing_channels(guild, clone_id) if guild else []
            entry = next((m for m in missing if m["key"] == key), None)
            if entry is None:
                await interaction.response.send_message(
                    "That suggestion was already handled — refresh with `/setup channels`.", ephemeral=True
                )
                return
            await open_name_panel(interaction, guild_id, key, page, entry["name"])
        elif custom_id.startswith("setupch_styleall:"):
            _, guild_id_s, page_s = custom_id.split(":", 2)
            await open_style_all_panel(interaction, int(guild_id_s), int(page_s))
        elif custom_id.startswith("setupch_create:"):
            _, guild_id_s, key, page_s = custom_id.split(":", 3)
            guild_id, page = int(guild_id_s), int(page_s)
            guild = interaction.client.get_guild(guild_id)
            if guild is None:
                await interaction.response.send_message("I'm not in that server anymore.", ephemeral=True)
                return
            await interaction.response.defer()
            clone_id = _clone_id_of(interaction.client)
            try:
                channel = await self._create_one(guild, clone_id, key, invoker_id=interaction.user.id)
            except discord.Forbidden:
                await interaction.followup.send(
                    "I don't have permission to create channels here — grant me **Manage Channels** and try again.",
                    ephemeral=True,
                )
                return
            except discord.HTTPException:
                logger.exception("setup_channels create failed for guild %s key %s", guild_id, key)
                await interaction.followup.send(
                    "Something went wrong creating that channel — try again in a moment.", ephemeral=True,
                )
                return
            missing = await scan_missing_channels(guild, clone_id)
            note = f"\n\n✅ Created {channel.mention}." if channel else "\n\nThat one was already handled."
            if is_v2_message(interaction):
                layout = build_suggestions_layout_view(guild, missing, page=page, extra_note=note)
                await interaction.edit_original_response(view=layout)
            else:
                embed = build_suggestions_embed(guild, missing, page=page)
                view = SetupSuggestView(guild_id, missing, page=page)
                embed.description = (embed.description or "") + note
                await interaction.edit_original_response(embed=embed, view=view)
        elif custom_id.startswith("setupch_createall:"):
            _, guild_id_s, page_s = custom_id.split(":", 2)
            guild_id, page = int(guild_id_s), int(page_s)
            guild = interaction.client.get_guild(guild_id)
            if guild is None:
                await interaction.response.send_message("I'm not in that server anymore.", ephemeral=True)
                return
            await interaction.response.defer()
            clone_id = _clone_id_of(interaction.client)
            created = []
            # Re-scan before EACH creation (not once up front) — creating
            # #welcome could itself never affect #bump's missing-ness, but
            # this keeps every single create consistent with _create_one's
            # own re-verify contract rather than assuming the list is
            # still accurate after the first change.
            missing = await scan_missing_channels(guild, clone_id)
            forbidden = False
            for entry in missing:
                try:
                    channel = await self._create_one(guild, clone_id, entry["key"], invoker_id=interaction.user.id)
                except discord.Forbidden:
                    # Stop here rather than raising past the loop: whatever
                    # got created before the permission ran out (or was
                    # revoked mid-loop) stays created, and the note below
                    # still reports it instead of losing that progress to
                    # an unhandled exception.
                    forbidden = True
                    break
                except discord.HTTPException:
                    logger.exception("setup_channels create-all failed for guild %s key %s", guild_id, entry["key"])
                    continue
                if channel:
                    created.append(channel)
            remaining = await scan_missing_channels(guild, clone_id)
            note = ""
            if created:
                mentions = ", ".join(c.mention for c in created)
                note = f"\n\n✅ Created: {mentions}"
            if forbidden:
                note += ("\n\n⚠️ Stopped early — I don't have permission to create channels here. "
                         "Grant me **Manage Channels** and run this again for the rest.")
            # Create All just wiped out everything on every page, so page 0
            # is the only page guaranteed to still make sense — clamping to
            # the tapped page here could easily land past the new (much
            # shorter, maybe empty) list. build_suggestions_embed/
            # build_suggestions_layout_view and their views both clamp
            # internally too, but starting from 0 avoids landing on an
            # oddly-numbered empty-looking page.
            if is_v2_message(interaction):
                layout = build_suggestions_layout_view(guild, remaining, page=0, extra_note=note)
                await interaction.edit_original_response(view=layout)
            else:
                embed = build_suggestions_embed(guild, remaining, page=0)
                view = SetupSuggestView(guild_id, remaining, page=0)
                if note:
                    embed.description = (embed.description or "") + note
                await interaction.edit_original_response(embed=embed, view=view)
        elif custom_id.startswith("setupch_skip:"):
            _, guild_id_s, key, page_s = custom_id.split(":", 3)
            guild_id, page = int(guild_id_s), int(page_s)
            clone_id = _clone_id_of(interaction.client)
            await db.dismiss_setup_suggestion(guild_id, key, clone_id=clone_id)
            guild = interaction.client.get_guild(guild_id)
            missing = await scan_missing_channels(guild, clone_id) if guild else []
            if is_v2_message(interaction):
                layout = build_suggestions_layout_view(guild, missing, page=page)
                await interaction.response.edit_message(view=layout)
            else:
                embed = build_suggestions_embed(guild, missing, page=page)
                view = SetupSuggestView(guild_id, missing, page=page)
                await interaction.response.edit_message(embed=embed, view=view)
        elif custom_id.startswith("setupch_page:"):
            _, guild_id_s, direction, page_s = custom_id.split(":", 3)
            guild_id, current_page = int(guild_id_s), int(page_s)
            target_page = current_page + 1 if direction == "next" else current_page - 1
            clone_id = _clone_id_of(interaction.client)
            guild = interaction.client.get_guild(guild_id)
            if guild is None:
                await interaction.response.send_message("I'm not in that server anymore.", ephemeral=True)
                return
            missing = await scan_missing_channels(guild, clone_id)
            if is_v2_message(interaction):
                layout = build_suggestions_layout_view(guild, missing, page=target_page)
                await interaction.response.edit_message(view=layout)
            else:
                embed = build_suggestions_embed(guild, missing, page=target_page)
                view = SetupSuggestView(guild_id, missing, page=target_page)
                await interaction.response.edit_message(embed=embed, view=view)
        elif custom_id.startswith("setupch_back:"):
            # Only ever reached from the join-DM's V2 "Create suggested
            # channels" screen (is_v2_message gates every other branch's
            # layout-vs-classic choice above; this button only exists on
            # the layout view in the first place — see
            # SetupSuggestLayoutView) — restores the original quickstart
            # list on that same message, same rebuild the welcome
            # sub-screen's own Back button (_WelcomeBackButton) uses.
            _, guild_id_s = custom_id.split(":", 1)
            guild_id = int(guild_id_s)
            clone_id = _clone_id_of(interaction.client)
            from discord_bot.cogs._views_join_dm import (
                FEATURE_TOGGLES, _enabled_feature_keys, _build_main_join_dm_parts, build_join_dm_view,
            )
            await interaction.response.defer()
            all_feature_keys = list(FEATURE_TOGGLES.keys())
            enabled = await _enabled_feature_keys(guild_id, clone_id)
            intro, title, notices = await _build_main_join_dm_parts(interaction.client, guild_id, clone_id)
            main_view = build_join_dm_view(
                guild_id, clone_id=clone_id, feature_keys=all_feature_keys,
                intro=intro, title=title, notices=notices, enabled_keys=enabled,
            )
            await interaction.edit_original_response(view=main_view, attachments=[])

    group = app_commands.guild_only()(app_commands.Group(name="setup", description="Server setup helpers"))

    @group.command(name="channels", description="Suggest and create commonly-useful channels for this server")
    async def channels_cmd(self, interaction: discord.Interaction):
        if not _require_perm(interaction, "manage_channels"):
            await _deny(interaction, "Manage Channels")
            return
        clone_id = _clone_id_of(interaction.client)
        await interaction.response.defer(ephemeral=True)
        missing = await scan_missing_channels(interaction.guild, clone_id)
        embed = build_suggestions_embed(interaction.guild, missing)
        if missing:
            view = SetupSuggestView(interaction.guild_id, missing)
            await interaction.followup.send(embed=embed, view=view, ephemeral=True)
        else:
            await interaction.followup.send(embed=embed, ephemeral=True)

    @group.command(name="reportchannel", description="Choose which channel gets 'report this server' submissions")
    @app_commands.describe(channel="The channel reports should be forwarded into")
    async def reportchannel_cmd(self, interaction: discord.Interaction, channel: discord.TextChannel):
        # Run in-server rather than via DM: Discord's ChannelSelect (used
        # by the one-time DM picker in _views_report_channel_picker.py)
        # only populates options from the guild the interaction happened
        # in — there's no guild in a DM, so that component can't list
        # anything there. A plain channel option on an in-server command
        # doesn't have that problem, since discord.py resolves it against
        # this guild's real channel list.
        if not _require_perm(interaction, "manage_guild"):
            await _deny(interaction, "Manage Server")
            return
        clone_id = _clone_id_of(interaction.client)
        try:
            await db.set_report_notify_channel(clone_id, interaction.guild_id, channel.id)
        except Exception:
            logger.exception("[setup] /setup reportchannel save failed")
            await interaction.response.send_message(
                "Something went wrong saving that — try again in a moment.", ephemeral=True,
            )
            return
        await interaction.response.send_message(
            f"✅ Reports will be forwarded to {channel.mention} from now on.", ephemeral=True,
        )

    @group.command(name="servers", description="Get your link to list this server in PRIME-BOT's public directory")
    @app_commands.describe(resend="Resend the one-time auto-listing DM offer (Agree/Deny), even if it was declined before")
    async def servers_cmd(self, interaction: discord.Interaction, resend: bool = False):
        if resend:
            if not _require_perm(interaction, "manage_guild"):
                await _deny(interaction, "Manage Server")
                return
            clone_id = _clone_id_of(interaction.client)
            await db.reset_auto_listing_offer(interaction.guild_id, clone_id)
            await interaction.response.defer(ephemeral=True)
            await offer_auto_listing(interaction.client, interaction.guild)
            await interaction.followup.send(
                "Resent — check your DMs (or this server's fallback channel if DMs are closed) "
                "for the Agree/Deny listing offer.",
                ephemeral=True,
            )
            return

        listing_cog = interaction.client.get_cog("ServerListingCog")
        if listing_cog is None:
            await interaction.response.send_message("Server listing module isn't loaded.", ephemeral=True)
            return
        await listing_cog.request_listing_link(interaction)

    @group.command(name="roastme", description="Request the bot roast someone — needs an admin's approval")
    async def roastme_cmd(self, interaction: discord.Interaction):
        roast_cog = interaction.client.get_cog("RoastCog")
        if roast_cog is None:
            await interaction.response.send_message("Roast module isn't loaded.", ephemeral=True)
            return
        await roast_cog.request_from_member(interaction)

    @group.command(name="roaststart", description="Manually start a roast battle right now (skips the wait for inactivity/random)")
    async def roaststart_cmd(self, interaction: discord.Interaction):
        roast_cog = interaction.client.get_cog("RoastCog")
        if roast_cog is None:
            await interaction.response.send_message("Roast module isn't loaded.", ephemeral=True)
            return
        await roast_cog.manual_trigger(interaction)

    @group.command(name="roast", description="Configure auto-roast triggers for this server")
    @app_commands.describe(
        inactivity_minutes="Minutes of silence before proposing a roast",
        random_chance_percent="Odds (0-100) of proposing a roast on each random check even if active",
        enabled="Turn auto-roast on/off for this server",
    )
    async def roast_cmd(self, interaction: discord.Interaction, inactivity_minutes: int = None,
                         random_chance_percent: int = None, enabled: bool = None):
        # Delegates to RoastCog.configure() rather than duplicating the
        # DB write here — roast.py owns discord_roast_config, this is just
        # the entry point so we don't burn another top-level slash command
        # (bot's already near Discord's 100-command app-command cap).
        roast_cog = interaction.client.get_cog("RoastCog")
        if roast_cog is None:
            await interaction.response.send_message("Roast module isn't loaded.", ephemeral=True)
            return
        await roast_cog.configure(interaction, inactivity_minutes, random_chance_percent, enabled)

    @group.command(name="shiptrigger", description="Manually ship two currently-active members right now")
    async def shiptrigger_cmd(self, interaction: discord.Interaction):
        ship_cog = interaction.client.get_cog("ShipCog")
        if ship_cog is None:
            await interaction.response.send_message("Ship module isn't loaded.", ephemeral=True)
            return
        await ship_cog.manual_trigger(interaction)

    @group.command(name="shipconfig", description="Configure the random shipping feature for this server")
    @app_commands.describe(
        channel="Channel where ship prompts get posted",
        check_interval_minutes="Minimum minutes between ship prompts",
        chance_percent="Odds (0-100) of shipping on each check",
        enabled="Turn the shipping feature on/off for this server",
    )
    async def shipconfig_cmd(self, interaction: discord.Interaction, channel: discord.TextChannel = None,
                              check_interval_minutes: int = None, chance_percent: int = None, enabled: bool = None):
        ship_cog = interaction.client.get_cog("ShipCog")
        if ship_cog is None:
            await interaction.response.send_message("Ship module isn't loaded.", ephemeral=True)
            return
        await ship_cog.configure(interaction, channel, check_interval_minutes, chance_percent, enabled)


    @group.command(name="downloadhub", description="Set up a channel where anyone can submit music/video download links")
    async def downloadhub_cmd(self, interaction: discord.Interaction):
        # Delegates to _views_download_wizard.py rather than a separate
        # top-level /download* group — bot's already at Discord's 100
        # top-level-command cap (see roast/ship's identical note above),
        # so this rides on /setup instead of costing another slot.
        if not _require_perm(interaction, "manage_channels"):
            await _deny(interaction, "Manage Channels")
            return
        await interaction.response.defer()
        clone_id = _clone_id_of(interaction.client)
        config = await db.get_download_config(interaction.guild_id, clone_id=clone_id)
        view = build_download_wizard_view(interaction.guild_id, clone_id, interaction.user.id, config)
        await interaction.followup.send(view=view)
        sent = await interaction.original_response()
        await remember_download_wizard_message(interaction.guild_id, clone_id, interaction.user.id, sent.channel.id, sent.id)


async def setup(bot: commands.Bot):
    await bot.add_cog(SetupChannelsCog(bot))
