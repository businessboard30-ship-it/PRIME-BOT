# path: discord_bot/cogs/_views_welcome.py

"""
Bumper-style multi-step setup wizard for /welcome setup.

Mirrors the "Setup X in N steps" pattern: one message, a checklist that
fills in with ✅ as each step is completed, and every step done via a
component (Channel Select / Select / Modal / Buttons) instead of a
separate slash command with typed args. Kept in its own file for the
same reason as _views_moderation.py — this is a bespoke multi-piece
flow, not a generic one-button nav aid (that's _views_shared.py).

Built entirely from discord.ui.DynamicItem (same pattern as
_views_join_dm.py's wizard), NOT plain View components with an
in-memory `config` dict. Two consequences of that choice, both
deliberate:

  - timeout=None on the outer LayoutView, and no on_timeout handler —
    there's nothing to "expire": every component re-fetches the
    current DB config itself, so there's no stale in-memory state
    that could go bad while a message sits untouched.
  - it survives bot restarts. A plain View instance disposed on
    restart means Discord still shows the buttons but nothing
    handles their clicks. Dynamic items are matched by a regex
    against custom_id and reconstructed on the fly (guild_id /
    clone_id / invoker_id are encoded straight into the custom_id),
    so a click on a wizard message posted before a restart still
    works exactly the same after one — registered once via
    bot.add_dynamic_items(*DYNAMIC_ITEMS) in setup_hook.
"""

import asyncio
import io
import logging
import re

import aiohttp
import discord

from database import db
from discord_bot.cogs._views_shared import check_wizard_access
from modules.welcome_card import render_welcome_card

logger = logging.getLogger(__name__)

# name -> (background_hex, accent_hex). Kept here (rather than
# duplicated in welcome.py) since both the wizard's Select and the
# older /welcome colors autocomplete want the same palette.
THEMES = {
    "Discord Dark": ("#2b2d31", "#5865F2"),
    "Dark Navy": ("#1a1a2e", "#e94560"),
    "Midnight Purple": ("#1a1a2e", "#9b59b6"),
    "Ocean Blue": ("#0f2027", "#3498db"),
    "Forest Green": ("#0d1b1e", "#2ecc71"),
    "Sunset Orange": ("#1a1a2e", "#e67e22"),
    "Hot Pink": ("#1a1a2e", "#ff6b9d"),
}

# name -> direct GIF/PNG/WEBP URL. A curated starter set for the wizard's
# sticker dropdown so most admins never have to go hunting for a direct
# media link themselves — "Custom URL…" still opens WelcomeStickerModal
# for anyone who wants something specific.
STICKER_PRESETS = {
    "Evil Chihuahua": "https://media.tenor.com/HXvyLMZcw_8AAAA1/stan-twt-evil-chihuahua.webp",
    "Hahahaha": "https://media.tenor.com/0gv1aFBwTbUAAAAM/hahahaha.gif",
    "HD Smirk": "https://media.tenor.com/ZARBViZffU4AAAAM/hd-smirk.gif",
    "Archie Malone": "https://media.tenor.com/8TCR7qPoTNIAAAAM/archie-archie-malone.gif",
    "Dark Knight (Joker)": "https://media.tenor.com/viN4goiv9CwAAAAM/the-dark-knight-heath-ledger.gif",
    "Uzi Gun": "https://media.tenor.com/sSY9Hx2Go40AAAAM/uzi-gun.gif",
    "Aura Walk": "https://media4.giphy.com/media/v1.Y2lkPTc5MGI3NjExYTdhZnEyMzN4Z29oNWw5MGg1b3JjMHcwbjMwZ2xyd2Nla3d3bTNrdSZlcD12MV9pbnRlcm5hbF9naWZfYnlfaWQmY3Q9dg/7cjZHladKVmfyjr9wZ/giphy.gif",
    "None (no sticker)": "",
}


def _clone_id_of(interaction: discord.Interaction):
    return getattr(interaction.client, "clone_id", None)


def _apply_template(template: str, member: discord.Member) -> str:
    return (
        template
        .replace("{member}", member.mention)
        .replace("{guild}", member.guild.name)
        .replace("{count}", str(member.guild.member_count))
    )


def _theme_name_for(config: dict) -> str | None:
    for name, (bg, accent) in THEMES.items():
        if bg == config.get("background_color") and accent == config.get("accent_color"):
            return name
    return None


