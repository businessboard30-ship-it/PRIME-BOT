# path: discord_bot/cogs/_views_card_customize.py

"""
Customize Card (ULTRA card) wizard — opened by the "Customize Card" button in
the /welcome setup wizard (WelcomeUltraPackButton in _views_welcome.py).

"Customize Card" is the ultra pack: the server supplies its OWN background
image and the bot draws the welcome card over it (see
modules/welcome_card._draw_custom_bg_card). This wizard is where that card is
designed — one ephemeral message (only the admin who tapped sees it):

  - background   upload a png/jpg or paste a direct link (+ clear)
  - banner       bottom / top / none
  - darkness     light / medium / heavy
  - avatar side  left / right, and avatar frame shape
  - text color   white, gold, cyan, pink, green, red
  - heading      replace the "Welcome to {server}!" line
  - member #     show/hide the "MEMBER #N" line
  - the TV       the real card is drawn at the top of the wizard and redraws by
                 itself after every change (a sample backdrop is used until a
                 background is set); reset puts the layout back to default

The controls are split into three tabs under the TV (Background / Layout / Text) so only
2-3 controls show at a time. There is NO preview rate limit here (the old Preview button
is kept only so wizards posted before this change keep working).

Try-before-buy: the editor opens for EVERY server. Servers that haven't
bought Customize Card edit an in-memory DRAFT (never written to the DB, never
uploaded to the hosting channel) and get watermarked, rate-limited previews;
saving/applying needs the purchase (Unlock button, same payment flow as
/welcome buyultra, with the free bot-owner bypass). Every DB write is still
gated server-side: unpaid servers only ever touch the in-memory draft. Premium servers arrive already
unlocked via get_welcome_config.

Layout options are stored as JSON in discord_welcome_config.ultra_card_json
and validated by modules.welcome_card.parse_ultra_options.

Same architecture as _views_welcome.py: every component is a
discord.ui.DynamicItem that re-reads the DB itself (nothing goes stale,
survives restarts). Registered via DYNAMIC_ITEMS in bot.py's setup_hook.
Import direction: this module imports _views_welcome at module level;
_views_welcome imports THIS module only lazily inside a callback.
"""

import asyncio
import io
import json
import logging
import re
import time

import aiohttp
import discord
from PIL import Image, ImageDraw

import config as bot_config
from database import db
from discord_bot.cogs._views_shared import check_wizard_access
from discord_bot.cogs import _views_welcome as vw
from modules.welcome_card import (
    ULTRA_AVATAR_SIDES, ULTRA_BANNERS, ULTRA_DIM_ALPHA, ULTRA_HEADING_MAX, ULTRA_TEXT_COLORS,
    TEMPLATE_HEIGHT, TEMPLATE_WIDTH, parse_ultra_options, render_welcome_card,
)

logger = logging.getLogger(__name__)


# ── ids / access ──────────────────────────────────────────────────────────

def _encode(field: str, guild_id: int, clone_id, invoker_id) -> str:
    clone_part = "-" if clone_id is None else str(clone_id)
    inv_part = "-" if invoker_id is None else str(invoker_id)
    return f"cardwz_{field}:{guild_id}:{clone_part}:{inv_part}"


def _decode(match: "re.Match"):
    guild_id = int(match.group(1))
    clone_id = None if match.group(2) == "-" else int(match.group(2))
    invoker_id = None if match.group(3) == "-" else int(match.group(3))
    return guild_id, clone_id, invoker_id


def _id_pattern(field: str) -> str:
    return rf"^cardwz_{field}:(\d+):(-|\d+):(-|\d+)$"


async def _check_access(interaction: discord.Interaction, invoker_id, guild_id: int | None = None) -> bool:
    return await check_wizard_access(interaction, invoker_id, "welcome", "manage_guild", "Manage Server", guild_id=guild_id)


# ── drafts (unpaid servers only; memory only) ─────────────────────────────

_DRAFT_TTL = 30 * 60
_DRAFT_MAX = 100
_DRAFTS: dict = {}          # (guild_id, clone_id, user_id) -> draft dict


def _draft_key(guild_id: int, clone_id, user_id):
    return (guild_id, clone_id, user_id)


def _get_draft(guild_id: int, clone_id, user_id, create: bool = False):
    now = time.monotonic()
    for k in [k for k, d in _DRAFTS.items() if now - d["ts"] > _DRAFT_TTL]:
        _DRAFTS.pop(k, None)
    key = _draft_key(guild_id, clone_id, user_id)
    d = _DRAFTS.get(key)
    if d is None and create:
        while len(_DRAFTS) >= _DRAFT_MAX:
            _DRAFTS.pop(min(_DRAFTS, key=lambda k: _DRAFTS[k]["ts"]), None)
        d = _DRAFTS[key] = {"opts": parse_ultra_options(None), "shape": "circle", "bg": None, "ts": now}
    if d is not None:
        d["ts"] = now
    return d


def _effective_config(config: dict, guild_id: int, clone_id, user_id) -> dict:
    """Config as the editor should display/preview it: the DB row for
    unlocked servers, DB row overlaid with the user's draft otherwise."""
    if config.get("ultra_pack_unlocked"):
        return config
    d = _get_draft(guild_id, clone_id, user_id)
    if d is None:
        return dict(config, custom_background_url=None)
    return dict(
        config, ultra_card_json=json.dumps(d["opts"]), avatar_shape=d["shape"],
        custom_background_url="draft" if d["bg"] else None,
    )


def _shrink_bg(data: bytes):
    """Downscale a draft background so memory stays small. None if not an image."""
    try:
        img = Image.open(io.BytesIO(data)).convert("RGB")
        img.thumbnail((1600, 1600))
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=88)
        return out.getvalue()
    except Exception:
        return None


def _watermark(card_bytes: bytes) -> bytes:
    try:
        img = Image.open(io.BytesIO(card_bytes)).convert("RGBA")
        layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        step = max(90, img.size[0] // 6)
        for y in range(-step, img.size[1] + step, step):
            for x in range(-step, img.size[0] + step, step * 2):
                d.text((x + (y // step % 2) * step, y), "PREVIEW", fill=(255, 255, 255, 70))
        out = io.BytesIO()
        Image.alpha_composite(img, layer).convert("RGB").save(out, format="PNG")
        return out.getvalue()
    except Exception:
        return card_bytes


# ── option storage ────────────────────────────────────────────────────────

async def _save_option(guild_id: int, clone_id, user_id=None, **changes) -> None:
    cfg = await db.get_welcome_config(guild_id, clone_id=clone_id)
    if not cfg.get("ultra_pack_unlocked"):
        d = _get_draft(guild_id, clone_id, user_id, create=True)
        d["opts"].update(changes)
        return
    opts = parse_ultra_options(cfg.get("ultra_card_json"))
    opts.update(changes)
    await db.set_welcome_config(guild_id, clone_id=clone_id, ultra_card_json=json.dumps(opts))


# ── rendering the wizard ──────────────────────────────────────────────────

_BANNER_LABELS = {"bottom": "Bottom banner (classic)", "top": "Top banner", "none": "No banner (text over image)"}
_DIM_LABELS = {"light": "Light — see more of your image", "medium": "Medium (default)", "heavy": "Heavy — best text contrast"}
_SIDE_LABELS = {"left": "Avatar on the left (classic)", "right": "Avatar on the right"}
_COLOR_LABELS = {"white": "White (default)", "gold": "Gold", "cyan": "Cyan", "pink": "Pink", "green": "Green", "red": "Red"}


# -- the TV: live preview drawn inside the wizard message ---------------------

_TABS = ("bg", "layout", "text")
_TAB_LABELS = {"bg": "\U0001F5BC\uFE0F Background", "layout": "\U0001F4D0 Layout", "text": "\U0001F524 Text"}
_TAB_TTL = 30 * 60
_STATE_MAX = 1000
_TAB_STATE: dict = {}        # (guild, clone, user) -> (tab, monotonic ts)
_TV_NOTES: dict = {}         # same key -> caption note from the last render
_GEN: dict = {}              # same key -> latest redraw request number (batches quick taps)
_LOCKS: dict = {}            # same key -> asyncio.Lock (one render at a time per editor)
_REDRAW_DELAY = 0.35         # taps inside this window collapse into ONE redraw
_TV_WIDTH = 960              # the TV is a downscaled copy; the real card is untouched
_AVATAR_CACHE: dict = {}     # (user_id, avatar key) -> bytes
_BG_CACHE: dict = {}         # custom background url -> (monotonic ts, bytes)
_BG_TTL = 600
_BG_MAX = 16
_BG_TASKS: set = set()


def _trim(d: dict, limit: int) -> None:
    if len(d) > limit:
        for k in list(d)[: len(d) - limit // 2]:
            d.pop(k, None)


def _get_tab(key) -> str:
    st = _TAB_STATE.get(key)
    if st is None or time.monotonic() - st[1] > _TAB_TTL or st[0] not in _TABS:
        return "bg"
    return st[0]


def _set_tab(key, tab: str) -> None:
    _TAB_STATE[key] = (tab if tab in _TABS else "bg", time.monotonic())
    _trim(_TAB_STATE, _STATE_MAX)


def _caption(unlocked: bool, note, tv_ok: bool) -> str:
    lines = ["### \U0001F3A8 Customize your welcome card"]
    if not unlocked:
        lines.append(
            f"\U0001F513 **Preview mode** \u2014 play with everything. To **save** it (and use your own "
            f"background on every join) unlock Customize Card: one-time **${bot_config.ULTRA_PACK_FEE_USD:g}**, whole server."
        )
    if tv_ok:
        lines.append(f"-# {note or 'Preview'} \u2014 the card above redraws after every change. Changes apply to the next welcome.")
    else:
        lines.append("-# Couldn't draw the preview just now \u2014 change anything to try again.")
    return "\n".join(lines)


def build_customize_view(guild_id: int, clone_id, invoker_id, config: dict, tab: str = "bg",
                         has_tv: bool = False, note=None) -> discord.ui.LayoutView:
    unlocked = bool(config.get("ultra_pack_unlocked"))
    config = _effective_config(config, guild_id, clone_id, invoker_id)
    tab = tab if tab in _TABS else "bg"

    opts = parse_ultra_options(config.get("ultra_card_json"))
    has_bg = bool(config.get("custom_background_url"))

    view = discord.ui.LayoutView(timeout=None)
    container = discord.ui.Container(accent_colour=discord.Color.blurple())
    container.add_item(discord.ui.TextDisplay(_caption(unlocked, note, has_tv)))
    if has_tv:
        container.add_item(discord.ui.MediaGallery(discord.MediaGalleryItem("attachment://card.png")))
    container.add_item(discord.ui.Separator())

    tabs = discord.ui.ActionRow()
    for t in _TABS:
        tabs.add_item(CardTabButton(t, guild_id, clone_id, invoker_id, active=(t == tab)))
    container.add_item(tabs)

    def _select_row(select_cls):
        row = discord.ui.ActionRow()
        row.add_item(select_cls(guild_id, clone_id, invoker_id, config))
        container.add_item(row)

    if tab == "bg":
        bg_row = discord.ui.ActionRow()
        bg_row.add_item(CardBackgroundButton(guild_id, clone_id, invoker_id))
        if has_bg:
            bg_row.add_item(CardClearBackgroundButton(guild_id, clone_id, invoker_id))
        container.add_item(bg_row)
        _select_row(CardDimSelect)
    elif tab == "layout":
        for select_cls in (CardBannerSelect, CardSideSelect, CardShapeSelect):
            _select_row(select_cls)
    else:
        _select_row(CardColorSelect)
        text_row = discord.ui.ActionRow()
        text_row.add_item(CardHeadingButton(guild_id, clone_id, invoker_id))
        text_row.add_item(CardNumberToggleButton(guild_id, clone_id, invoker_id, opts["show_number"]))
        container.add_item(text_row)

    container.add_item(discord.ui.Separator())
    act_row = discord.ui.ActionRow()
    act_row.add_item(CardResetButton(guild_id, clone_id, invoker_id))
    if unlocked:
        act_row.add_item(CardDoneButton(guild_id, clone_id, invoker_id))
    else:
        act_row.add_item(CardUnlockButton(guild_id, clone_id, invoker_id))
    container.add_item(act_row)

    view.add_item(container)
    return view


def _tv_finish(card_bytes: bytes, watermark: bool) -> bytes:
    """Downscaled PNG copy for the TV (the card the bot posts on joins is never touched)."""
    img = Image.open(io.BytesIO(card_bytes)).convert("RGB")
    if img.width > _TV_WIDTH:
        img.thumbnail((_TV_WIDTH, _TV_WIDTH * 4))
    out = io.BytesIO()
    img.save(out, format="PNG")
    data = out.getvalue()
    return _watermark(data) if watermark else data


async def _avatar_bytes(session: aiohttp.ClientSession, user) -> bytes:
    asset = user.display_avatar
    key = (user.id, getattr(asset, "key", None))
    hit = _AVATAR_CACHE.get(key)
    if hit is not None:
        return hit
    async with session.get(str(asset.replace(size=256).url), timeout=aiohttp.ClientTimeout(total=10)) as resp:
        data = await resp.read()
    _AVATAR_CACHE[key] = data
    _trim(_AVATAR_CACHE, 200)
    return data


async def _render_tv(client, guild, guild_id: int, clone_id, user):
    """Draw the real card for the editor. Returns (png_bytes, caption_note). Raises on failure.
    No rate limit: unpaid servers get the same watermarked draft preview, as often as they like."""
    from discord_bot.cogs.welcome import _custom_bg_bytes_for_render
    if guild is None:
        raise RuntimeError("the server isn't available to this bot")
    real_cfg = await db.get_welcome_config(guild_id, clone_id=clone_id)
    unlocked = bool(real_cfg.get("ultra_pack_unlocked"))
    cfg = _effective_config(real_cfg, guild_id, clone_id, user.id)
    async with aiohttp.ClientSession() as session:
        avatar = await _avatar_bytes(session, user)
        if unlocked:
            url = str(cfg.get("custom_background_url") or "")
            hit = _BG_CACHE.get(url)
            if hit is not None and time.monotonic() - hit[0] < _BG_TTL:
                bg_bytes = hit[1]
            else:
                bg_bytes = await _custom_bg_bytes_for_render(session, cfg, client)
                if bg_bytes is not None and url:
                    _BG_CACHE[url] = (time.monotonic(), bg_bytes)
                    _trim(_BG_CACHE, _BG_MAX)
        else:
            d = _get_draft(guild_id, clone_id, user.id)
            bg_bytes = d["bg"] if d else None
    using_sample = bg_bytes is None
    if using_sample:
        bg_bytes = _sample_backdrop()
    card_bytes, _fmt = await asyncio.to_thread(
        render_welcome_card,
        avatar, user.display_name, f"Member #{guild.member_count}",
        avatar_shape=cfg.get("avatar_shape", "circle"),
        guild_name=guild.name, use_template=True,
        custom_background_bytes=bg_bytes,
        ultra_options=cfg.get("ultra_card_json"),
    )
    png = await asyncio.to_thread(_tv_finish, card_bytes, not unlocked)
    note = "Sample backdrop \u2014 set a background to use your own image" if using_sample else "Your background"
    return png, note


def _tv_file(png: bytes) -> discord.File:
    return discord.File(fp=io.BytesIO(png), filename="card.png")


async def open_customize_wizard(interaction: discord.Interaction, guild_id: int, clone_id) -> None:
    """Called by the setup wizard's Customize Card button. The caller has
    already deferred (ephemeral) \u2014 this just posts the wizard as a followup."""
    config = await db.get_welcome_config(guild_id, clone_id=clone_id)
    if config.get("ultra_pack_unlocked"):
        # Bought after designing in preview mode: carry the draft's layout
        # over (the draft background is never stored \u2014 set it again).
        d = _DRAFTS.pop(_draft_key(guild_id, clone_id, interaction.user.id), None)
        if d is not None:
            await db.set_welcome_config(
                guild_id, clone_id=clone_id,
                ultra_card_json=json.dumps(d["opts"]), avatar_shape=d["shape"],
            )
            config = await db.get_welcome_config(guild_id, clone_id=clone_id)
    key = _draft_key(guild_id, clone_id, interaction.user.id)
    _set_tab(key, "bg")
    tv = None
    try:
        tv = await _render_tv(interaction.client, interaction.guild or interaction.client.get_guild(guild_id),
                              guild_id, clone_id, interaction.user)
    except Exception as e:
        logger.warning(f"[cardwz] first preview failed for guild {guild_id}: {e}")
    _TV_NOTES[key] = tv[1] if tv else None
    _trim(_TV_NOTES, _STATE_MAX)
    view = build_customize_view(guild_id, clone_id, interaction.user.id, config, tab="bg",
                                has_tv=tv is not None, note=_TV_NOTES[key])
    if tv:
        await interaction.followup.send(view=view, file=_tv_file(tv[0]), ephemeral=True)
    else:
        await interaction.followup.send(view=view, ephemeral=True)


def _refresh_public_wizard(client, guild_id: int, clone_id) -> None:
    """Keep the public /welcome setup message's status fresh WITHOUT making the TV wait for it."""
    async def _run():
        try:
            await vw.refresh_posted_wizard(client, guild_id, clone_id)
        except Exception as e:  # best-effort
            logger.debug(f"[cardwz] main wizard refresh skipped: {e}")
    task = asyncio.create_task(_run())
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)


async def _rerender(interaction: discord.Interaction, guild_id: int, clone_id, invoker_id):
    """Interaction must already be deferred. Redraws the TV and the controls. Quick taps are batched:
    only the newest request inside _REDRAW_DELAY draws, and one render runs at a time per editor."""
    key = _draft_key(guild_id, clone_id, interaction.user.id)
    gen = _GEN[key] = _GEN.get(key, 0) + 1
    _trim(_GEN, _STATE_MAX)
    await asyncio.sleep(_REDRAW_DELAY)
    if _GEN.get(key) != gen:
        return                                  # a newer tap will draw the latest state
    lock = _LOCKS.setdefault(key, asyncio.Lock())
    _trim(_LOCKS, _STATE_MAX)
    async with lock:
        if _GEN.get(key) != gen:
            return
        config = await db.get_welcome_config(guild_id, clone_id=clone_id)
        tv = None
        try:
            tv = await _render_tv(interaction.client, interaction.guild or interaction.client.get_guild(guild_id),
                                  guild_id, clone_id, interaction.user)
        except Exception as e:
            logger.warning(f"[cardwz] preview failed for guild {guild_id}: {e}")
        _TV_NOTES[key] = tv[1] if tv else None
        _trim(_TV_NOTES, _STATE_MAX)
        view = build_customize_view(guild_id, clone_id, invoker_id, config, tab=_get_tab(key),
                                    has_tv=tv is not None, note=_TV_NOTES[key])
        if tv:
            await interaction.edit_original_response(view=view, attachments=[_tv_file(tv[0])])
        else:
            await interaction.edit_original_response(view=view, attachments=[])
    _refresh_public_wizard(interaction.client, guild_id, clone_id)


async def _require_unlocked(interaction: discord.Interaction, guild_id: int, clone_id) -> bool:
    """Server-side gate for every write (the UI can be stale). Interaction
    must already be deferred/acked."""
    cfg = await db.get_welcome_config(guild_id, clone_id=clone_id)
    if cfg.get("ultra_pack_unlocked"):
        return True
    await interaction.followup.send("Customize Card isn't unlocked for this server.", ephemeral=True)
    return False


# ── option selects (one mixin, five thin subclasses) ──────────────────────

class _OptionSelectMixin:
    FIELD = ""        # key in ultra_card_json (or column) this select edits
    ID = ""           # custom_id field name — must match the class's template
    PLACEHOLDER = ""
    CHOICES: dict = {}

    def __init__(self, guild_id: int, clone_id, invoker_id, config: dict):
        self.guild_id = guild_id
        self.clone_id = clone_id
        self.invoker_id = invoker_id
        current = self._current(config)
        options = [
            discord.SelectOption(label=label, value=value, default=(value == current))
            for value, label in self.CHOICES.items()
        ]
        super().__init__(discord.ui.Select(
            placeholder=self.PLACEHOLDER, options=options,
            custom_id=_encode(self.ID, guild_id, clone_id, invoker_id),
        ))

    def _current(self, config: dict):
        return parse_ultra_options(config.get("ultra_card_json"))[self.FIELD]

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        g, c, i = _decode(match)
        return cls(g, c, i, {})

    async def _save(self, value: str, user_id=None):
        await _save_option(self.guild_id, self.clone_id, user_id, **{self.FIELD: value})

    async def callback(self, interaction: discord.Interaction):
        if not await _check_access(interaction, self.invoker_id, self.guild_id):
            return
        await interaction.response.defer()
        await self._save(self.item.values[0], interaction.user.id)
        await _rerender(interaction, self.guild_id, self.clone_id, self.invoker_id)


class CardBannerSelect(_OptionSelectMixin, discord.ui.DynamicItem[discord.ui.Select], template=_id_pattern("banner")):
    FIELD, ID, PLACEHOLDER, CHOICES = "banner", "banner", "Banner position", _BANNER_LABELS


class CardDimSelect(_OptionSelectMixin, discord.ui.DynamicItem[discord.ui.Select], template=_id_pattern("dim")):
    FIELD, ID, PLACEHOLDER, CHOICES = "dim", "dim", "Banner darkness", _DIM_LABELS


class CardSideSelect(_OptionSelectMixin, discord.ui.DynamicItem[discord.ui.Select], template=_id_pattern("side")):
    FIELD, ID, PLACEHOLDER, CHOICES = "avatar_side", "side", "Avatar side", _SIDE_LABELS


class CardColorSelect(_OptionSelectMixin, discord.ui.DynamicItem[discord.ui.Select], template=_id_pattern("color")):
    FIELD, ID, PLACEHOLDER, CHOICES = "text_color", "color", "Text color", _COLOR_LABELS


class CardShapeSelect(_OptionSelectMixin, discord.ui.DynamicItem[discord.ui.Select], template=_id_pattern("shape")):
    """Avatar frame shape lives in its own column (shared with the other
    card modes), not in ultra_card_json."""
    FIELD, ID, PLACEHOLDER, CHOICES = "avatar_shape", "shape", "Avatar frame shape", vw.AVATAR_SHAPE_LABELS

    def _current(self, config: dict):
        return config.get("avatar_shape", "circle")

    async def _save(self, value: str, user_id=None):
        cfg = await db.get_welcome_config(self.guild_id, clone_id=self.clone_id)
        if not cfg.get("ultra_pack_unlocked"):
            _get_draft(self.guild_id, self.clone_id, user_id, create=True)["shape"] = value
            return
        await db.set_welcome_config(self.guild_id, clone_id=self.clone_id, avatar_shape=value)


# ── custom background ─────────────────────────────────────────────────────

class CardBackgroundModal(discord.ui.Modal, title="Custom card background"):
    def __init__(self, guild_id: int, clone_id, invoker_id):
        super().__init__()
        self.guild_id = guild_id
        self.clone_id = clone_id
        self.invoker_id = invoker_id
        self.upload = discord.ui.FileUpload(required=False, min_values=0, max_values=1)
        self.url = discord.ui.TextInput(
            style=discord.TextStyle.short, required=False, max_length=500,
            placeholder="https://…/image.png",
        )
        self.add_item(discord.ui.Label(
            text="Upload an image", description="png or jpg, up to 8MB", component=self.upload,
        ))
        self.add_item(discord.ui.Label(
            text="…or paste a direct image link", description="Fill in only one of the two", component=self.url,
        ))

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        from discord_bot.cogs.welcome import _upload_custom_bg, _fetch_custom_bg_bytes

        cfg = await db.get_welcome_config(self.guild_id, clone_id=self.clone_id)
        unlocked = bool(cfg.get("ultra_pack_unlocked"))
        files = list(self.upload.values or [])
        url = str(self.url.value or "").strip()
        if files and url:
            await interaction.followup.send("⚠️ Use either an upload or a link, not both.", ephemeral=True)
            return
        if not files and not url:
            await interaction.followup.send("⚠️ Upload an image or paste a link first.", ephemeral=True)
            return

        if not unlocked:
            # Preview only: hold the image in memory, never host or store it.
            from discord_bot.cogs.welcome import CUSTOM_BG_ALLOWED_CONTENT_TYPES, CUSTOM_BG_MAX_BYTES
            if files:
                ct = (files[0].content_type or "").split(";")[0].strip().lower()
                if ct not in CUSTOM_BG_ALLOWED_CONTENT_TYPES or files[0].size > CUSTOM_BG_MAX_BYTES:
                    await interaction.followup.send("⚠️ Use a png/jpg under 8MB.", ephemeral=True)
                    return
                data = await files[0].read()
            else:
                async with aiohttp.ClientSession() as session:
                    data, reason = await _fetch_custom_bg_bytes(session, url)
                if data is None:
                    await interaction.followup.send(f"⚠️ Couldn't use that image — {reason}.", ephemeral=True)
                    return
            small = await asyncio.to_thread(_shrink_bg, data)
            if small is None:
                await interaction.followup.send("⚠️ Couldn't read that image.", ephemeral=True)
                return
            _get_draft(self.guild_id, self.clone_id, interaction.user.id, create=True)["bg"] = small
            await _rerender(interaction, self.guild_id, self.clone_id, self.invoker_id)
            return

        if files:
            host_channel_id, host_message_id, cdn_url, reason = await _upload_custom_bg(
                interaction.client, files[0], (interaction.guild or interaction.client.get_guild(self.guild_id))
            )
            if reason:
                await interaction.followup.send(f"⚠️ Couldn't use that image — {reason}.", ephemeral=True)
                return
            await db.set_welcome_config(
                self.guild_id, clone_id=self.clone_id,
                custom_background_url=cdn_url,
                custom_bg_channel_id=host_channel_id, custom_bg_message_id=host_message_id,
            )
        else:
            async with aiohttp.ClientSession() as session:
                data, reason = await _fetch_custom_bg_bytes(session, url)
            if data is None:
                await interaction.followup.send(f"⚠️ Couldn't use that image — {reason}.", ephemeral=True)
                return
            await db.set_welcome_config(
                self.guild_id, clone_id=self.clone_id,
                custom_background_url=url, custom_bg_channel_id=None, custom_bg_message_id=None,
            )
        await _rerender(interaction, self.guild_id, self.clone_id, self.invoker_id)


class _Btn:
    """Shared constructor/from_custom_id for the plain buttons."""
    FIELD = ""
    LABEL = ""
    STYLE = discord.ButtonStyle.secondary

    def __init__(self, guild_id: int, clone_id, invoker_id, *_):
        self.guild_id = guild_id
        self.clone_id = clone_id
        self.invoker_id = invoker_id
        super().__init__(discord.ui.Button(
            label=self.label_text(*_), style=self.STYLE,
            custom_id=_encode(self.FIELD, guild_id, clone_id, invoker_id),
        ))

    def label_text(self, *_):
        return self.LABEL

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        g, c, i = _decode(match)
        return cls(g, c, i)


class CardBackgroundButton(_Btn, discord.ui.DynamicItem[discord.ui.Button], template=_id_pattern("setbg")):
    FIELD, LABEL, STYLE = "setbg", "🖼️ Set background", discord.ButtonStyle.success

    async def callback(self, interaction: discord.Interaction):
        if not await _check_access(interaction, self.invoker_id, self.guild_id):
            return
        # The modal must be the first response (3s window); the unlocked
        # check happens again in on_submit, which is what gates the write.
        try:
            await interaction.response.send_modal(CardBackgroundModal(self.guild_id, self.clone_id, self.invoker_id))
        except discord.HTTPException as e:
            if e.code != 40060:  # double-click: modal already open from the first tap
                raise


class CardClearBackgroundButton(_Btn, discord.ui.DynamicItem[discord.ui.Button], template=_id_pattern("clearbg")):
    FIELD, LABEL = "clearbg", "🗑️ Clear background"

    async def callback(self, interaction: discord.Interaction):
        if not await _check_access(interaction, self.invoker_id, self.guild_id):
            return
        await interaction.response.defer()
        cfg = await db.get_welcome_config(self.guild_id, clone_id=self.clone_id)
        if not cfg.get("ultra_pack_unlocked"):
            d = _get_draft(self.guild_id, self.clone_id, interaction.user.id)
            if d:
                d["bg"] = None
            await _rerender(interaction, self.guild_id, self.clone_id, self.invoker_id)
            return
        await db.set_welcome_config(
            self.guild_id, clone_id=self.clone_id,
            custom_background_url=None, custom_bg_channel_id=None, custom_bg_message_id=None,
        )
        await _rerender(interaction, self.guild_id, self.clone_id, self.invoker_id)


class CardUnlockButton(_Btn, discord.ui.DynamicItem[discord.ui.Button], template=_id_pattern("unlock")):
    FIELD, STYLE = "unlock", discord.ButtonStyle.success

    def label_text(self, *_):
        return f"🔒 Unlock Customize Card (${bot_config.ULTRA_PACK_FEE_USD:g})"

    async def callback(self, interaction: discord.Interaction):
        if not await _check_access(interaction, self.invoker_id, self.guild_id):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        cfg = await db.get_welcome_config(self.guild_id, clone_id=self.clone_id)
        if cfg.get("ultra_pack_unlocked"):
            await interaction.followup.send("✅ Already unlocked — tap **Customize Card** again to open the editor.", ephemeral=True)
            return
        from discord_bot.views_card_pack import start_ultra_pack_payment
        # Pass the guild explicitly: this button can be pressed from a DM
        # copy of the wizard, where interaction.guild_id is None and the
        # payment row would be logged with chat_id NULL (unlock then fails).
        await start_ultra_pack_payment(interaction, guild_id=self.guild_id)


# ── heading / member number / reset ───────────────────────────────────────

class CardHeadingModal(discord.ui.Modal, title="Card heading text"):
    def __init__(self, guild_id: int, clone_id, invoker_id, current: str):
        super().__init__()
        self.guild_id = guild_id
        self.clone_id = clone_id
        self.invoker_id = invoker_id
        self.heading = discord.ui.TextInput(
            label="Heading ({guild}/{member}; blank=default)",
            style=discord.TextStyle.short, required=False, max_length=ULTRA_HEADING_MAX,
            default=current or "", placeholder="Welcome to {guild}!",
        )
        self.add_item(self.heading)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        await _save_option(self.guild_id, self.clone_id, interaction.user.id, heading=str(self.heading.value or "").strip())
        await _rerender(interaction, self.guild_id, self.clone_id, self.invoker_id)


class CardHeadingButton(_Btn, discord.ui.DynamicItem[discord.ui.Button], template=_id_pattern("heading")):
    FIELD, LABEL = "heading", "✍️ Heading text"

    async def callback(self, interaction: discord.Interaction):
        if not await _check_access(interaction, self.invoker_id, self.guild_id):
            return
        cfg = await vw._get_config_for_modal(self.guild_id, self.clone_id)
        cfg = _effective_config(cfg, self.guild_id, self.clone_id, interaction.user.id)
        current = parse_ultra_options(cfg.get("ultra_card_json"))["heading"]
        try:
            await interaction.response.send_modal(CardHeadingModal(self.guild_id, self.clone_id, self.invoker_id, current))
        except discord.HTTPException as e:
            if e.code != 40060:
                raise


class CardNumberToggleButton(_Btn, discord.ui.DynamicItem[discord.ui.Button], template=_id_pattern("number")):
    FIELD = "number"

    def __init__(self, guild_id: int, clone_id, invoker_id, shown: bool = True):
        self._shown = shown
        super().__init__(guild_id, clone_id, invoker_id, shown)

    def label_text(self, shown=True, *_):
        return "🔢 Member #: on" if shown else "🔢 Member #: off"

    async def callback(self, interaction: discord.Interaction):
        if not await _check_access(interaction, self.invoker_id, self.guild_id):
            return
        await interaction.response.defer()
        cfg = await db.get_welcome_config(self.guild_id, clone_id=self.clone_id)
        cfg = _effective_config(cfg, self.guild_id, self.clone_id, interaction.user.id)
        current = parse_ultra_options(cfg.get("ultra_card_json"))["show_number"]
        await _save_option(self.guild_id, self.clone_id, interaction.user.id, show_number=not current)
        await _rerender(interaction, self.guild_id, self.clone_id, self.invoker_id)


class CardResetButton(_Btn, discord.ui.DynamicItem[discord.ui.Button], template=_id_pattern("reset")):
    FIELD, LABEL = "reset", "↩️ Reset layout"

    async def callback(self, interaction: discord.Interaction):
        if not await _check_access(interaction, self.invoker_id, self.guild_id):
            return
        await interaction.response.defer()
        cfg = await db.get_welcome_config(self.guild_id, clone_id=self.clone_id)
        if not cfg.get("ultra_pack_unlocked"):
            _DRAFTS.pop(_draft_key(self.guild_id, self.clone_id, interaction.user.id), None)
            await _rerender(interaction, self.guild_id, self.clone_id, self.invoker_id)
            return
        await db.set_welcome_config(self.guild_id, clone_id=self.clone_id, ultra_card_json=None)
        await _rerender(interaction, self.guild_id, self.clone_id, self.invoker_id)


# ── preview / done ────────────────────────────────────────────────────────

def _sample_backdrop() -> bytes:
    """Stand-in image for previews before a background has been set."""
    img = Image.new("RGB", (TEMPLATE_WIDTH, TEMPLATE_HEIGHT))
    d = ImageDraw.Draw(img)
    for y in range(TEMPLATE_HEIGHT):
        t = y / TEMPLATE_HEIGHT
        d.line([(0, y), (TEMPLATE_WIDTH, y)], fill=(int(70 + 90 * t), int(90 + 40 * t), int(200 - 80 * t)))
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


class CardPreviewButton(_Btn, discord.ui.DynamicItem[discord.ui.Button], template=_id_pattern("preview")):
    """Kept so wizards posted BEFORE the TV existed keep working. The new wizard has no Preview button.
    No cooldown."""
    FIELD, LABEL, STYLE = "preview", "\U0001F441\uFE0F Preview", discord.ButtonStyle.primary

    async def callback(self, interaction: discord.Interaction):
        if not await _check_access(interaction, self.invoker_id, self.guild_id):
            return
        await interaction.response.defer(ephemeral=True)
        try:
            real_cfg = await db.get_welcome_config(self.guild_id, clone_id=self.clone_id)
            unlocked = bool(real_cfg.get("ultra_pack_unlocked"))
            png, note = await _render_tv(interaction.client, interaction.guild or interaction.client.get_guild(self.guild_id),
                                         self.guild_id, self.clone_id, interaction.user)
            await interaction.followup.send(
                content=f"**Preview** ({note.lower()}) \u2014 only visible to you" + ("" if unlocked else " \u2014 unlock to save & remove the watermark"),
                file=discord.File(fp=io.BytesIO(png), filename="preview.png"),
                ephemeral=True,
            )
        except Exception as e:
            logger.warning(f"[cardwz] preview failed for guild {self.guild_id}: {e}")
            await interaction.followup.send(f"Couldn't render a preview: {e}", ephemeral=True)


class CardDoneButton(_Btn, discord.ui.DynamicItem[discord.ui.Button], template=_id_pattern("done")):
    FIELD, LABEL = "done", "✅ Done"

    async def callback(self, interaction: discord.Interaction):
        if not await _check_access(interaction, self.invoker_id, self.guild_id):
            return
        await interaction.response.defer()
        done = discord.ui.LayoutView(timeout=None)
        done.add_item(discord.ui.Container(
            discord.ui.TextDisplay("✅ **Card customization saved.** Your next welcome card will use these settings."),
            accent_colour=discord.Color.green(),
        ))
        await interaction.edit_original_response(view=done)


class CardTabButton(discord.ui.DynamicItem[discord.ui.Button], template=r"^cardwz_tab(bg|layout|text):(\d+):(-|\d+):(-|\d+)$"):
    """Switches which group of controls shows under the TV. Does not redraw the card."""

    def __init__(self, tab: str, guild_id: int, clone_id, invoker_id, active: bool = False):
        self.tab = tab
        self.guild_id = guild_id
        self.clone_id = clone_id
        self.invoker_id = invoker_id
        super().__init__(discord.ui.Button(
            label=_TAB_LABELS[tab],
            style=discord.ButtonStyle.primary if active else discord.ButtonStyle.secondary,
            custom_id=_encode("tab" + tab, guild_id, clone_id, invoker_id),
        ))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match.group(1), int(match.group(2)),
                   None if match.group(3) == "-" else int(match.group(3)),
                   None if match.group(4) == "-" else int(match.group(4)))

    async def callback(self, interaction: discord.Interaction):
        if not await _check_access(interaction, self.invoker_id, self.guild_id):
            return
        await interaction.response.defer()
        key = _draft_key(self.guild_id, self.clone_id, interaction.user.id)
        _set_tab(key, self.tab)
        config = await db.get_welcome_config(self.guild_id, clone_id=self.clone_id)
        msg = interaction.message
        has_tv = bool(msg and any(a.filename == "card.png" for a in msg.attachments))
        await interaction.edit_original_response(view=build_customize_view(
            self.guild_id, self.clone_id, self.invoker_id, config, tab=self.tab, has_tv=has_tv, note=_TV_NOTES.get(key)))


DYNAMIC_ITEMS = (
    CardBannerSelect, CardDimSelect, CardSideSelect, CardColorSelect, CardShapeSelect,
    CardBackgroundButton, CardClearBackgroundButton, CardUnlockButton,
    CardHeadingButton, CardNumberToggleButton, CardResetButton, CardPreviewButton, CardDoneButton,
    CardTabButton,
)