async def _fetch_sticker_bytes(session: aiohttp.ClientSession, sticker_url: str | None) -> bytes | None:
    """Same helper as discord_bot/cogs/welcome.py's — duplicated rather
    than imported to avoid a cross-cog import cycle (welcome.py already
    imports this module for the wizard). Requires a direct link to the
    image file itself (e.g. a media.tenor.com/....gif URL), not a
    tenor.com/view/... page URL, which is HTML and won't decode."""
    sticker_url = (sticker_url or "").strip()
    if not sticker_url:
        return None
    try:
        async with session.get(sticker_url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                return None
            return await resp.read()
    except Exception:
        return None


def _status_color(config: dict) -> discord.Color:
    accent_hex = config.get("accent_color") or "#5865F2"
    try:
        return discord.Color(int(accent_hex.lstrip("#"), 16))
    except (ValueError, AttributeError):
        return discord.Color.blurple()


def render_status_lines(config: dict) -> list:
    """Components V2 counterpart to render_status_embed — a list of body
    lines for a TextDisplay instead of embed fields.

    Branches on use_template: in wolf-card (template) mode, colors/style/
    sticker have no visual effect (see modules/welcome_card.py), so they're
    shown struck through with a note instead of a fake ✅ — only channel,
    delivery, message, and avatar shape are real, live-editable steps
    there. Flat/animated mode shows the original full 7-step checklist."""
    ch = f"<#{config['channel_id']}>" if config.get("channel_id") else "*not set*"
    msg = config.get("message_template") or ""
    msg_preview = (msg[:80] + "…") if len(msg) > 80 else msg
    enabled = bool(config.get("enabled"))
    use_template = config.get("use_template", True)

    step1 = "✅" if config.get("channel_id") else "⬜"
    step2 = "✅" if config.get("message_template") else "⬜"
    shape_label = AVATAR_SHAPE_LABELS.get(config.get("avatar_shape", "circle"), "Circle").split(" — ")[0]
    delivery_mode = config.get("delivery_mode", "channel")
    delivery_label = "DM to member" if delivery_mode == "dm" else "posted in channel"

    lines = [
        f"{step1} **Step 1: Channel** — {ch}",
        f"✅ **Step 2: Delivery** — {delivery_label}",
        f"{step2} **Step 3: Message** — {msg_preview or '*not set*'}",
    ]

    if use_template:
        card_look_name = {"wolf": "Wolf", "reaper": "Metallic Reaper", "shadow": "Shadow Monarch",
                           "sorcerer": "Emerald Sorcerer"}.get(config.get("card_theme", "wolf"), "Wolf")
        lines.append(f"✅ **Step 4: Card look** — {card_look_name}")
        lines.append(f"✅ **Step 7: Avatar shape** — {shape_label}")
        lines.append("-# ~~Colors~~ ~~Style~~ ~~Sticker~~ — only in animated mode (switch below)")
    else:
        theme = _theme_name_for(config) or "custom"
        style_label = "animated" if config.get("card_style", "gif") == "gif" else "static"
        sticker_url = config.get("sticker_url") or ""
        sticker_label = "none" if not sticker_url else next(
            (name for name, url in STICKER_PRESETS.items() if url == sticker_url), "custom"
        )
        step_colors = "✅" if config.get("background_color") else "⬜"
        lines.append(f"{step_colors} **Step 4: Colors** — {theme}")
        lines.append(f"✅ **Step 5: Style** — {style_label}")
        lines.append(f"✅ **Step 6: Sticker** — {sticker_label}")
        lines.append(f"✅ **Step 7: Avatar shape** — {shape_label}")

    card_label = card_look_name if use_template else "animated card"
    lines.append(f"-# Card: {card_label} · Status: {'enabled' if enabled else 'disabled'}")

    ultra_unlocked = bool(config.get("ultra_pack_unlocked"))
    ultra_status = "✅ Unlocked" if ultra_unlocked else "🔒 Locked"
    lines.append(
        f"-# Ultra Pack ({ultra_status}) — use your own png/jpg as the welcome-card "
        f"background instead of a preset look, one-time purchase for the whole server"
    )
    return lines


# ---------------------------------------------------------------------------
# custom_id encoding shared by every dynamic item in this wizard.
# Shape: welcome_wz_<field>:<guild_id>:<clone_id or "-">:<invoker_id or "-">
# invoker_id "-" means "anyone with Manage Server", same meaning as the old
# WelcomeSetupView(invoker_id=None) — used when the wizard is auto-posted
# on join rather than from a command a specific person ran.
# ---------------------------------------------------------------------------

def _encode(field: str, guild_id: int, clone_id, invoker_id) -> str:
    clone_part = "-" if clone_id is None else str(clone_id)
    inv_part = "-" if invoker_id is None else str(invoker_id)
    return f"welcome_wz_{field}:{guild_id}:{clone_part}:{inv_part}"


def _decode(match: "re.Match"):
    guild_id = int(match.group(1))
    clone_part = match.group(2)
    inv_part = match.group(3)
    clone_id = None if clone_part == "-" else int(clone_part)
    invoker_id = None if inv_part == "-" else int(inv_part)
    return guild_id, clone_id, invoker_id


def _id_pattern(field: str) -> str:
    return rf"^welcome_wz_{field}:(\d+):(-|\d+):(-|\d+)$"


async def _check_access(interaction: discord.Interaction, invoker_id) -> bool:
    return await check_wizard_access(interaction, invoker_id, "welcome", "manage_guild", "Manage Server")


async def _get_config_for_modal(guild_id: int, clone_id) -> dict:
    """True cold-path fallback only: a bounded DB fetch, used solely when
    _message_cache (defined below) has nothing yet for this
    (guild_id, clone_id) — i.e. the bot restarted and nobody has
    interacted with or re-posted this wizard message since, so
    build_wizard_view never ran in this process to populate the cache.
    Hard timeout so a slow DB never eats the 3s window a modal must be
    opened within (response.send_modal is the interaction's first and
    only ack here, so nothing can defer() ahead of it the way the other
    callbacks do). On timeout we fall back to an empty dict — the modal
    still opens, just with a blank default instead of the current value,
    which is far better than the whole interaction dying with "The
    application didn't respond in time."."""
    try:
        return await asyncio.wait_for(db.get_welcome_config(guild_id, clone_id=clone_id), timeout=1.0)
    except asyncio.TimeoutError:
        return {}


_message_cache: dict = {}
"""(guild_id, clone_id) -> current message_template, refreshed every time
build_wizard_view renders (i.e. every wizard interaction, plus the two
places that first post/refresh a wizard — welcome.py's setup command and
refresh_posted_wizard). WelcomeEditMessageButton reads this instead of
hitting the DB, because real Discord clicks reconstruct the button via
from_custom_id (no config passed in), so there is no per-instance state to
rely on — this module-level cache is what actually makes the button fast,
not anything stored on the button object itself. Falls back to a bounded
DB fetch only on a true cold click (bot just restarted, nobody has
touched this wizard message since, so build_wizard_view never ran in
this process) — see _get_config_for_modal."""


def build_wizard_view(guild_id: int, clone_id, invoker_id, config: dict) -> discord.ui.LayoutView:
    """Builds a fresh wizard message from a config dict already fetched
    by the caller. Every dynamic item inside re-fetches its own current
    config on interaction rather than trusting this snapshot, so this
    function is only ever used to render — never to hold state between
    clicks.

    timeout=None on the LayoutView (and no on_timeout handler) is
    deliberate, same as the module docstring explains: nothing here is
    ever allowed to "expire" — components re-fetch config themselves and
    are dynamic items, so a wizard message from before a bot restart
    keeps working identically after one.

    Branches on use_template (default True): the wolf card only supports
    live-editing channel/delivery/message/avatar-shape, so template mode
    shows just those rows plus a button to switch to the animated card.
    Flat/animated mode shows the full original row set (colors, style,
    sticker, avatar shape) plus a button to switch back to the wolf card."""
    _message_cache[(guild_id, clone_id)] = config.get("message_template")
    use_template = config.get("use_template", True)

    view = discord.ui.LayoutView(timeout=None)
    container = discord.ui.Container(accent_colour=_status_color(config))

    channel_row = discord.ui.ActionRow()
    channel_row.add_item(WelcomeChannelSelect(guild_id, clone_id, invoker_id, config))
    delivery_row = discord.ui.ActionRow()
    delivery_row.add_item(WelcomeDeliverySelect(guild_id, clone_id, invoker_id, config))
    shape_row = discord.ui.ActionRow()
    shape_row.add_item(WelcomeAvatarShapeSelect(guild_id, clone_id, invoker_id, config))

    button_row = discord.ui.ActionRow()
    button_row.add_item(WelcomeEditMessageButton(guild_id, clone_id, invoker_id, config))
    button_row.add_item(WelcomeToggleButton(guild_id, clone_id, invoker_id, config))
    button_row.add_item(WelcomePreviewButton(guild_id, clone_id, invoker_id))
    button_row.add_item(WelcomeUltraPackButton(guild_id, clone_id, invoker_id, config))

    mode_row = discord.ui.ActionRow()
    mode_row.add_item(WelcomeModeToggleButton(guild_id, clone_id, invoker_id, config))

    text = discord.ui.TextDisplay("\n".join(["### 🚩 Set up welcome cards", *render_status_lines(config)]))

    items = [text, discord.ui.Separator(), channel_row, delivery_row]

    if use_template:
        look_row = discord.ui.ActionRow()
        look_row.add_item(WelcomeCardLookSelect(guild_id, clone_id, invoker_id, config))
        items.append(look_row)
        items.append(shape_row)
    else:
        theme_row = discord.ui.ActionRow()
        theme_row.add_item(WelcomeThemeSelect(guild_id, clone_id, invoker_id, config))
        style_row = discord.ui.ActionRow()
        style_row.add_item(WelcomeCardStyleSelect(guild_id, clone_id, invoker_id, config))
        sticker_row = discord.ui.ActionRow()
        sticker_row.add_item(WelcomeStickerPresetSelect(guild_id, clone_id, invoker_id, config))
        items += [theme_row, style_row, sticker_row, shape_row]

    items += [discord.ui.Separator(), button_row, mode_row]

    for item in items:
        container.add_item(item)

    view.add_item(container)
    return view


async def _rerender(interaction: discord.Interaction, guild_id: int, clone_id, invoker_id):
    """Re-fetches config fresh from the DB and edits the wizard message
    in place — the one thing every dynamic item does after writing its
    own change. Callers must defer() the interaction before doing their
    own DB write, so by the time this runs the response is already
    acknowledged — hence edit_original_response rather than
    response.edit_message (a second response.* call here would raise
    InteractionResponded, and skipping the early defer is what caused
    "The application didn't respond in time" on every wizard click: two
    DB round-trips — the callback's own write, then this function's read —
    stacked up before anything ever ack'd the interaction)."""
    config = await db.get_welcome_config(guild_id, clone_id=clone_id)
    view = build_wizard_view(guild_id, clone_id, invoker_id, config)
    await interaction.edit_original_response(view=view)


async def refresh_posted_wizard(bot, guild_id: int, clone_id=None) -> None:
    """Called by the 6 standalone /welcome commands (enable/disable/
    message/colors/sticker/style) after they write a change directly,
    bypassing the wizard entirely. Without this, a wizard message left
    open in a channel would keep showing whatever it last rendered until
    someone happened to click one of its own components — this pushes
    the DB's current state onto it immediately instead.

    Best-effort and silent: no pointer recorded yet, channel deleted,
    message deleted, or the bot no longer having access are all normal,
    non-error situations (there may simply be no wizard currently open),
    so any failure here just means there was nothing to refresh."""
    config = await db.get_welcome_config(guild_id, clone_id=clone_id)
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


class WelcomeMessageModal(discord.ui.Modal, title="Welcome message"):
    def __init__(self, guild_id: int, clone_id, invoker_id, current: str):
        super().__init__()
        self.guild_id = guild_id
        self.clone_id = clone_id
        self.invoker_id = invoker_id
        self.template = discord.ui.TextInput(
            label="Message ({member} {guild} {count})",
            style=discord.TextStyle.paragraph,
            default=current or "",
            max_length=300,
            required=True,
        )
        self.add_item(self.template)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        logger.info(
            "[welcome edit] submitted by user_id=%s (%s) in guild_id=%s — new template=%r",
            interaction.user.id, interaction.user, self.guild_id, str(self.template.value),
        )
        await db.set_welcome_config(
            self.guild_id, clone_id=self.clone_id, message_template=str(self.template.value)
        )
        await _rerender(interaction, self.guild_id, self.clone_id, self.invoker_id)


class WelcomeChannelSelect(discord.ui.DynamicItem[discord.ui.ChannelSelect], template=_id_pattern("chan")):
    def __init__(self, guild_id: int, clone_id, invoker_id, config: dict):
        self.guild_id = guild_id
        self.clone_id = clone_id
        self.invoker_id = invoker_id
        super().__init__(discord.ui.ChannelSelect(
            placeholder="Step 1 — pick the welcome channel",
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
        await interaction.response.defer()
        channel = self.item.values[0]
        await db.set_welcome_config(self.guild_id, clone_id=self.clone_id, channel_id=channel.id)
        await _rerender(interaction, self.guild_id, self.clone_id, self.invoker_id)


class WelcomeDeliverySelect(discord.ui.DynamicItem[discord.ui.Select], template=_id_pattern("deliv")):
    """Where the welcome card actually gets sent — posted in the Step 1
    channel (default), or DMed straight to the new member instead, for
    admins who don't want join announcements visible in-channel."""

    def __init__(self, guild_id: int, clone_id, invoker_id, config: dict):
        self.guild_id = guild_id
        self.clone_id = clone_id
        self.invoker_id = invoker_id
        current = config.get("delivery_mode", "channel")
        options = [
            discord.SelectOption(label="Post in channel", value="channel",
                                  description="Send in the Step 1 channel", default=(current == "channel")),
            discord.SelectOption(label="Direct message the member", value="dm",
                                  description="Send privately, nothing posted in-server", default=(current == "dm")),
        ]
        super().__init__(discord.ui.Select(
            placeholder="Step 2 — where to send it", options=options,
            custom_id=_encode("deliv", guild_id, clone_id, invoker_id),
        ))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        guild_id, clone_id, invoker_id = _decode(match)
        return cls(guild_id, clone_id, invoker_id, {})

    async def callback(self, interaction: discord.Interaction):
        if not await _check_access(interaction, self.invoker_id):
            return
        await interaction.response.defer()
        await db.set_welcome_config(self.guild_id, clone_id=self.clone_id, delivery_mode=self.item.values[0])
        await _rerender(interaction, self.guild_id, self.clone_id, self.invoker_id)


class WelcomeThemeSelect(discord.ui.DynamicItem[discord.ui.Select], template=_id_pattern("theme")):
    def __init__(self, guild_id: int, clone_id, invoker_id, config: dict):
        self.guild_id = guild_id
        self.clone_id = clone_id
        self.invoker_id = invoker_id
        current = _theme_name_for(config)
        options = [discord.SelectOption(label=name, description=f"{bg} / {accent}", default=(name == current))
                   for name, (bg, accent) in THEMES.items()]
        super().__init__(discord.ui.Select(
            placeholder="Step 4 — pick a color theme", options=options,
            custom_id=_encode("theme", guild_id, clone_id, invoker_id),
        ))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        guild_id, clone_id, invoker_id = _decode(match)
        return cls(guild_id, clone_id, invoker_id, {})

    async def callback(self, interaction: discord.Interaction):
        if not await _check_access(interaction, self.invoker_id):
            return
        await interaction.response.defer()
        bg, accent = THEMES[self.item.values[0]]
        # Mirrors set_welcome_config's own customizing check server-side —
        # picking a color theme only has any visible effect in flat-card
        # mode, so drop to it here too (set_welcome_config already does
        # this automatically when background_color/accent_color are in
        # the write and use_template isn't explicitly passed).
        await db.set_welcome_config(
            self.guild_id, clone_id=self.clone_id, background_color=bg, accent_color=accent
        )
        await _rerender(interaction, self.guild_id, self.clone_id, self.invoker_id)


class WelcomeCardLookSelect(discord.ui.DynamicItem[discord.ui.Select], template=_id_pattern("look")):
    """Step to pick the designed card look (wolf/reaper/shadow/sorcerer —
    modules.welcome_card.PREMIUM_THEMES), distinct from WelcomeThemeSelect
    above (which is flat-card color palettes, only shown in the other
    branch). Previously this whole concept was only reachable via the
    standalone /welcome theme command — the wizard never surfaced it at
    all, so someone who bought the pack through /welcome buypack had no
    way to find or apply a premium look without already knowing that
    command existed.

    Selecting a locked premium look renders an ephemeral one-off preview
    using that look, but does NOT write card_theme — the config is only
    updated once the guild has actually bought the pack (or the free
    'wolf' look is picked), mirroring /welcome theme's own gate exactly."""

    _LOOKS = [
        ("wolf", "Wolf (free, default)"),
        ("reaper", "Metallic Reaper (premium)"),
        ("shadow", "Shadow Monarch (premium)"),
        ("sorcerer", "Emerald Sorcerer (premium)"),
    ]

    def __init__(self, guild_id: int, clone_id, invoker_id, config: dict):
        self.guild_id = guild_id
        self.clone_id = clone_id
        self.invoker_id = invoker_id
        from modules.welcome_card import PREMIUM_THEMES
        current = config.get("card_theme", "wolf")
        unlocked = bool(config.get("card_pack_unlocked"))
        options = []
        for value, label in self._LOOKS:
            locked = value in PREMIUM_THEMES and not unlocked
            options.append(discord.SelectOption(
                label=f"🔒 {label}" if locked else label,
                value=value,
                description=("Locked — preview only, /welcome buypack to use" if locked else None),
                default=(value == current),
            ))
        super().__init__(discord.ui.Select(
            placeholder="Step 4 — pick a card look", options=options,
            custom_id=_encode("look", guild_id, clone_id, invoker_id),
        ))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        guild_id, clone_id, invoker_id = _decode(match)
        return cls(guild_id, clone_id, invoker_id, {})

    async def callback(self, interaction: discord.Interaction):
        if not await _check_access(interaction, self.invoker_id):
            return
        await interaction.response.defer()
        chosen = self.item.values[0]
        from modules.welcome_card import PREMIUM_THEMES
        config = await db.get_welcome_config(self.guild_id, clone_id=self.clone_id)
        unlocked = bool(config.get("card_pack_unlocked"))

        if chosen in PREMIUM_THEMES and not unlocked:
            # Locked: show a preview-only render of this look, but leave
            # the stored card_theme (and the wizard's own selection state)
            # untouched — re-rendering the wizard here puts the select back
            # on whatever look is actually applied, not the locked one just
            # previewed, so nothing looks half-applied.
            await self._send_locked_preview(interaction, chosen, config)
            await _rerender(interaction, self.guild_id, self.clone_id, self.invoker_id)
            return

        await db.set_welcome_config(self.guild_id, clone_id=self.clone_id, card_theme=chosen, use_template=True)
        await _rerender(interaction, self.guild_id, self.clone_id, self.invoker_id)

    async def _send_locked_preview(self, interaction: discord.Interaction, theme: str, config: dict):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    str(interaction.user.display_avatar.replace(size=256).url),
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    avatar_bytes = await resp.read()
                sticker_bytes = await _fetch_sticker_bytes(session, config.get("sticker_url"))
            card_bytes, image_format = await asyncio.to_thread(
                render_welcome_card,
                avatar_bytes, interaction.user.display_name, f"Member #{interaction.guild.member_count}",
                background_color=config.get("background_color"), accent_color=config.get("accent_color"),
                sticker_bytes=sticker_bytes, animate=(config.get("card_style") == "gif"),
                guild_name=interaction.guild.name, use_template=True,
                avatar_shape=config.get("avatar_shape", "circle"),
                theme=theme,
            )
            ext = "gif" if image_format == "GIF" else "png"
            file = discord.File(fp=io.BytesIO(card_bytes), filename=f"locked_preview.{ext}")
            await interaction.followup.send(
                content=(
                    "🔒 **Locked preview** — this server hasn't bought the premium card pack yet. "
                    "Run `/welcome buypack` to unlock this look for real."
                ),
                file=file,
                ephemeral=True,
            )
        except Exception:
            await interaction.followup.send(
                "🔒 That look is part of the premium pack — run `/welcome buypack` to unlock it "
                "(couldn't render a live preview right now).",
                ephemeral=True,
            )


class WelcomeCardStyleSelect(discord.ui.DynamicItem[discord.ui.Select], template=_id_pattern("style")):
    def __init__(self, guild_id: int, clone_id, invoker_id, config: dict):
        self.guild_id = guild_id
        self.clone_id = clone_id
        self.invoker_id = invoker_id
        current = config.get("card_style", "gif")
        options = [
            discord.SelectOption(label="Animated — sticker dances in the card", value="gif", default=(current == "gif")),
            discord.SelectOption(label="Static — plain image, no animation", value="static", default=(current == "static")),
        ]
        super().__init__(discord.ui.Select(
            placeholder="Step 5 — sticker animation style", options=options,
            custom_id=_encode("style", guild_id, clone_id, invoker_id),
        ))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        guild_id, clone_id, invoker_id = _decode(match)
        return cls(guild_id, clone_id, invoker_id, {})

    async def callback(self, interaction: discord.Interaction):
        if not await _check_access(interaction, self.invoker_id):
            return
        await interaction.response.defer()
        await db.set_welcome_config(self.guild_id, clone_id=self.clone_id, card_style=self.item.values[0])
        await _rerender(interaction, self.guild_id, self.clone_id, self.invoker_id)


# value -> display label. Kept here rather than in modules/welcome_card.py
# so the wizard's copy can stay UI-friendly while the renderer's
# AVATAR_SHAPES dict stays a plain value -> mask-fn map.
AVATAR_SHAPE_LABELS = {
    "circle": "Circle — classic frame",
    "rounded_square": "Rounded square — soft, app-icon look",
    "square": "Square — sharp, blocky corners",
    "hexagon": "Hexagon — gamer badge look",
    "diamond": "Diamond — sharp, premium feel",
}


class WelcomeAvatarShapeSelect(discord.ui.DynamicItem[discord.ui.Select], template=_id_pattern("shape")):
    def __init__(self, guild_id: int, clone_id, invoker_id, config: dict):
        self.guild_id = guild_id
        self.clone_id = clone_id
        self.invoker_id = invoker_id
        current = config.get("avatar_shape", "circle")
        options = [
            discord.SelectOption(label=label, value=value, default=(value == current))
            for value, label in AVATAR_SHAPE_LABELS.items()
        ]
        super().__init__(discord.ui.Select(
            placeholder="Step 7 — avatar frame shape", options=options,
            custom_id=_encode("shape", guild_id, clone_id, invoker_id),
        ))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        guild_id, clone_id, invoker_id = _decode(match)
        return cls(guild_id, clone_id, invoker_id, {})

    async def callback(self, interaction: discord.Interaction):
        if not await _check_access(interaction, self.invoker_id):
            return
        await interaction.response.defer()
        # Unlike theme/sticker/style, avatar_shape renders in BOTH card
        # modes (see modules/welcome_card.py's _draw_template_card), so
        # this never needs to touch use_template — it's a plain field set.
        await db.set_welcome_config(self.guild_id, clone_id=self.clone_id, avatar_shape=self.item.values[0])
        await _rerender(interaction, self.guild_id, self.clone_id, self.invoker_id)


class WelcomeStickerModal(discord.ui.Modal, title="Custom sticker URL"):
    def __init__(self, guild_id: int, clone_id, invoker_id, current: str):
        super().__init__()
        self.guild_id = guild_id
        self.clone_id = clone_id
        self.invoker_id = invoker_id
        self.url = discord.ui.TextInput(
            label="Direct GIF/PNG URL (blank = no sticker)",
            style=discord.TextStyle.short,
            default=current or "",
            max_length=500,
            required=False,
        )
        self.add_item(self.url)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        url = str(self.url.value).strip()
        await db.set_welcome_config(self.guild_id, clone_id=self.clone_id, sticker_url=url)
        await _rerender(interaction, self.guild_id, self.clone_id, self.invoker_id)


class WelcomeStickerPresetSelect(discord.ui.DynamicItem[discord.ui.Select], template=_id_pattern("sticker")):
    def __init__(self, guild_id: int, clone_id, invoker_id, config: dict):
        self.guild_id = guild_id
        self.clone_id = clone_id
        self.invoker_id = invoker_id
        current = config.get("sticker_url") or ""
        options = [
            discord.SelectOption(label=name, value=name, default=(url == current))
            for name, url in STICKER_PRESETS.items()
        ]
        options.append(discord.SelectOption(label="Custom URL…", value="__custom__",
                                             description="Paste your own direct GIF/PNG link"))
        super().__init__(discord.ui.Select(
            placeholder="Step 6 — pick a sticker (or paste your own)", options=options,
            custom_id=_encode("sticker", guild_id, clone_id, invoker_id),
        ))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        guild_id, clone_id, invoker_id = _decode(match)
        return cls(guild_id, clone_id, invoker_id, {})

    async def callback(self, interaction: discord.Interaction):
        if not await _check_access(interaction, self.invoker_id):
            return
        if self.item.values[0] == "__custom__":
            config = await _get_config_for_modal(self.guild_id, self.clone_id)
            await interaction.response.send_modal(
                WelcomeStickerModal(self.guild_id, self.clone_id, self.invoker_id, config.get("sticker_url"))
            )
            return
        await interaction.response.defer()
        url = STICKER_PRESETS[self.item.values[0]]
        await db.set_welcome_config(self.guild_id, clone_id=self.clone_id, sticker_url=url)
        await _rerender(interaction, self.guild_id, self.clone_id, self.invoker_id)


class WelcomeEditMessageButton(discord.ui.DynamicItem[discord.ui.Button], template=_id_pattern("edit")):
    def __init__(self, guild_id: int, clone_id, invoker_id, config: dict = None):
        self.guild_id = guild_id
        self.clone_id = clone_id
        self.invoker_id = invoker_id
        super().__init__(discord.ui.Button(
            label="✏️ Edit Message", style=discord.ButtonStyle.secondary,
            custom_id=_encode("edit", guild_id, clone_id, invoker_id),
        ))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        # NOTE: this is the path EVERY real click actually takes — Discord
        # reconstructs dynamic items from custom_id on each interaction, it
        # does not reuse whatever Python object build_wizard_view created at
        # render time. So there is deliberately no config-passing here; see
        # callback() for how the current message is actually obtained fast.
        guild_id, clone_id, invoker_id = _decode(match)
        return cls(guild_id, clone_id, invoker_id)

    async def callback(self, interaction: discord.Interaction):
        if not await _check_access(interaction, self.invoker_id):
            return
        # Debug logging for the "preview shows the wrong person" reports —
        # logs exactly who clicked vs. who the wizard was opened by, plus
        # whether the modal is about to be pre-filled from the warm cache
        # or a cold DB fetch, so a mismatch (if there is one) shows up
        # here instead of needing to be reproduced live.
        logger.info(
            "[welcome edit] clicked by user_id=%s (%s) in guild_id=%s — wizard invoker_id=%s%s",
            interaction.user.id, interaction.user, self.guild_id, self.invoker_id,
            "" if interaction.user.id == self.invoker_id else "  <-- MISMATCH",
        )
        # send_modal() must be the interaction's first and only response, so
        # unlike every other button here we can't defer() before doing async
        # work — whatever we do has to fit inside Discord's hard 3s window.
        # _message_cache (module-level, populated by every build_wizard_view
        # call — i.e. every prior wizard interaction) covers the overwhelming
        # majority of real clicks with zero I/O. Only a true cold click, on a
        # wizard message nobody has touched since the bot last restarted,
        # falls through to the bounded DB fetch below.
        key = (self.guild_id, self.clone_id)
        if key in _message_cache:
            current = _message_cache[key]
            logger.info("[welcome edit] using cached message_template for guild_id=%s", self.guild_id)
        else:
            config = await _get_config_for_modal(self.guild_id, self.clone_id)
            current = config.get("message_template")
            logger.info("[welcome edit] cache miss, fetched from DB for guild_id=%s", self.guild_id)
        await interaction.response.send_modal(
            WelcomeMessageModal(self.guild_id, self.clone_id, self.invoker_id, current)
        )


class WelcomeToggleButton(discord.ui.DynamicItem[discord.ui.Button], template=_id_pattern("toggle")):
    def __init__(self, guild_id: int, clone_id, invoker_id, config: dict):
        self.guild_id = guild_id
        self.clone_id = clone_id
        self.invoker_id = invoker_id
        enabled = bool(config.get("enabled"))
        super().__init__(discord.ui.Button(
            label="Disable" if enabled else "Enable",
            style=discord.ButtonStyle.danger if enabled else discord.ButtonStyle.success,
            custom_id=_encode("toggle", guild_id, clone_id, invoker_id),
        ))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        guild_id, clone_id, invoker_id = _decode(match)
        return cls(guild_id, clone_id, invoker_id, {})

    async def callback(self, interaction: discord.Interaction):
        if not await _check_access(interaction, self.invoker_id):
            return
        config = await db.get_welcome_config(self.guild_id, clone_id=self.clone_id)
        new_state = not config.get("enabled")
        dm_mode = config.get("delivery_mode") == "dm"
        if new_state and not dm_mode and not config.get("channel_id"):
            await interaction.response.send_message("Pick a channel (Step 1) before enabling.", ephemeral=True)
            return
        await interaction.response.defer()
        await db.set_welcome_config(self.guild_id, clone_id=self.clone_id, enabled=new_state)
        await _rerender(interaction, self.guild_id, self.clone_id, self.invoker_id)


class WelcomeModeToggleButton(discord.ui.DynamicItem[discord.ui.Button], template=_id_pattern("mode")):
    """Switches a guild between the wolf card (use_template=True, the
    default) and the flat/animated card (use_template=False). This is the
    ONLY place use_template is ever set directly to True — going template
    -> flat happens implicitly too, via set_welcome_config's "customizing"
    check whenever a color is picked (see database.py), but coming back
    from flat -> template needs an explicit action since there's no
    field-edit that would imply "go back to the wolf card" on its own."""

    def __init__(self, guild_id: int, clone_id, invoker_id, config: dict):
        self.guild_id = guild_id
        self.clone_id = clone_id
        self.invoker_id = invoker_id
        use_template = config.get("use_template", True)
        label = "🎬 Switch to animated card" if use_template else "🐺 Switch to wolf card"
        super().__init__(discord.ui.Button(
            label=label, style=discord.ButtonStyle.secondary,
            custom_id=_encode("mode", guild_id, clone_id, invoker_id),
        ))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        guild_id, clone_id, invoker_id = _decode(match)
        return cls(guild_id, clone_id, invoker_id, {})

    async def callback(self, interaction: discord.Interaction):
        if not await _check_access(interaction, self.invoker_id):
            return
        await interaction.response.defer()
        config = await db.get_welcome_config(self.guild_id, clone_id=self.clone_id)
        new_use_template = not config.get("use_template", True)
        await db.set_welcome_config(self.guild_id, clone_id=self.clone_id, use_template=new_use_template)
        await _rerender(interaction, self.guild_id, self.clone_id, self.invoker_id)


class WelcomePreviewButton(discord.ui.DynamicItem[discord.ui.Button], template=_id_pattern("preview")):
    def __init__(self, guild_id: int, clone_id, invoker_id):
        self.guild_id = guild_id
        self.clone_id = clone_id
        self.invoker_id = invoker_id
        super().__init__(discord.ui.Button(
            label="👁️ Preview", style=discord.ButtonStyle.primary,
            custom_id=_encode("preview", guild_id, clone_id, invoker_id),
        ))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        guild_id, clone_id, invoker_id = _decode(match)
        return cls(guild_id, clone_id, invoker_id)

    async def callback(self, interaction: discord.Interaction):
        if not await _check_access(interaction, self.invoker_id):
            return
        await interaction.response.defer(ephemeral=True)
        try:
            config = await db.get_welcome_config(self.guild_id, clone_id=self.clone_id)
            async with aiohttp.ClientSession() as session:
                async with session.get(str(interaction.user.display_avatar.replace(size=256).url), timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    avatar_bytes = await resp.read()
                sticker_bytes = await _fetch_sticker_bytes(session, config.get("sticker_url"))
            # Off-loaded to a thread: this is synchronous PIL work (can take
            # real time, especially compositing an animated GIF sticker) and
            # would otherwise block the bot's single event loop entirely —
            # freezing every other interaction bot-wide, not just this one —
            # for however long it takes to render.
            card_bytes, image_format = await asyncio.to_thread(
                render_welcome_card,
                avatar_bytes, interaction.user.display_name, f"Member #{interaction.guild.member_count}",
                background_color=config.get("background_color", "#2b2d31"),
                accent_color=config.get("accent_color", "#5865F2"),
                sticker_bytes=sticker_bytes, animate=(config.get("card_style") == "gif"),
                avatar_shape=config.get("avatar_shape", "circle"),
                use_template=config.get("use_template", True),
                theme=config.get("card_theme", "wolf"),
            )
            ext = "gif" if image_format == "GIF" else "png"
            file = discord.File(fp=io.BytesIO(card_bytes), filename=f"preview.{ext}")
            template = config.get("message_template") or "Welcome {member} to {guild}! You are member #{count}."
            text = discord.ui.TextDisplay("\n".join(["### Welcome preview", _apply_template(template, interaction.user), "-# Preview only — visible to you"]))
            gallery = discord.ui.MediaGallery(discord.MediaGalleryItem(file))
            preview_view = discord.ui.LayoutView()
            preview_view.add_item(discord.ui.Container(text, gallery, accent_colour=discord.Color.blurple()))
            await interaction.followup.send(view=preview_view, file=file, ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"Couldn't render a preview: {e}", ephemeral=True)


class WelcomeUltraPackButton(discord.ui.DynamicItem[discord.ui.Button], template=_id_pattern("ultra")):
    """Surfaces the ultra pack (own png/jpg welcome-card background, see
    discord_bot/views_card_pack.py) inside the wizard itself — previously
    this was only reachable via the standalone `/welcome buyultra` slash
    command, with nothing in the wizard even mentioning it existed.

    Locked: kicks off the same payment flow `/welcome buyultra` does
    (start_ultra_pack_payment, including its own free bot-owner bypass),
    then re-renders the wizard in place so the button flips to unlocked
    as soon as payment is verified — no need to close and reopen /welcome
    setup. Unlocked: just points the admin at `/welcome custombg`, since
    setting the actual background is its own upload/URL step, not
    something a wizard button can collect input for."""

    def __init__(self, guild_id: int, clone_id, invoker_id, config: dict):
        self.guild_id = guild_id
        self.clone_id = clone_id
        self.invoker_id = invoker_id
        unlocked = bool(config.get("ultra_pack_unlocked"))
        label = "🖼️ Ultra Pack ✅" if unlocked else "🖼️ Buy Ultra Pack"
        style = discord.ButtonStyle.secondary if unlocked else discord.ButtonStyle.success
        super().__init__(discord.ui.Button(
            label=label, style=style,
            custom_id=_encode("ultra", guild_id, clone_id, invoker_id),
        ))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        guild_id, clone_id, invoker_id = _decode(match)
        return cls(guild_id, clone_id, invoker_id, {})

    async def callback(self, interaction: discord.Interaction):
        if not await _check_access(interaction, self.invoker_id):
            return
        config = await db.get_welcome_config(self.guild_id, clone_id=self.clone_id)
        if config.get("ultra_pack_unlocked"):
            await interaction.response.send_message(
                "This server already owns the ultra pack — set your background with `/welcome custombg`.",
                ephemeral=True,
            )
            return
        # ephemeral+thinking here (not the plain defer() every other button
        # in this wizard uses) because start_ultra_pack_payment posts its
        # own separate ephemeral embed via followup.send — it does not
        # edit the wizard message itself, so there's nothing to
        # _rerender() until the purchase is actually verified. That
        # refresh happens over in views_card_pack.py's verify callback via
        # refresh_posted_wizard, same as every other out-of-band write.
        await interaction.response.defer(ephemeral=True, thinking=True)
        from discord_bot.views_card_pack import start_ultra_pack_payment
        await start_ultra_pack_payment(interaction)


# Registered once in discord_bot/bot.py's setup_hook via
# bot.add_dynamic_items(*DYNAMIC_ITEMS) — same mechanism as
# _views_join_dm.py's DYNAMIC_ITEMS, so these keep working after a
# restart regardless of which process originally sent the message.
DYNAMIC_ITEMS = (
    WelcomeChannelSelect, WelcomeDeliverySelect, WelcomeThemeSelect, WelcomeCardLookSelect,
    WelcomeCardStyleSelect, WelcomeAvatarShapeSelect, WelcomeStickerPresetSelect,
    WelcomeEditMessageButton, WelcomeToggleButton, WelcomePreviewButton, WelcomeModeToggleButton,
    WelcomeUltraPackButton,
)
