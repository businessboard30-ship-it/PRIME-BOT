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

Welcome Card Studio, Phase 1: the three tabs became FOUR numbered steps (1 Pictures, 2 Look, 3 Text, 4 Send). Each\nstep shows at most 3 controls ("Easy"); "More controls" reveals that step's extra ones. Every step has Undo and\n"Reset this step"; step 4 has "Send me a test". The internal step keys stay bg/layout/text (+ send) so wizards posted\nbefore this change keep working. No new card features, the stored options and the renderer are untouched.\n\nA "Style presets" dropdown above the tabs sets banner / darkness / avatar side / text colour / member # /
avatar shape in one tap (heading and background are never touched); then fine-tune on the tabs.
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
import secrets
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
    ULTRA_AVATAR_SIDES, ULTRA_FONTS, ULTRA_OUTLINES, ULTRA_BRIGHTNESS, ULTRA_BANNERS, ULTRA_DIM_ALPHA, ULTRA_HEADING_MAX, ULTRA_NUDGE_LIMIT, ULTRA_TEXT_COLORS,
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


_STUDIO_PREVIEW: dict = {}   # (guild, clone, user) -> {studio option: value} for Ultra-only servers: preview, never stored


def _studio_overlay(config: dict, guild_id: int, clone_id, user_id) -> dict:
    """Ultra-only servers (not Premium/Plus) can try studio options in the editor; they live in memory only."""
    pv = _STUDIO_PREVIEW.get(_draft_key(guild_id, clone_id, user_id)) if user_id is not None else None
    if not pv:
        return config
    cur = parse_ultra_options(config.get("ultra_card_json"))
    cur.update(pv)
    return dict(config, ultra_card_json=json.dumps(cur))


def _effective_config(config: dict, guild_id: int, clone_id, user_id) -> dict:
    """Config as the editor should display/preview it: the DB row for
    unlocked servers, DB row overlaid with the user's draft otherwise."""
    if config.get("ultra_pack_unlocked"):
        return _studio_overlay(config, guild_id, clone_id, user_id)
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


# ── undo (look options only; the picture is not part of it) ───────────────

_UNDO_MAX = 10
_UNDO: dict = {}             # (guild, clone, user) -> list of (ultra_card_json str|None, avatar_shape), newest last


def _push_undo(guild_id: int, clone_id, user_id, cfg: dict) -> None:
    """Remember the look as it is NOW (what the editor shows: the draft for unpaid servers, the DB row otherwise)."""
    if user_id is None:
        return
    eff = _effective_config(cfg, guild_id, clone_id, user_id)
    snap = (eff.get("ultra_card_json"), eff.get("avatar_shape", "circle"))
    stack = _UNDO.setdefault(_draft_key(guild_id, clone_id, user_id), [])
    stack.append(snap)
    del stack[:-_UNDO_MAX]
    _trim(_UNDO, _STATE_MAX)


async def _undo_last(guild_id: int, clone_id, user_id) -> bool:
    """Put the look back one step. False when there is nothing to undo. Gated server-side like every write."""
    stack = _UNDO.get(_draft_key(guild_id, clone_id, user_id))
    if not stack:
        return False
    raw, shape = stack.pop()
    cfg = await db.get_welcome_config(guild_id, clone_id=clone_id)
    if not cfg.get("ultra_pack_unlocked"):
        d = _get_draft(guild_id, clone_id, user_id, create=True)
        d["opts"] = parse_ultra_options(raw)
        d["shape"] = shape or "circle"
        return True
    from modules.welcome_studio import STUDIO_KEYS, studio_unlocked
    if not await studio_unlocked(guild_id, clone_id):
        # the snapshot may carry preview-only studio values: put them back in memory, never in the database
        parsed = parse_ultra_options(raw)
        key = _draft_key(guild_id, clone_id, user_id)
        _STUDIO_PREVIEW[key] = {k: parsed[k] for k in STUDIO_KEYS}
        stored = parse_ultra_options(cfg.get("ultra_card_json"))
        parsed.update({k: stored[k] for k in STUDIO_KEYS})
        raw = json.dumps(parsed)
    await db.set_welcome_config(guild_id, clone_id=clone_id, ultra_card_json=raw, avatar_shape=shape or "circle")
    return True


# ── option storage ────────────────────────────────────────────────────────

async def _save_option(guild_id: int, clone_id, user_id=None, **changes) -> None:
    cfg = await db.get_welcome_config(guild_id, clone_id=clone_id)
    _push_undo(guild_id, clone_id, user_id, cfg)
    if not cfg.get("ultra_pack_unlocked"):
        d = _get_draft(guild_id, clone_id, user_id, create=True)
        d["opts"].update(changes)
        return
    changes = await _gate_studio_changes(guild_id, clone_id, user_id, changes)
    if not changes:
        return
    opts = parse_ultra_options(cfg.get("ultra_card_json"))
    opts.update(changes)
    await db.set_welcome_config(guild_id, clone_id=clone_id, ultra_card_json=json.dumps(opts))


async def _gate_studio_changes(guild_id: int, clone_id, user_id, changes: dict) -> dict:
    """Studio keys (zoom, pan, text size) are saved only for Premium/Plus (checked here, on the server). An Ultra-only
    server keeps them in memory so the preview still shows them. Returns the changes that go to the database."""
    from modules.welcome_studio import STUDIO_KEYS, studio_unlocked
    studio = {k: v for k, v in changes.items() if k in STUDIO_KEYS}
    if not studio:
        return changes
    key = _draft_key(guild_id, clone_id, user_id) if user_id is not None else None
    if await studio_unlocked(guild_id, clone_id):
        if key is not None and key in _STUDIO_PREVIEW:
            for k in studio:
                _STUDIO_PREVIEW[key].pop(k, None)
        return changes
    if key is not None:
        _STUDIO_PREVIEW.setdefault(key, {}).update(studio)
        _trim(_STUDIO_PREVIEW, _STATE_MAX)
    return {k: v for k, v in changes.items() if k not in STUDIO_KEYS}


# ── rendering the wizard ──────────────────────────────────────────────────

_BANNER_LABELS = {"bottom": "Bottom banner (classic)", "top": "Top banner", "none": "No banner (text over image)"}
_DIM_LABELS = {"light": "Light — see more of your image", "medium": "Medium (default)", "heavy": "Heavy — best text contrast"}
_SIDE_LABELS = {"left": "Avatar on the left (classic)", "right": "Avatar on the right"}
_LAYOUT_LABELS = {"banner": "Banner layout (classic)", "centered": "Centered — big avatar in the middle"}
_FONT_LABELS = {"classic": "Classic font (default)", "clean": "Clean — Poppins", "tall": "Tall — Bebas Neue", "script": "Handwritten — Pacifico",
                "tech": "Tech — Orbitron", "elegant": "Elegant — Cinzel", "pixel": "Pixel — Press Start 2P",
                "gothic": "Gothic — Pirata One", "comic": "Comic — Bangers", "horror": "Horror — Creepster",
                "round": "Rounded — Righteous", "cursive": "Cursive — Lobster", "poster": "Poster — Anton",
                "wide": "Wide — Audiowide", "marquee": "Marquee — Monoton"}
_BRACKET_LABELS = {"none": "No brackets (default)", "square": "[ Name ] square", "round": "( Name ) round",
                   "curly": "{ Name } curly", "angle": "< Name > angle", "guillemet": "\u00ab Name \u00bb guillemets",
                   "slashes": "// Name // slashes", "stars": "* Name * stars"}
_FOCUS_LABELS = {"auto": "Crop: smart (keeps the busiest part)", "center": "Crop: center (default)", "top": "Crop: keep the top", "bottom": "Crop: keep the bottom",
                 "left": "Crop: keep the left", "right": "Crop: keep the right"}
_RATIO_LABELS = {"auto": "Card shape: Auto (match my image)", "3:2": "Card shape: 3:2 classic (default)", "16:9": "Card shape: 16:9 widescreen", "2:1": "Card shape: 2:1 wide",
                 "3:1": "Card shape: 3:1 strip (slim)", "4:3": "Card shape: 4:3 tall", "1:1": "Card shape: 1:1 square"}
_TEXT_POS_LABELS = {"auto": "Automatic", "top-left": "Top left", "top": "Top middle", "top-right": "Top right",
                    "left": "Middle left", "center": "Center", "right": "Middle right",
                    "bottom-left": "Bottom left", "bottom": "Bottom middle", "bottom-right": "Bottom right"}
_COLOR_LABELS = {"white": "White (default)", "gold": "Gold", "cyan": "Cyan", "pink": "Pink", "green": "Green", "red": "Red", "avatar": "Match my avatar (each member differs)"}


# -- style presets: one tap sets the look, then fine-tune on the tabs -----------
# Never touches the heading or the background. Every value must be one parse_ultra_options accepts
# (a test checks this), and `shape` one of the avatar frame shapes.

STYLE_PRESETS = {
    "classic": {"label": "Classic", "desc": "The default look: bottom banner, avatar left, white text.",
                "opts": {"banner": "bottom", "dim": "medium", "avatar_side": "left", "text_color": "white", "show_number": True},
                "shape": "circle"},
    "minimal": {"label": "Minimal", "desc": "Your image shows through; text sits right on it, no member number.",
                "opts": {"banner": "none", "dim": "light", "avatar_side": "left", "text_color": "white", "show_number": False},
                "shape": "circle"},
    "bold": {"label": "Bold", "desc": "Top banner, heavy shade, gold text, hexagon avatar on the right.",
             "opts": {"banner": "top", "dim": "heavy", "avatar_side": "right", "text_color": "gold", "show_number": True},
             "shape": "hexagon"},
    "neon": {"label": "Neon", "desc": "Dark and sharp: cyan text over a heavy shade, diamond avatar.",
             "opts": {"banner": "none", "dim": "heavy", "avatar_side": "left", "text_color": "cyan", "show_number": True},
             "shape": "diamond"},
    "sunset": {"label": "Sunset", "desc": "Soft pink text, rounded avatar on the right, bottom banner.",
               "opts": {"banner": "bottom", "dim": "medium", "avatar_side": "right", "text_color": "pink", "show_number": True},
               "shape": "rounded_square"},
    "ember": {"label": "Ember", "desc": "Strong red text under a top banner, square avatar.",
              "opts": {"banner": "top", "dim": "heavy", "avatar_side": "left", "text_color": "red", "show_number": True},
              "shape": "square"},
}


async def _apply_style(guild_id: int, clone_id, user_id, opts: dict, shape) -> None:
    """Apply a preset (or a step reset) in ONE write. `shape` None leaves the avatar shape alone. Unpaid servers only
    touch the in-memory draft (same gate as every other edit). Pushes an undo snapshot first."""
    cfg = await db.get_welcome_config(guild_id, clone_id=clone_id)
    _push_undo(guild_id, clone_id, user_id, cfg)
    if not cfg.get("ultra_pack_unlocked"):
        d = _get_draft(guild_id, clone_id, user_id, create=True)
        d["opts"].update(opts)
        if shape is not None:
            d["shape"] = shape
        return
    opts = await _gate_studio_changes(guild_id, clone_id, user_id, opts)
    cur = parse_ultra_options(cfg.get("ultra_card_json"))
    cur.update(opts)
    changes = {"ultra_card_json": json.dumps(cur)}
    if shape is not None:
        changes["avatar_shape"] = shape
    await db.set_welcome_config(guild_id, clone_id=clone_id, **changes)


# -- the TV: live preview drawn inside the wizard message ---------------------

_TABS = ("bg", "layout", "text", "send")     # steps 1-4; the keys stay as they were so old wizard messages keep working
_TAB_LABELS = {"bg": "1 \U0001F5BC\uFE0F Pictures", "layout": "2 \U0001F4D0 Look", "text": "3 \U0001F524 Text",
               "send": "4 \U0001F4E8 Send"}
_STEP_HINTS = {
    "bg": "**Step 1 of 4 · Pictures** — add your picture and choose the card shape.",
    "layout": "**Step 2 of 4 · Look** — where the banner and avatar sit.",
    "text": "**Step 3 of 4 · Text** — color, font and wording.",
    "send": "**Step 4 of 4 · Send** — send yourself a test, then tap Done.",
}
# What "Reset this step" puts back to default (the picture is never touched). Shape lives in its own column.
_STEP_RESET_FIELDS = {
    "bg": ("ratio", "dim", "focus"),
    "layout": ("layout", "banner", "avatar_side", "zoom", "pan_x", "pan_y", "frame", "frame_color", "avatar_pos", "effect", "brightness"),
    "text": ("text_color", "font", "bracket", "heading", "show_number", "ring", "soft_edge", "shadow", "big_name", "glass", "auto_contrast", "text_size", "text_dx", "text_dy", "heading_font", "text_outline", "text_pos"),
}
_STEP_RESET_SHAPE = {"layout": "circle"}
_MORE_STATE: dict = {}       # (guild, clone, user) -> (set of steps showing "More controls", monotonic ts)
_TEST_LAST: dict = {}        # same key -> monotonic time of the last "Send me a test"
_TEST_COOLDOWN = 5.0
_TAB_TTL = 30 * 60
_STATE_MAX = 1000
_TAB_STATE: dict = {}        # (guild, clone, user) -> (tab, monotonic ts)
_TV_STATE: dict = {}         # same key -> filename of the picture now on the editor message (None = no picture)
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


def _more_open(key, tab: str) -> bool:
    st = _MORE_STATE.get(key)
    if st is None or time.monotonic() - st[1] > _TAB_TTL:
        return False
    return tab in st[0]


def _toggle_more(key, tab: str) -> bool:
    """Flip "More controls" for ONE step. Returns the new state."""
    st = _MORE_STATE.get(key)
    steps = set(st[0]) if st is not None and time.monotonic() - st[1] <= _TAB_TTL else set()
    now_open = tab not in steps
    steps.symmetric_difference_update({tab})
    _MORE_STATE[key] = (steps, time.monotonic())
    _trim(_MORE_STATE, _STATE_MAX)
    return now_open


def _caption(unlocked: bool, note, tv_ok: bool, config=None) -> str:
    lines = ["### \U0001F3A8 Customize your welcome card"]
    config = config or {}
    if config.get("ultra_trial_active"):
        ends = config.get("ultra_trial_ends_at")
        when = f"<t:{int(ends.timestamp())}:R>" if ends else "soon"
        lines.append(
            f"\U0001F381 **Free 5-day trial active** \u2014 everything below is saved and live on every join until it ends {when}. "
            f"After that your server falls back to its normal welcome card (your settings are kept). "
            f"Unlock for ${bot_config.ULTRA_PACK_FEE_USD:g} to keep it for good."
        )
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


_ROTATION_DRAFT: dict = {}   # (guild, clone, user) -> rotation mode picked in preview by a server without Premium (memory only, never saved)


async def _with_studio(config: dict, guild_id: int, clone_id, user_id=None) -> dict:
    """Phase 2c: add a small read-only `_studio` summary (Premium?, picture count, rotation, slots needing a picture)
    so the sync view builder can show the gallery line. Never raises; on any error the Pictures step looks as before."""
    try:
        from modules import welcome_gallery as wg
        from modules.welcome_studio import studio_unlocked
        if not await studio_unlocked(guild_id, clone_id):
            mode = wg.clean_mode(_ROTATION_DRAFT.get(_draft_key(guild_id, clone_id, user_id)))
            return dict(config, _studio={"unlocked": False, "mode": mode})
        rows = await db.gallery_list(guild_id, clone_id)
        return dict(config, _studio={
            "unlocked": True, "pictures": len(rows),
            "needs": [r["slot"] for r in rows if r.get("needs_new_picture")],
            "slots": [r["slot"] for r in rows],
            "mode": wg.clean_mode(config.get("rotation_mode")),
            **await _design_summary(guild_id, clone_id),                          # studio 5b + 7b: designs, schedules, live today
        })
    except Exception:
        logger.exception("[cardwz] studio summary failed for guild %s", guild_id)
        return config


async def _design_summary(guild_id: int, clone_id) -> dict:
    """Saved designs (id, name, schedule in words) plus the name of the scheduled design that is live today, if any."""
    from modules import welcome_schedule as sch
    from modules.welcome_studio import _today_utc
    rows = await db.design_list(guild_id, clone_id)
    live = sch.pick_scheduled(rows, _today_utc())
    return {"designs": [{"id": d["id"], "name": d["name"], "when": sch.describe(d)} for d in rows],
            "live_now": live["name"] if live else None}


def _gallery_line(studio: dict | None) -> str | None:
    """One plain line under the Pictures hint. None when there is nothing to say (summary missing)."""
    from modules import welcome_gallery as wg
    from modules.welcome_studio import LOCKED_LINE
    if not studio:
        return None
    if not studio.get("unlocked"):
        return (f"-# \U0001F512 Preview only: {LOCKED_LINE} Try it here, it saves with Premium.\n"
                f"-# \U0001F4DA Change picture: {wg.MODE_LABELS[wg.clean_mode(studio.get('mode'))]}")
    n = int(studio.get("pictures") or 0)
    line = f"-# \U0001F4DA Gallery: {n} of {wg.MAX_PICTURES} pictures · {wg.MODE_LABELS[wg.clean_mode(studio.get('mode'))]}"
    needs = studio.get("needs") or []
    slots = studio.get("slots")
    if slots:
        have = {int(x) for x in slots}
        bad = {int(x) for x in needs}
        # Numbered strip: 1 to 10, filled / empty / needs a new picture. Plain symbols, no jargon.
        warn, full, empty = "\u26A0\uFE0F", "\u25CF", "\u25CB"
        strip = " ".join(str(n) + (warn if n in bad else (full if n in have else empty))
                         for n in range(1, wg.MAX_PICTURES + 1))
        line += f"\n-# {strip}  (\u25CF has a picture, \u25CB empty)"
    if needs:
        line += "\n-# \u26A0\uFE0F Picture " + ", ".join(str(x) for x in needs) + " needs a new picture."
    return line


def build_customize_view(guild_id: int, clone_id, invoker_id, config: dict, tab: str = "bg",
                         has_tv: bool = False, note=None, tv_name: str = "card.png",
                         more: bool = False) -> discord.ui.LayoutView:
    unlocked = bool(config.get("ultra_pack_unlocked"))
    raw_config = config
    config = _effective_config(config, guild_id, clone_id, invoker_id)
    tab = tab if tab in _TABS else "bg"

    opts = parse_ultra_options(config.get("ultra_card_json"))
    has_bg = bool(config.get("custom_background_url"))

    # The Text step with More controls is the fullest screen (Discord allows 40 components, we keep it under 40). The two
    # plain divider lines are the only thing there that does no work, so they are the first thing dropped (3d + 4c + 4b).
    crowded = tab == "text" and more
    view = discord.ui.LayoutView(timeout=None)
    container = discord.ui.Container(accent_colour=discord.Color.blurple())
    container.add_item(discord.ui.TextDisplay(_caption(unlocked, note, has_tv, raw_config)))
    if has_tv:
        container.add_item(discord.ui.MediaGallery(discord.MediaGalleryItem(f"attachment://{tv_name}")))
    if not crowded:
        container.add_item(discord.ui.Separator())

    preset_row = discord.ui.ActionRow()
    preset_row.add_item(CardPresetSelect(guild_id, clone_id, invoker_id))
    container.add_item(preset_row)

    tabs = discord.ui.ActionRow()
    for t in _TABS:
        tabs.add_item(CardTabButton(t, guild_id, clone_id, invoker_id, active=(t == tab)))
    container.add_item(tabs)

    def _select_row(select_cls):
        row = discord.ui.ActionRow()
        row.add_item(select_cls(guild_id, clone_id, invoker_id, config))
        container.add_item(row)

    hint = _STEP_HINTS[tab]
    if tab == "layout" and more:   # fold the picture-move line into the hint: frees one component for brightness (4d)
        hint += " Arrows move the picture inside the card; the magnifier zooms."
    if tab == "text" and more:   # fold the nudge line into the hint: frees one component for the outline button (4c)
        hint += " Spot and arrows move the text; it stops at the card edge and next to your avatar."
    container.add_item(discord.ui.TextDisplay(f"-# {hint}"))

    # Easy mode shows at most 3 controls per step; "More controls" adds that step's extra ones (never more than 5 selects).
    if tab == "bg":
        gline = _gallery_line(raw_config.get("_studio"))
        if gline:
            container.add_item(discord.ui.TextDisplay(gline))
        bg_row = discord.ui.ActionRow()
        bg_row.add_item(CardBackgroundButton(guild_id, clone_id, invoker_id))
        if has_bg:
            bg_row.add_item(CardClearBackgroundButton(guild_id, clone_id, invoker_id))
        studio = raw_config.get("_studio")
        if studio is not None and not studio.get("unlocked"):
            bg_row.add_item(CardStudioPremiumButton(guild_id, clone_id, invoker_id))
        container.add_item(bg_row)
        extra = ()
        if more:
            extra = (CardFocusSelect, CardRotationSelect)
        for select_cls in (CardRatioSelect, CardDimSelect) + extra:
            _select_row(select_cls)
        if more and studio is not None:
            manage_row = discord.ui.ActionRow()
            manage_row.add_item(CardPictureAddButton(guild_id, clone_id, invoker_id))
            if not studio.get("unlocked") or int(studio.get("pictures") or 0) > 0:
                manage_row.add_item(CardPictureReplaceButton(guild_id, clone_id, invoker_id))
                manage_row.add_item(CardPictureRemoveButton(guild_id, clone_id, invoker_id))
                manage_row.add_item(CardPictureFirstButton(guild_id, clone_id, invoker_id))
            container.add_item(manage_row)
    elif tab == "layout":
        for select_cls in (CardLayoutSelect, CardBannerSelect, CardSideSelect) + ((CardShapeSelect,) if more else ()):
            _select_row(select_cls)
        if more:   # Phase 3a: move and zoom the picture inside the card (arrows, never typed numbers)
            frame_row = discord.ui.ActionRow()
            frame_row.add_item(CardPanLeftButton(guild_id, clone_id, invoker_id))
            frame_row.add_item(CardPanUpButton(guild_id, clone_id, invoker_id))
            frame_row.add_item(CardPanDownButton(guild_id, clone_id, invoker_id))
            frame_row.add_item(CardPanRightButton(guild_id, clone_id, invoker_id))
            frame_row.add_item(CardZoomButton(guild_id, clone_id, invoker_id, opts["zoom"]))
            container.add_item(frame_row)
            border_row = discord.ui.ActionRow()
            border_row.add_item(CardFrameButton(guild_id, clone_id, invoker_id, opts["frame"]))
            border_row.add_item(CardAvatarPosButton(guild_id, clone_id, invoker_id, opts["avatar_pos"]))   # studio 3e
            border_row.add_item(CardEffectButton(guild_id, clone_id, invoker_id, opts["effect"]))          # studio 4a
            border_row.add_item(CardBrightnessButton(guild_id, clone_id, invoker_id, opts["brightness"]))  # studio 4d
            if opts["frame"] != "none":
                border_row.add_item(CardFrameColorButton(guild_id, clone_id, invoker_id, opts["frame_color"]))
            container.add_item(border_row)
    elif tab == "text":
        for select_cls in (CardColorSelect, CardFontSelect) + ((CardBracketSelect, CardStyleSelect) if more else ()):
            _select_row(select_cls)
        text_row = discord.ui.ActionRow()
        text_row.add_item(CardHeadingButton(guild_id, clone_id, invoker_id))
        text_row.add_item(CardNumberToggleButton(guild_id, clone_id, invoker_id, opts["show_number"]))
        if more:
            text_row.add_item(CardTextSizeButton(guild_id, clone_id, invoker_id, opts["text_size"]))
            text_row.add_item(CardHeadingFontButton(guild_id, clone_id, invoker_id, opts["heading_font"]))   # studio 4b
            text_row.add_item(CardOutlineButton(guild_id, clone_id, invoker_id, opts["text_outline"]))     # studio 4c
        container.add_item(text_row)
        if more:   # studio 3d: the text spot (nine cells). Own row: the buttons above already fill a row (5 is Discord's limit)
            spot_row = discord.ui.ActionRow()
            spot_row.add_item(CardTextPosButton(guild_id, clone_id, invoker_id, opts["text_pos"]))
            container.add_item(spot_row)
        if more:   # Phase 3c: nudge the text block (arrows, never typed numbers); the guard keeps it on the card
            nudge_row = discord.ui.ActionRow()
            for cls in (CardNudgeLeftButton, CardNudgeUpButton, CardNudgeDownButton, CardNudgeRightButton, CardNudgeCenterButton):
                nudge_row.add_item(cls(guild_id, clone_id, invoker_id))
            container.add_item(nudge_row)
    else:
        container.add_item(discord.ui.TextDisplay(
            "Your card goes out the way your **/welcome** setup says (channel or DM). "
            "**Send me a test** shows you the real, full-size card, only you see it."))
        # Studio 5b: saved designs (the look only, never the pictures). Open to everyone as a preview; saving is Premium.
        dstudio = raw_config.get("_studio")
        dline = _designs_line(dstudio)
        if dline:
            container.add_item(discord.ui.TextDisplay(dline))
        designs = (dstudio or {}).get("designs") or []
        if designs:
            drow = discord.ui.ActionRow()
            drow.add_item(CardDesignSelect(guild_id, clone_id, invoker_id, designs))
            container.add_item(drow)
        if dstudio is not None:
            dbtn = discord.ui.ActionRow()
            dbtn.add_item(CardDesignSaveButton(guild_id, clone_id, invoker_id))
            if designs:
                dbtn.add_item(CardDesignRenameButton(guild_id, clone_id, invoker_id))
                dbtn.add_item(CardDesignScheduleButton(guild_id, clone_id, invoker_id))
                dbtn.add_item(CardDesignDeleteButton(guild_id, clone_id, invoker_id))
            container.add_item(dbtn)

    if not crowded:
        container.add_item(discord.ui.Separator())
    act_row = discord.ui.ActionRow()
    if tab == "send":
        act_row.add_item(CardTestSendButton(guild_id, clone_id, invoker_id))
        act_row.add_item(CardResetButton(guild_id, clone_id, invoker_id))
    else:
        act_row.add_item(CardUndoButton(guild_id, clone_id, invoker_id))
        act_row.add_item(CardStepResetButton(guild_id, clone_id, invoker_id))
        act_row.add_item(CardMoreButton(guild_id, clone_id, invoker_id, more))
    if unlocked:
        act_row.add_item(CardDoneButton(guild_id, clone_id, invoker_id))
        if raw_config.get("ultra_trial_active"):
            act_row.add_item(CardUnlockButton(guild_id, clone_id, invoker_id))
    else:
        act_row.add_item(CardUnlockButton(guild_id, clone_id, invoker_id))
    container.add_item(act_row)

    view.add_item(container)
    return view


def _tv_finish(card_bytes: bytes, watermark: bool, full: bool = False) -> bytes:
    """Downscaled PNG copy for the TV (the card the bot posts on joins is never touched). `full` keeps the real size."""
    img = Image.open(io.BytesIO(card_bytes)).convert("RGB")
    if img.width > _TV_WIDTH and not full:
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


async def _gallery_preview_bytes(session, client, guild_id: int, clone_id):
    """Phase 2e: the editor preview shows gallery picture 1 for Premium/Plus servers (the real join rotates; the preview
    stays stable so the admin can judge the look). None for everyone else, for an empty gallery, or on any error, so the
    old single picture path runs exactly as before. Never raises."""
    try:
        from modules.welcome_studio import studio_unlocked
        if client is None or not await studio_unlocked(guild_id, clone_id):
            return None
        from discord_bot.cogs.welcome import _gallery_slot_url, _fetch_custom_bg_bytes
        rows = [r for r in await db.gallery_list(guild_id, clone_id) if not r.get("needs_new_picture")]
        for row in rows[:3]:
            key = f"gal:{guild_id}:{clone_id}:{row.get('message_id')}"
            hit = _BG_CACHE.get(key)
            if hit is not None and time.monotonic() - hit[0] < _BG_TTL:
                return hit[1]
            url, gone = await _gallery_slot_url(client, row)
            if gone or not url:
                continue
            data, _why = await _fetch_custom_bg_bytes(session, url)
            if data is not None:
                _BG_CACHE[key] = (time.monotonic(), data)
                _trim(_BG_CACHE, _BG_MAX)
                return data
        return None
    except Exception:
        logger.exception("[cardwz] gallery preview failed for guild %s", guild_id)
        return None


async def _render_tv(client, guild, guild_id: int, clone_id, user, full: bool = False):
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
            bg_bytes = await _gallery_preview_bytes(session, client, guild_id, clone_id)
            url = str(cfg.get("custom_background_url") or "")
            hit = _BG_CACHE.get(url)
            if bg_bytes is not None:
                pass   # Phase 2e: Premium/Plus servers see picture 1 of the gallery, like the real welcome
            elif hit is not None and time.monotonic() - hit[0] < _BG_TTL:
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
    png = await asyncio.to_thread(_tv_finish, card_bytes, not unlocked, full)
    note = "Sample backdrop \u2014 tap Upload image to use your own" if using_sample else "Your background"
    return png, note


def _new_tv_name() -> str:
    """A fresh file name for every picture. Re-uploading a second file called card.png while the first is still on the
    message makes the picture blink out; a unique name per render can't be confused with the one being replaced."""
    return f"card-{secrets.token_hex(3)}.png"


def _tv_file(png: bytes, name: str = "card.png") -> discord.File:
    return discord.File(fp=io.BytesIO(png), filename=name)


async def open_customize_wizard(interaction: discord.Interaction, guild_id: int, clone_id) -> None:
    """Called by the setup wizard's Customize Card button. The caller has
    already deferred (ephemeral) \u2014 this just posts the wizard as a followup."""
    config = await db.get_welcome_config(guild_id, clone_id=clone_id)
    trial_started = False
    if not config.get("ultra_pack_unlocked"):
        # New trials are switched off (ULTRA_TRIAL_ENABLED in database.py), so this is a no-op for servers that never had one;
        # a server whose trial already started is handled by the config overlay, not here.
        trial_started = await db.start_ultra_trial(guild_id, interaction.user.id, clone_id=clone_id)
        if trial_started:
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
    config = await _with_studio(config, guild_id, clone_id, interaction.user.id)
    key = _draft_key(guild_id, clone_id, interaction.user.id)
    _set_tab(key, "bg")
    tv = None
    try:
        tv = await _render_tv(interaction.client, interaction.guild or interaction.client.get_guild(guild_id),
                              guild_id, clone_id, interaction.user)
    except Exception as e:
        logger.warning(f"[cardwz] first preview failed for guild {guild_id}: {type(e).__name__}: {e}", exc_info=True)
    tv_name = _new_tv_name() if tv else None
    _TV_STATE[key] = tv_name
    _trim(_TV_STATE, _STATE_MAX)
    _TV_NOTES[key] = tv[1] if tv else None
    _trim(_TV_NOTES, _STATE_MAX)
    view = build_customize_view(guild_id, clone_id, interaction.user.id, config, tab="bg",
                                has_tv=tv is not None, note=_TV_NOTES[key], tv_name=tv_name or "card.png",
                                more=_more_open(key, "bg"))
    if trial_started:
        await interaction.followup.send(
            f"\U0001F381 **You have a free 5-day trial of Customize Card!** Design it and it goes live on every "
            f"join right away. When the 5 days end, your server goes back to its normal welcome card.",
            ephemeral=True,
        )
    if tv:
        await interaction.followup.send(view=view, file=_tv_file(tv[0], tv_name), ephemeral=True)
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
        config = await _with_studio(await db.get_welcome_config(guild_id, clone_id=clone_id), guild_id, clone_id, interaction.user.id)
        tv = None
        try:
            tv = await _render_tv(interaction.client, interaction.guild or interaction.client.get_guild(guild_id),
                                  guild_id, clone_id, interaction.user)
        except Exception as e:
            logger.warning(f"[cardwz] preview failed for guild {guild_id}: {type(e).__name__}: {e}", exc_info=True)
        tab = _get_tab(key)
        if tv:
            name = _new_tv_name()
            _TV_NOTES[key] = tv[1]
            view = build_customize_view(guild_id, clone_id, invoker_id, config, tab=tab, has_tv=True, note=tv[1], tv_name=name,
                                        more=_more_open(key, tab))
            await interaction.edit_original_response(view=view, attachments=[_tv_file(tv[0], name)])
            _TV_STATE[key] = name                   # only after Discord accepted the edit
        else:
            keep = _TV_STATE.get(key)               # a failed redraw must not wipe the picture that is already there
            note = "Couldn't refresh the preview just now. Your change is saved; tap anything to try again." if keep else None
            _TV_NOTES[key] = note
            view = build_customize_view(guild_id, clone_id, invoker_id, config, tab=tab, has_tv=bool(keep), note=note,
                                        tv_name=keep or "card.png", more=_more_open(key, tab))
            if keep:
                await interaction.edit_original_response(view=view)              # attachments untouched: the old picture stays
            else:
                await interaction.edit_original_response(view=view, attachments=[])
        _trim(_TV_NOTES, _STATE_MAX)
        _trim(_TV_STATE, _STATE_MAX)
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


class CardLayoutSelect(_OptionSelectMixin, discord.ui.DynamicItem[discord.ui.Select], template=_id_pattern("layout")):
    FIELD, ID, PLACEHOLDER, CHOICES = "layout", "layout", "Card layout", _LAYOUT_LABELS


class CardFontSelect(_OptionSelectMixin, discord.ui.DynamicItem[discord.ui.Select], template=_id_pattern("font")):
    FIELD, ID, PLACEHOLDER, CHOICES = "font", "font", "Font", _FONT_LABELS


class CardBracketSelect(_OptionSelectMixin, discord.ui.DynamicItem[discord.ui.Select], template=_id_pattern("bracket")):
    FIELD, ID, PLACEHOLDER, CHOICES = "bracket", "bracket", "Brackets around the username", _BRACKET_LABELS


class CardFocusSelect(_OptionSelectMixin, discord.ui.DynamicItem[discord.ui.Select], template=_id_pattern("focus")):
    FIELD, ID, PLACEHOLDER, CHOICES = "focus", "focus", "Image crop focus", _FOCUS_LABELS


class CardRatioSelect(_OptionSelectMixin, discord.ui.DynamicItem[discord.ui.Select], template=_id_pattern("ratio")):
    FIELD, ID, PLACEHOLDER, CHOICES = "ratio", "ratio", "Card shape (ratio)", _RATIO_LABELS


class CardColorSelect(_OptionSelectMixin, discord.ui.DynamicItem[discord.ui.Select], template=_id_pattern("color")):
    FIELD, ID, PLACEHOLDER, CHOICES = "text_color", "color", "Text color", _COLOR_LABELS


_STYLE_EXTRAS = {
    "ring": ("Avatar ring", "Colored frame around the avatar"),
    "soft_edge": ("Soft banner edge", "Banner fades into your image"),
    "shadow": ("Text shadow", "Soft shadow so text pops on busy images"),
    "big_name": ("Large username", "Bigger name, smaller heading emphasis"),
    "glass": ("Glass banner", "Frosted, blurred banner instead of solid black"),
    "auto_contrast": ("Auto contrast", "Adds outline/shading when the image is bright"),
}


class CardStyleSelect(discord.ui.DynamicItem[discord.ui.Select], template=_id_pattern("style")):
    """Optional extras (multi-select, all off by default). Selecting nothing
    turns them all off."""

    def __init__(self, guild_id: int, clone_id, invoker_id, config: dict):
        self.guild_id = guild_id
        self.clone_id = clone_id
        self.invoker_id = invoker_id
        opts = parse_ultra_options(config.get("ultra_card_json"))
        super().__init__(discord.ui.Select(
            placeholder="Style extras (optional)", min_values=0, max_values=len(_STYLE_EXTRAS),
            options=[
                discord.SelectOption(label=lbl, description=desc, value=key, default=bool(opts.get(key)))
                for key, (lbl, desc) in _STYLE_EXTRAS.items()
            ],
            custom_id=_encode("style", guild_id, clone_id, invoker_id),
        ))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        g, c, i = _decode(match)
        return cls(g, c, i, {})

    async def callback(self, interaction: discord.Interaction):
        if not await _check_access(interaction, self.invoker_id, self.guild_id):
            return
        await interaction.response.defer()
        chosen = set(self.item.values)
        await _save_option(self.guild_id, self.clone_id, interaction.user.id,
                           **{k: (k in chosen) for k in _STYLE_EXTRAS})
        await _rerender(interaction, self.guild_id, self.clone_id, self.invoker_id)


class CardShapeSelect(_OptionSelectMixin, discord.ui.DynamicItem[discord.ui.Select], template=_id_pattern("shape")):
    """Avatar frame shape lives in its own column (shared with the other
    card modes), not in ultra_card_json."""
    FIELD, ID, PLACEHOLDER, CHOICES = "avatar_shape", "shape", "Avatar frame shape", vw.AVATAR_SHAPE_LABELS

    def _current(self, config: dict):
        return config.get("avatar_shape", "circle")

    async def _save(self, value: str, user_id=None):
        cfg = await db.get_welcome_config(self.guild_id, clone_id=self.clone_id)
        _push_undo(self.guild_id, self.clone_id, user_id, cfg)
        if not cfg.get("ultra_pack_unlocked"):
            _get_draft(self.guild_id, self.clone_id, user_id, create=True)["shape"] = value
            return
        await db.set_welcome_config(self.guild_id, clone_id=self.clone_id, avatar_shape=value)


class CardRotationSelect(discord.ui.DynamicItem[discord.ui.Select], template=_id_pattern("rotation")):
    """Phase 2c: how pictures change for each newcomer. Open to everyone as a preview; only Premium/Plus
    servers can SAVE it (re-checked on the server when tapped)."""

    def __init__(self, guild_id: int, clone_id, invoker_id, config: dict | None = None):
        from modules import welcome_gallery as wg
        self.guild_id, self.clone_id, self.invoker_id = guild_id, clone_id, invoker_id
        studio = (config or {}).get("_studio") or {}
        current = wg.clean_mode(studio.get("mode") or (config or {}).get("rotation_mode"))
        super().__init__(discord.ui.Select(
            placeholder="Change picture for each newcomer",
            options=[discord.SelectOption(label=label, value=value, default=(value == current))
                     for value, label in wg.MODE_LABELS.items()],
            custom_id=_encode("rotation", guild_id, clone_id, invoker_id)))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        g, c, i = _decode(match)
        return cls(g, c, i, {})

    async def callback(self, interaction: discord.Interaction):
        if not await _check_access(interaction, self.invoker_id, self.guild_id):
            return
        await interaction.response.defer()
        from modules.welcome_studio import studio_unlocked
        if not await studio_unlocked(self.guild_id, self.clone_id):
            # Preview only: remember the pick in memory so they can try it, store nothing.
            _ROTATION_DRAFT[_draft_key(self.guild_id, self.clone_id, interaction.user.id)] = self.item.values[0]
            _trim(_ROTATION_DRAFT, _STATE_MAX)
            await _rerender(interaction, self.guild_id, self.clone_id, self.invoker_id)
            return
        await db.gallery_set_rotation_mode(self.guild_id, self.clone_id, self.item.values[0])
        await _rerender(interaction, self.guild_id, self.clone_id, self.invoker_id)


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
            # A pasted link can expire or disappear, so keep our own copy in the hosting channel whenever it's available.
            from discord_bot.cogs.welcome import _host_bytes
            hosted = await _host_bytes(interaction.client, data, interaction.guild or interaction.client.get_guild(self.guild_id), self.guild_id)
            if hosted is not None:
                await db.set_welcome_config(
                    self.guild_id, clone_id=self.clone_id,
                    custom_background_url=hosted[2] or url, custom_bg_channel_id=hosted[0], custom_bg_message_id=hosted[1],
                )
            else:
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
    FIELD, LABEL, STYLE = "setbg", "🖼️ Upload image", discord.ButtonStyle.success

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
        try:   # Phase 2e: say it plainly when gallery pictures still exist (Clear never deletes them)
            if await db.gallery_list(self.guild_id, self.clone_id):
                await interaction.followup.send(
                    "ℹ️ The single background is cleared, but your gallery pictures are still used. "
                    "Open More controls and use Remove a picture to take those out.", ephemeral=True)
        except Exception:
            logger.exception("[cardwz] clear-background gallery note failed for guild %s", self.guild_id)
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
        if cfg.get("ultra_pack_unlocked") and not cfg.get("ultra_trial_active"):
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
    FIELD, LABEL = "reset", "↩️ Reset all"

    async def callback(self, interaction: discord.Interaction):
        if not await _check_access(interaction, self.invoker_id, self.guild_id):
            return
        await interaction.response.defer()
        cfg = await db.get_welcome_config(self.guild_id, clone_id=self.clone_id)
        _push_undo(self.guild_id, self.clone_id, interaction.user.id, cfg)
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


class CardPresetSelect(discord.ui.DynamicItem[discord.ui.Select], template=_id_pattern("preset")):
    """One tap sets banner, darkness, avatar side, text colour, member # and avatar shape. Heading and
    background are left alone. Always shows the placeholder (the options are independent, so no preset
    is 'current')."""

    def __init__(self, guild_id: int, clone_id, invoker_id):
        self.guild_id = guild_id
        self.clone_id = clone_id
        self.invoker_id = invoker_id
        super().__init__(discord.ui.Select(
            placeholder="\u2728 Style presets \u2014 pick one, then fine-tune",
            options=[discord.SelectOption(label=p["label"], value=key, description=p["desc"][:100])
                     for key, p in STYLE_PRESETS.items()],
            custom_id=_encode("preset", guild_id, clone_id, invoker_id),
        ))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        g, c, i = _decode(match)
        return cls(g, c, i)

    async def callback(self, interaction: discord.Interaction):
        if not await _check_access(interaction, self.invoker_id, self.guild_id):
            return
        await interaction.response.defer()
        preset = STYLE_PRESETS.get(self.item.values[0])
        if preset is not None:                    # an unknown value is ignored, never trusted
            await _apply_style(self.guild_id, self.clone_id, interaction.user.id, dict(preset["opts"]), preset["shape"])
        await _rerender(interaction, self.guild_id, self.clone_id, self.invoker_id)


async def _show_step(interaction: discord.Interaction, guild_id: int, clone_id, invoker_id, tab: str) -> None:
    """Redraw only the controls for `tab` (the card picture is not redrawn). Interaction must already be deferred."""
    key = _draft_key(guild_id, clone_id, interaction.user.id)
    config = await db.get_welcome_config(guild_id, clone_id=clone_id)
    if tab == "bg":
        config = await _with_studio(config, guild_id, clone_id, interaction.user.id)
    name = _TV_STATE.get(key)
    if name is None and key not in _TV_STATE:        # state lost (bot restarted): fall back to what is on the message
        msg = interaction.message
        name = next((a.filename for a in (msg.attachments if msg else []) if a.filename.startswith("card") and a.filename.endswith(".png")), None)
    await interaction.edit_original_response(view=build_customize_view(
        guild_id, clone_id, invoker_id, config, tab=tab, has_tv=bool(name), note=_TV_NOTES.get(key),
        tv_name=name or "card.png", more=_more_open(key, tab)))


class CardTabButton(discord.ui.DynamicItem[discord.ui.Button], template=r"^cardwz_tab(bg|layout|text|send):(\d+):(-|\d+):(-|\d+)$"):
    """Switches which step shows under the TV. Does not redraw the card."""

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
        _set_tab(_draft_key(self.guild_id, self.clone_id, interaction.user.id), self.tab)
        await _show_step(interaction, self.guild_id, self.clone_id, self.invoker_id, self.tab)


class CardMoreButton(_Btn, discord.ui.DynamicItem[discord.ui.Button], template=_id_pattern("more")):
    """Show or hide the extra controls for the step you are on. Does not redraw the card."""
    FIELD = "more"

    def __init__(self, guild_id: int, clone_id, invoker_id, shown: bool = False):
        super().__init__(guild_id, clone_id, invoker_id, shown)

    def label_text(self, shown=False, *_):
        return "➖ Fewer controls" if shown else "➕ More controls"

    async def callback(self, interaction: discord.Interaction):
        if not await _check_access(interaction, self.invoker_id, self.guild_id):
            return
        await interaction.response.defer()
        key = _draft_key(self.guild_id, self.clone_id, interaction.user.id)
        tab = _get_tab(key)
        _toggle_more(key, tab)
        await _show_step(interaction, self.guild_id, self.clone_id, self.invoker_id, tab)


class CardUndoButton(_Btn, discord.ui.DynamicItem[discord.ui.Button], template=_id_pattern("undo")):
    """Takes back the last change to the look (up to 10). The picture is not part of undo."""
    FIELD, LABEL = "undo", "↩️ Undo"

    async def callback(self, interaction: discord.Interaction):
        if not await _check_access(interaction, self.invoker_id, self.guild_id):
            return
        await interaction.response.defer()
        if not await _undo_last(self.guild_id, self.clone_id, interaction.user.id):
            await interaction.followup.send("Nothing to undo yet.", ephemeral=True)
            return
        await _rerender(interaction, self.guild_id, self.clone_id, self.invoker_id)


class CardStepResetButton(_Btn, discord.ui.DynamicItem[discord.ui.Button], template=_id_pattern("stepreset")):
    """Puts the controls of the step you are on back to default. The picture and the other steps are untouched."""
    FIELD, LABEL = "stepreset", "🔄 Reset this step"

    async def callback(self, interaction: discord.Interaction):
        if not await _check_access(interaction, self.invoker_id, self.guild_id):
            return
        await interaction.response.defer()
        tab = _get_tab(_draft_key(self.guild_id, self.clone_id, interaction.user.id))
        fields = _STEP_RESET_FIELDS.get(tab)
        if fields:
            defaults = parse_ultra_options(None)
            await _apply_style(self.guild_id, self.clone_id, interaction.user.id,
                               {k: defaults[k] for k in fields}, _STEP_RESET_SHAPE.get(tab))
        await _rerender(interaction, self.guild_id, self.clone_id, self.invoker_id)


class CardTestSendButton(_Btn, discord.ui.DynamicItem[discord.ui.Button], template=_id_pattern("testsend")):
    """Step 4: the real card, full size, shown only to the admin (watermarked while the server is unpaid)."""
    FIELD, LABEL, STYLE = "testsend", "📨 Send me a test", discord.ButtonStyle.primary

    async def callback(self, interaction: discord.Interaction):
        if not await _check_access(interaction, self.invoker_id, self.guild_id):
            return
        key = _draft_key(self.guild_id, self.clone_id, interaction.user.id)
        now = time.monotonic()
        if now - _TEST_LAST.get(key, -_TEST_COOLDOWN) < _TEST_COOLDOWN:
            await interaction.response.send_message("One moment, then try again.", ephemeral=True)
            return
        _TEST_LAST[key] = now
        _trim(_TEST_LAST, _STATE_MAX)
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            real_cfg = await db.get_welcome_config(self.guild_id, clone_id=self.clone_id)
            unlocked = bool(real_cfg.get("ultra_pack_unlocked"))
            png, _note = await _render_tv(interaction.client, interaction.guild or interaction.client.get_guild(self.guild_id),
                                          self.guild_id, self.clone_id, interaction.user, full=True)
            await interaction.followup.send(
                content="**Test welcome card** — only visible to you" + ("" if unlocked else " — unlock to save it and remove the watermark"),
                file=discord.File(fp=io.BytesIO(png), filename="test-card.png"), ephemeral=True,
            )
        except Exception as e:
            logger.warning(f"[cardwz] test send failed for guild {self.guild_id}: {type(e).__name__}: {e}")
            await interaction.followup.send("Couldn't draw the test card just now. Try again in a moment.", ephemeral=True)


# ── Phase 2c-2: remove a picture / make a picture first / unlock ─────────

class CardPictureNumberModal(discord.ui.Modal):
    """Asks which picture (1 to 10) to remove or make first. The write is gated on Premium/Plus on the server."""

    def __init__(self, guild_id: int, clone_id, invoker_id, action: str):
        super().__init__(title="Remove a picture" if action == "remove" else "Make a picture first")
        self.guild_id, self.clone_id, self.invoker_id, self.action = guild_id, clone_id, invoker_id, action
        self.number = discord.ui.TextInput(style=discord.TextStyle.short, required=True, max_length=2, placeholder="1 to 10")
        self.add_item(discord.ui.Label(
            text="Picture number",
            description=("It is taken out of the gallery." if action == "remove"
                         else "It becomes picture 1, the one used by \"Same picture every time\"."),
            component=self.number))

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        from modules import welcome_gallery as wg
        from modules.welcome_studio import studio_unlocked, LOCKED_LINE
        try:
            n = int(str(self.number.value or "").strip())
        except ValueError:
            n = 0
        if not 1 <= n <= wg.MAX_PICTURES:
            await interaction.followup.send(f"\u26A0\uFE0F Type a number from 1 to {wg.MAX_PICTURES}.", ephemeral=True)
            return
        if not await studio_unlocked(self.guild_id, self.clone_id):
            # Preview only: a server without Premium/Plus has no stored pictures, so there is nothing to change.
            await interaction.followup.send(f"\U0001F512 Preview only: {LOCKED_LINE} Pictures are stored and managed with Premium.", ephemeral=True)
            return
        try:
            if self.action == "remove":
                done = await db.gallery_remove(self.guild_id, self.clone_id, n)
            else:
                done = await db.gallery_make_first(self.guild_id, self.clone_id, n)
        except Exception:
            logger.exception("[cardwz] gallery %s failed for guild %s", self.action, self.guild_id)
            await interaction.followup.send("Couldn't do that just now. Try again in a moment.", ephemeral=True)
            return
        if not done:
            msg = (f"Picture {n} is already first." if (self.action == "first" and n == 1)
                   else f"There is no picture {n} in the gallery.")
            await interaction.followup.send(msg, ephemeral=True)
            return
        await _rerender(interaction, self.guild_id, self.clone_id, self.invoker_id)


# ── saved designs (studio 5b) ─────────────────────────────────────────────

def _designs_line(studio: dict | None) -> str | None:
    """One plain line about saved designs on the Send step. None when the summary is missing."""
    from modules import welcome_designs as wd
    from modules.welcome_studio import LOCKED_LINE
    if not studio:
        return None
    if not studio.get("unlocked"):
        return f"-# \U0001F512 Preview only: {LOCKED_LINE} Saved designs save with Premium."
    designs = studio.get("designs") or []
    if not designs:
        return f"-# \U0001F3A8 Saved designs: none yet (up to {wd.MAX_DESIGNS}). Save this look to reuse it later."
    names = " \u00b7 ".join(f"{i} {d['name']}" for i, d in enumerate(designs, 1))
    line = f"-# \U0001F3A8 Saved designs ({len(designs)} of {wd.MAX_DESIGNS}): {names}"
    whens = " \u00b7 ".join(f"{i} {d['name']}: {d['when']}" for i, d in enumerate(designs, 1) if d.get("when"))
    if whens:
        line += f"\n-# \U0001F4C5 Scheduled (UTC): {whens}"
    if studio.get("live_now"):
        line += (f"\n-# \u2728 Today new members get **{studio['live_now']}** (scheduled). "
                 "Your regular look comes back when it ends; the test button shows the regular look.")
    return line


class CardDesignSelect(discord.ui.DynamicItem[discord.ui.Select], template=_id_pattern("designload")):
    """Studio 5b: load a saved design onto the card (look only, pictures stay). Undo covers it."""

    def __init__(self, guild_id: int, clone_id, invoker_id, designs: list | None = None):
        self.guild_id, self.clone_id, self.invoker_id = guild_id, clone_id, invoker_id
        options = [discord.SelectOption(label=str(d["name"])[:100], value=str(d["id"])) for d in (designs or [])[:25]]
        super().__init__(discord.ui.Select(
            placeholder="Load a saved design", options=options or [discord.SelectOption(label="No designs", value="0")],
            custom_id=_encode("designload", guild_id, clone_id, invoker_id)))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        g, c, i = _decode(match)
        return cls(g, c, i, [])

    async def callback(self, interaction: discord.Interaction):
        if not await _check_access(interaction, self.invoker_id, self.guild_id):
            return
        await interaction.response.defer()
        from modules import welcome_designs as wd
        from modules.welcome_studio import studio_unlocked, LOCKED_LINE
        if not await studio_unlocked(self.guild_id, self.clone_id):
            await interaction.followup.send(f"\U0001F512 Preview only: {LOCKED_LINE} Saved designs load with Premium.", ephemeral=True)
            return
        try:
            design = await db.design_get(self.guild_id, self.clone_id, int(self.item.values[0]))
        except (ValueError, TypeError):
            design = None
        if not design:
            await interaction.followup.send("That design is gone. Pick another one.", ephemeral=True)
            await _rerender(interaction, self.guild_id, self.clone_id, self.invoker_id)
            return
        opts_json, shape = wd.restore_look(design.get("options_json"), design.get("avatar_shape"))
        await _apply_style(self.guild_id, self.clone_id, interaction.user.id, json.loads(opts_json), shape)
        await db.set_active_design(self.guild_id, self.clone_id, design["id"])      # 7b: remember which design was last made live
        await _rerender(interaction, self.guild_id, self.clone_id, self.invoker_id)


class CardDesignSaveModal(discord.ui.Modal, title="Save this look"):
    """Asks for a name and saves the look the editor shows right now. The write is gated on Premium/Plus on the server."""

    def __init__(self, guild_id: int, clone_id, invoker_id):
        super().__init__()
        self.guild_id, self.clone_id, self.invoker_id = guild_id, clone_id, invoker_id
        self.name = discord.ui.TextInput(style=discord.TextStyle.short, required=True, max_length=30, placeholder="Halloween")
        self.add_item(discord.ui.Label(
            text="Design name",
            description="Saves the look only (layout, fonts, colors, effects). Your pictures are not part of it.",
            component=self.name))

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        from modules import welcome_designs as wd
        from modules.welcome_studio import studio_unlocked, LOCKED_LINE
        name = wd.clean_name(self.name.value)
        if not name:
            await interaction.followup.send("\u26A0\uFE0F Type a name for the design.", ephemeral=True)
            return
        if not await studio_unlocked(self.guild_id, self.clone_id):
            await interaction.followup.send(f"\U0001F512 Preview only: {LOCKED_LINE} Designs are saved with Premium.", ephemeral=True)
            return
        cfg = await db.get_welcome_config(self.guild_id, clone_id=self.clone_id)
        eff = _effective_config(cfg, self.guild_id, self.clone_id, interaction.user.id)
        opts_json, shape = wd.snapshot_look(eff.get("ultra_card_json"), eff.get("avatar_shape"))
        status, _ = await db.design_save(self.guild_id, self.clone_id, name, opts_json, shape, interaction.user.id, wd.MAX_DESIGNS)
        if status == "full":
            await interaction.followup.send(
                f"You already have {wd.MAX_DESIGNS} designs. Delete one first, or save under the name of one to replace it.", ephemeral=True)
            return
        if status == "error":
            await interaction.followup.send("Couldn't save that just now. Try again in a moment.", ephemeral=True)
            return
        from modules.welcome_studio import forget_schedule_cache
        forget_schedule_cache(self.guild_id, self.clone_id)       # 7b: saving over a scheduled design changes what it renders
        await _rerender(interaction, self.guild_id, self.clone_id, self.invoker_id)


class CardDesignNumberModal(discord.ui.Modal):
    """Asks which saved design (by its number in the list) to rename or delete."""

    def __init__(self, guild_id: int, clone_id, invoker_id, action: str):
        super().__init__(title="Rename a design" if action == "rename" else "Delete a design")
        self.guild_id, self.clone_id, self.invoker_id, self.action = guild_id, clone_id, invoker_id, action
        self.number = discord.ui.TextInput(style=discord.TextStyle.short, required=True, max_length=2, placeholder="1")
        self.add_item(discord.ui.Label(text="Design number", description="The number in the list above the buttons.", component=self.number))
        self.new_name = None
        if action == "rename":
            self.new_name = discord.ui.TextInput(style=discord.TextStyle.short, required=True, max_length=30, placeholder="New name")
            self.add_item(discord.ui.Label(text="New name", component=self.new_name))

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        from modules import welcome_designs as wd
        from modules.welcome_studio import studio_unlocked, LOCKED_LINE
        if not await studio_unlocked(self.guild_id, self.clone_id):
            await interaction.followup.send(f"\U0001F512 Preview only: {LOCKED_LINE} Designs are saved and managed with Premium.", ephemeral=True)
            return
        designs = await db.design_list(self.guild_id, self.clone_id)
        try:
            n = int(str(self.number.value or "").strip())
        except ValueError:
            n = 0
        if not 1 <= n <= len(designs):
            await interaction.followup.send(f"\u26A0\uFE0F Type a number from 1 to {len(designs) or 1}.", ephemeral=True)
            return
        target = designs[n - 1]
        if self.action == "delete":
            done = await db.design_delete(self.guild_id, self.clone_id, target["id"])
            msg = "That design is already gone."
        else:
            name = wd.clean_name(self.new_name.value)
            if not name:
                await interaction.followup.send("\u26A0\uFE0F Type a new name.", ephemeral=True)
                return
            done = await db.design_rename(self.guild_id, self.clone_id, target["id"], name)
            msg = "Couldn't rename it. Another design may already have that name."
        if not done:
            await interaction.followup.send(msg, ephemeral=True)
            return
        from modules.welcome_studio import forget_schedule_cache
        forget_schedule_cache(self.guild_id, self.clone_id)       # 7b: a deleted/renamed design must not linger in the render cache
        await _rerender(interaction, self.guild_id, self.clone_id, self.invoker_id)


class CardDesignScheduleModal(discord.ui.Modal, title="Schedule a design"):
    """Studio 7b: pick a saved design (by its number) and the days it should be live. Leave 'From' empty to remove its schedule.
    Dates are UTC. `25 Oct` repeats every year; `2026-10-25` happens once."""

    def __init__(self, guild_id: int, clone_id, invoker_id):
        super().__init__()
        self.guild_id, self.clone_id, self.invoker_id = guild_id, clone_id, invoker_id
        self.number = discord.ui.TextInput(style=discord.TextStyle.short, required=True, max_length=2, placeholder="1")
        self.add_item(discord.ui.Label(text="Design number", description="The number in the list above the buttons.", component=self.number))
        self.date_from = discord.ui.TextInput(style=discord.TextStyle.short, required=False, max_length=20, placeholder="25 Oct")
        self.add_item(discord.ui.Label(text="From", description="25 Oct = every year. 2026-10-25 = once. Empty = remove schedule.", component=self.date_from))
        self.date_to = discord.ui.TextInput(style=discord.TextStyle.short, required=False, max_length=20, placeholder="1 Nov")
        self.add_item(discord.ui.Label(text="Until (included)", description="Same style as From. Empty = just that one day. Dates are UTC.", component=self.date_to))

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        from modules import welcome_schedule as sch
        from modules.welcome_studio import studio_unlocked, forget_schedule_cache, LOCKED_LINE
        if not await studio_unlocked(self.guild_id, self.clone_id):
            await interaction.followup.send(f"\U0001F512 Preview only: {LOCKED_LINE} Designs are scheduled with Premium.", ephemeral=True)
            return
        designs = await db.design_list(self.guild_id, self.clone_id)
        try:
            n = int(str(self.number.value or "").strip())
        except ValueError:
            n = 0
        if not 1 <= n <= len(designs):
            await interaction.followup.send(f"\u26A0\uFE0F Type a number from 1 to {len(designs) or 1}.", ephemeral=True)
            return
        target = designs[n - 1]
        if not str(self.date_from.value or "").strip():
            done = await db.design_clear_schedule(self.guild_id, self.clone_id, target["id"])
            note = "Couldn't remove that schedule just now."
        else:
            status, a, b, yearly = sch.build_range(self.date_from.value, self.date_to.value)
            problems = {
                "bad_from": "I couldn't read the From day. Try `25 Oct` (every year) or `2026-10-25` (once).",
                "bad_to": "I couldn't read the Until day. Try `1 Nov` (every year) or `2026-11-01` (once).",
                "mixed": "Write both days the same way: both like `25 Oct` (every year) or both like `2026-10-25` (once).",
                "backwards": "Until comes before From. Swap them.",
            }
            if status != "ok":
                await interaction.followup.send(f"\u26A0\uFE0F {problems[status]}", ephemeral=True)
                return
            done = await db.design_set_schedule(self.guild_id, self.clone_id, target["id"], a, b, yearly)
            note = "Couldn't save that schedule just now. Try again in a moment."
        if not done:
            await interaction.followup.send(note, ephemeral=True)
            return
        forget_schedule_cache(self.guild_id, self.clone_id)
        await _rerender(interaction, self.guild_id, self.clone_id, self.invoker_id)


class _DesignButton(_Btn):
    ACTION = ""

    async def callback(self, interaction: discord.Interaction):
        if not await _check_access(interaction, self.invoker_id, self.guild_id):
            return
        modal = (CardDesignSaveModal(self.guild_id, self.clone_id, self.invoker_id) if self.ACTION == "save"
                 else CardDesignScheduleModal(self.guild_id, self.clone_id, self.invoker_id) if self.ACTION == "schedule"
                 else CardDesignNumberModal(self.guild_id, self.clone_id, self.invoker_id, self.ACTION))
        try:
            await interaction.response.send_modal(modal)
        except discord.HTTPException as e:
            if e.code != 40060:  # double-click: modal already open
                raise


class CardDesignSaveButton(_DesignButton, discord.ui.DynamicItem[discord.ui.Button], template=_id_pattern("designsave")):
    FIELD, LABEL, ACTION = "designsave", "\U0001F4BE Save this look", "save"


class CardDesignRenameButton(_DesignButton, discord.ui.DynamicItem[discord.ui.Button], template=_id_pattern("designrename")):
    FIELD, LABEL, ACTION = "designrename", "\u270F\uFE0F Rename a design", "rename"


class CardDesignScheduleButton(_DesignButton, discord.ui.DynamicItem[discord.ui.Button], template=_id_pattern("designsched")):
    FIELD, LABEL, ACTION = "designsched", "\U0001F4C5 Schedule a design", "schedule"


class CardDesignDeleteButton(_DesignButton, discord.ui.DynamicItem[discord.ui.Button], template=_id_pattern("designdelete")):
    FIELD, LABEL, ACTION = "designdelete", "\U0001F5D1\uFE0F Delete a design", "delete"


class _PictureButton(_Btn):
    ACTION = ""

    async def callback(self, interaction: discord.Interaction):
        if not await _check_access(interaction, self.invoker_id, self.guild_id):
            return
        try:
            await interaction.response.send_modal(CardPictureNumberModal(self.guild_id, self.clone_id, self.invoker_id, self.ACTION))
        except discord.HTTPException as e:
            if e.code != 40060:  # double-click: modal already open
                raise


class CardPictureRemoveButton(_PictureButton, discord.ui.DynamicItem[discord.ui.Button], template=_id_pattern("picremove")):
    FIELD, LABEL, ACTION = "picremove", "\U0001F5D1\uFE0F Remove a picture", "remove"


class CardPictureFirstButton(_PictureButton, discord.ui.DynamicItem[discord.ui.Button], template=_id_pattern("picfirst")):
    FIELD, LABEL, ACTION = "picfirst", "\u2B50 Make first", "first"


class CardPicturesAddModal(discord.ui.Modal, title="Add pictures"):
    """Phase 2d: up to 10 files at once, or one link. Free servers preview the first picture only (nothing hosted)."""

    def __init__(self, guild_id: int, clone_id, invoker_id):
        super().__init__()
        self.guild_id = guild_id
        self.clone_id = clone_id
        self.invoker_id = invoker_id
        from modules import welcome_gallery as wg
        self.upload = discord.ui.FileUpload(required=False, min_values=0, max_values=wg.MAX_PER_UPLOAD)
        self.url = discord.ui.TextInput(
            style=discord.TextStyle.short, required=False, max_length=500, placeholder="https://\u2026/image.png",
        )
        self.add_item(discord.ui.Label(
            text="Upload pictures", description=f"png or jpg, up to {wg.MAX_PER_UPLOAD} at once, 8MB each", component=self.upload,
        ))
        self.add_item(discord.ui.Label(
            text="\u2026or paste one direct image link", description="Adds one picture", component=self.url,
        ))

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        from discord_bot.cogs.welcome import (
            CUSTOM_BG_ALLOWED_CONTENT_TYPES, CUSTOM_BG_MAX_BYTES, _fetch_custom_bg_bytes,
        )
        from modules import welcome_gallery as wg
        from modules.welcome_studio import studio_unlocked, LOCKED_LINE

        files = list(self.upload.values or [])[:wg.MAX_PER_UPLOAD]
        url = str(self.url.value or "").strip()
        if not files and not url:
            await interaction.followup.send("\u26A0\uFE0F Upload a picture or paste a link first.", ephemeral=True)
            return

        # One list of (name, bytes-or-None, reason-or-None). Downloads happen here so a bad one is reported by name.
        incoming: list = []
        for f in files:
            ct = (f.content_type or "").split(";")[0].strip().lower()
            if ct not in CUSTOM_BG_ALLOWED_CONTENT_TYPES:
                incoming.append((f.filename, None, "not a png/jpg"))
            elif f.size > CUSTOM_BG_MAX_BYTES:
                incoming.append((f.filename, None, "over 8MB"))
            else:
                try:
                    incoming.append((f.filename, await f.read(), None))
                except Exception:
                    incoming.append((f.filename, None, "could not be read"))
        if url:
            async with aiohttp.ClientSession() as session:
                data, reason = await _fetch_custom_bg_bytes(session, url)
            incoming.append(("link", data, None if data else reason))

        if not await studio_unlocked(self.guild_id, self.clone_id):
            # Preview only: show the first good picture as the card background in memory. Nothing is hosted or stored.
            for _name, data, _why in incoming:
                small = await asyncio.to_thread(_shrink_bg, data) if data else None
                if small is not None:
                    _get_draft(self.guild_id, self.clone_id, interaction.user.id, create=True)["bg"] = small
                    break
            else:
                await interaction.followup.send("\u26A0\uFE0F Couldn't read those pictures. Use png or jpg under 8MB.", ephemeral=True)
                return
            await interaction.followup.send(
                f"\U0001F512 Preview only: {LOCKED_LINE} The first picture is shown on the card. "
                "A gallery of up to 10 pictures is stored with Premium.", ephemeral=True)
            await _rerender(interaction, self.guild_id, self.clone_id, self.invoker_id)
            return

        added, skipped = await _add_pictures(
            interaction.client, interaction.guild or interaction.client.get_guild(self.guild_id),
            self.guild_id, self.clone_id, incoming)
        bits = []
        if added:
            bits.append(f"\u2705 Added {len(added)} picture{'s' if len(added) != 1 else ''} (picture {', '.join(str(n) for n in added)}).")
        if skipped:
            bits.append("\u26A0\uFE0F Skipped: " + "; ".join(f"{n} ({why})" for n, why in skipped[:6])
                        + (f"; and {len(skipped) - 6} more" if len(skipped) > 6 else "") + ".")
        await interaction.followup.send("\n".join(bits) or "Nothing was added.", ephemeral=True)
        await _rerender(interaction, self.guild_id, self.clone_id, self.invoker_id)


async def _add_pictures(bot, guild, guild_id: int, clone_id, incoming: list):
    """Host and store prepared pictures for a Premium/Plus server. Returns (added_slots, [(name, reason)]). Never raises.

    Order: copy the old single picture in as picture 1 (once), then for each file check room and the daily limit,
    downscale to 1536 px JPEG, post to the hosting channel with a caption, store the slot.
    """
    from modules import welcome_gallery as wg, image_host
    added: list = []
    skipped: list = []
    try:
        await db.gallery_migrate_single(guild_id, clone_id)
        left = wg.uploads_left(guild_id, clone_id, await db.gallery_count_recent(guild_id, clone_id))
        gname = getattr(guild, "name", "?")
        for name, data, why in incoming:
            if why or data is None:
                skipped.append((name, why or "could not be read"))
                continue
            rows = await db.gallery_list(guild_id, clone_id)
            used = {int(r["slot"]) for r in rows}
            if len(used) >= wg.MAX_PICTURES:
                skipped.append((name, f"gallery is full, {wg.MAX_PICTURES} pictures"))
                continue
            if left <= 0:
                skipped.append((name, f"daily limit of {wg.DAILY_UPLOAD_LIMIT} pictures reached, try tomorrow"))
                continue
            small = await asyncio.to_thread(wg.prepare_picture, data)
            if small is None:
                skipped.append((name, "not a readable image"))
                continue
            nxt = next((n for n in range(1, wg.MAX_PICTURES + 1) if n not in used), None)
            hosted = await image_host.post_to_host(bot, small, f"Studio picture {nxt} \u2014 guild `{guild_id}` ({gname})")
            if hosted is None:
                skipped.append((name, "picture hosting is not available right now"))
                break
            slot = await db.gallery_add(guild_id, clone_id, hosted[0], hosted[1], hosted[2], label=name[:80])
            if slot is None:
                try:   # raced to full: do not leave an orphan in the hosting channel
                    ch = bot.get_channel(int(hosted[0]))
                    if ch is not None:
                        await ch.get_partial_message(int(hosted[1])).delete()
                except Exception:
                    pass
                skipped.append((name, "gallery is full"))
                continue
            wg.note_upload(guild_id, clone_id)
            left -= 1
            added.append(slot)
    except Exception:
        logger.exception("[cardwz] adding pictures failed for guild %s", guild_id)
        skipped.append(("the rest", "something went wrong, try again"))
    return added, skipped


class CardPictureAddButton(_Btn, discord.ui.DynamicItem[discord.ui.Button], template=_id_pattern("picadd")):
    FIELD, LABEL, STYLE = "picadd", "\u2795 Add pictures", discord.ButtonStyle.success

    async def callback(self, interaction: discord.Interaction):
        if not await _check_access(interaction, self.invoker_id, self.guild_id):
            return
        try:
            await interaction.response.send_modal(CardPicturesAddModal(self.guild_id, self.clone_id, self.invoker_id))
        except discord.HTTPException as e:
            if e.code != 40060:  # double-click: modal already open
                raise


# ── Phase 3a: zoom + pan arrows and text size ────────────────────────────

_PAN_STEP = 10
_NUDGE_STEP = 20
_ZOOM_ORDER = (100, 125, 150, 200)
_TEXT_SIZE_ORDER = ("small", "medium", "large", "xl")
_FRAME_LABELS = {"none": "None", "thin": "Thin", "medium": "Medium", "thick": "Thick", "double": "Double", "rounded": "Rounded", "glow": "Soft glow"}
_TEXT_SIZE_LABELS = {"small": "Small", "medium": "Medium", "large": "Large", "xl": "Extra large"}


async def _current_opts(guild_id: int, clone_id, user_id) -> dict:
    cfg = await db.get_welcome_config(guild_id, clone_id=clone_id)
    cfg = _effective_config(cfg, guild_id, clone_id, user_id)
    return parse_ultra_options(cfg.get("ultra_card_json"))


class _PanButton(_Btn):
    AXIS, DELTA = "pan_x", 0

    async def callback(self, interaction: discord.Interaction):
        if not await _check_access(interaction, self.invoker_id, self.guild_id):
            return
        await interaction.response.defer()
        cur = (await _current_opts(self.guild_id, self.clone_id, interaction.user.id))[self.AXIS]
        new = max(-100, min(100, cur + self.DELTA))
        if new != cur:
            await _save_option(self.guild_id, self.clone_id, interaction.user.id, **{self.AXIS: new})
        await _rerender(interaction, self.guild_id, self.clone_id, self.invoker_id)


class CardPanLeftButton(_PanButton, discord.ui.DynamicItem[discord.ui.Button], template=_id_pattern("panleft")):
    FIELD, LABEL, AXIS, DELTA = "panleft", "\u25C0", "pan_x", -_PAN_STEP


class CardPanUpButton(_PanButton, discord.ui.DynamicItem[discord.ui.Button], template=_id_pattern("panup")):
    FIELD, LABEL, AXIS, DELTA = "panup", "\u25B2", "pan_y", -_PAN_STEP


class CardPanDownButton(_PanButton, discord.ui.DynamicItem[discord.ui.Button], template=_id_pattern("pandown")):
    FIELD, LABEL, AXIS, DELTA = "pandown", "\u25BC", "pan_y", _PAN_STEP


class CardPanRightButton(_PanButton, discord.ui.DynamicItem[discord.ui.Button], template=_id_pattern("panright")):
    FIELD, LABEL, AXIS, DELTA = "panright", "\u25B6", "pan_x", _PAN_STEP


class CardZoomButton(_Btn, discord.ui.DynamicItem[discord.ui.Button], template=_id_pattern("zoom")):
    """Cycles 100% > 125% > 150% > 200% > 100%."""
    FIELD = "zoom"

    def __init__(self, guild_id: int, clone_id, invoker_id, zoom: int = 100):
        super().__init__(guild_id, clone_id, invoker_id, zoom)

    def label_text(self, zoom=100, *_):
        return f"\U0001F50D Zoom: {zoom}%"

    async def callback(self, interaction: discord.Interaction):
        if not await _check_access(interaction, self.invoker_id, self.guild_id):
            return
        await interaction.response.defer()
        cur = (await _current_opts(self.guild_id, self.clone_id, interaction.user.id))["zoom"]
        nxt = _ZOOM_ORDER[(_ZOOM_ORDER.index(cur) + 1) % len(_ZOOM_ORDER)] if cur in _ZOOM_ORDER else 100
        await _save_option(self.guild_id, self.clone_id, interaction.user.id, zoom=nxt)
        await _rerender(interaction, self.guild_id, self.clone_id, self.invoker_id)


class CardTextSizeButton(_Btn, discord.ui.DynamicItem[discord.ui.Button], template=_id_pattern("textsize")):
    """Cycles Small > Medium > Large > Extra large > Small."""
    FIELD = "textsize"

    def __init__(self, guild_id: int, clone_id, invoker_id, size: str = "medium"):
        super().__init__(guild_id, clone_id, invoker_id, size)

    def label_text(self, size="medium", *_):
        return f"\U0001F520 Text size: {_TEXT_SIZE_LABELS.get(size, 'Medium')}"

    async def callback(self, interaction: discord.Interaction):
        if not await _check_access(interaction, self.invoker_id, self.guild_id):
            return
        await interaction.response.defer()
        cur = (await _current_opts(self.guild_id, self.clone_id, interaction.user.id))["text_size"]
        nxt = _TEXT_SIZE_ORDER[(_TEXT_SIZE_ORDER.index(cur) + 1) % len(_TEXT_SIZE_ORDER)] if cur in _TEXT_SIZE_ORDER else "medium"
        await _save_option(self.guild_id, self.clone_id, interaction.user.id, text_size=nxt)
        await _rerender(interaction, self.guild_id, self.clone_id, self.invoker_id)


class CardFrameButton(_Btn, discord.ui.DynamicItem[discord.ui.Button], template=_id_pattern("frame")):
    """Cycles None > Thin > Thick > Double > Rounded."""
    FIELD = "frame"

    def __init__(self, guild_id: int, clone_id, invoker_id, frame: str = "none"):
        super().__init__(guild_id, clone_id, invoker_id, frame)

    def label_text(self, frame="none", *_):
        return f"\U0001F5BC\uFE0F Frame: {_FRAME_LABELS.get(frame, 'None')}"

    async def callback(self, interaction: discord.Interaction):
        if not await _check_access(interaction, self.invoker_id, self.guild_id):
            return
        await interaction.response.defer()
        cur = (await _current_opts(self.guild_id, self.clone_id, interaction.user.id))["frame"]
        order = tuple(_FRAME_LABELS)
        nxt = order[(order.index(cur) + 1) % len(order)] if cur in order else "none"
        await _save_option(self.guild_id, self.clone_id, interaction.user.id, frame=nxt)
        await _rerender(interaction, self.guild_id, self.clone_id, self.invoker_id)


_AVATAR_POS_LABELS = {
    "auto": "Automatic", "top-left": "Top left", "top": "Top", "top-right": "Top right", "left": "Left",
    "center": "Center", "right": "Right", "bottom-left": "Bottom left", "bottom": "Bottom", "bottom-right": "Bottom right",
}


class CardAvatarPosButton(_Btn, discord.ui.DynamicItem[discord.ui.Button], template=_id_pattern("avatarpos")):
    """Studio 3e: cycles the avatar through Automatic and the nine spots (reading order). The renderer's guard keeps it on
    the card and clear of the text, so a spot that would land on the text stops beside it."""
    FIELD = "avatarpos"

    def __init__(self, guild_id: int, clone_id, invoker_id, pos: str = "auto"):
        super().__init__(guild_id, clone_id, invoker_id, pos)

    def label_text(self, pos="auto", *_):
        return f"\U0001F9D1 Avatar spot: {_AVATAR_POS_LABELS.get(pos, 'Automatic')}"

    async def callback(self, interaction: discord.Interaction):
        if not await _check_access(interaction, self.invoker_id, self.guild_id):
            return
        await interaction.response.defer()
        cur = (await _current_opts(self.guild_id, self.clone_id, interaction.user.id))["avatar_pos"]
        order = tuple(_AVATAR_POS_LABELS)
        nxt = order[(order.index(cur) + 1) % len(order)] if cur in order else "auto"
        await _save_option(self.guild_id, self.clone_id, interaction.user.id, avatar_pos=nxt)
        await _rerender(interaction, self.guild_id, self.clone_id, self.invoker_id)


_EFFECT_LABELS = {"none": "None", "blur": "Blur", "vignette": "Vignette", "bw": "Black & white",
                  "warm": "Warm", "cool": "Cool", "fade": "Faded", "grain": "Film grain", "sparkle": "Sparkle"}


class CardEffectButton(_Btn, discord.ui.DynamicItem[discord.ui.Button], template=_id_pattern("effect")):
    """Studio 4a: cycles one look for the background picture (None, Blur, Vignette, Black & white, Warm, Cool, Faded)."""
    FIELD = "effect"

    def __init__(self, guild_id: int, clone_id, invoker_id, effect: str = "none"):
        super().__init__(guild_id, clone_id, invoker_id, effect)

    def label_text(self, effect="none", *_):
        return f"\u2728 Look: {_EFFECT_LABELS.get(effect, 'None')}"

    async def callback(self, interaction: discord.Interaction):
        if not await _check_access(interaction, self.invoker_id, self.guild_id):
            return
        await interaction.response.defer()
        cur = (await _current_opts(self.guild_id, self.clone_id, interaction.user.id))["effect"]
        order = tuple(_EFFECT_LABELS)
        nxt = order[(order.index(cur) + 1) % len(order)] if cur in order else "none"
        await _save_option(self.guild_id, self.clone_id, interaction.user.id, effect=nxt)
        await _rerender(interaction, self.guild_id, self.clone_id, self.invoker_id)


class CardBrightnessButton(_Btn, discord.ui.DynamicItem[discord.ui.Button], template=_id_pattern("bright")):
    """Studio 4d: how light the background picture is. Cycles Darker, Dark, Normal, Light, Lighter."""
    FIELD = "bright"

    def __init__(self, guild_id: int, clone_id, invoker_id, level: str = "normal"):
        super().__init__(guild_id, clone_id, invoker_id, level)

    def label_text(self, level="normal", *_):
        return f"\U0001F506 Brightness: {level.title()}"

    async def callback(self, interaction: discord.Interaction):
        if not await _check_access(interaction, self.invoker_id, self.guild_id):
            return
        await interaction.response.defer()
        cur = (await _current_opts(self.guild_id, self.clone_id, interaction.user.id))["brightness"]
        order = tuple(ULTRA_BRIGHTNESS)
        nxt = order[(order.index(cur) + 1) % len(order)] if cur in order else "normal"
        await _save_option(self.guild_id, self.clone_id, interaction.user.id, brightness=nxt)
        await _rerender(interaction, self.guild_id, self.clone_id, self.invoker_id)


class CardHeadingFontButton(_Btn, discord.ui.DynamicItem[discord.ui.Button], template=_id_pattern("headfont")):
    """Studio 4b: the heading line gets its own font. Cycles Same as name, then every font in the Font select."""
    FIELD = "headfont"

    def __init__(self, guild_id: int, clone_id, invoker_id, font: str = "same"):
        super().__init__(guild_id, clone_id, invoker_id, font)

    def label_text(self, font="same", *_):
        return f"\U0001F524 Heading font: {'Same as name' if font == 'same' else font.title()}"

    async def callback(self, interaction: discord.Interaction):
        if not await _check_access(interaction, self.invoker_id, self.guild_id):
            return
        await interaction.response.defer()
        cur = (await _current_opts(self.guild_id, self.clone_id, interaction.user.id))["heading_font"]
        order = ("same",) + tuple(ULTRA_FONTS)
        nxt = order[(order.index(cur) + 1) % len(order)] if cur in order else "same"
        await _save_option(self.guild_id, self.clone_id, interaction.user.id, heading_font=nxt)
        await _rerender(interaction, self.guild_id, self.clone_id, self.invoker_id)


class CardOutlineButton(_Btn, discord.ui.DynamicItem[discord.ui.Button], template=_id_pattern("outline")):
    """Studio 4c: dark edge around the text. Cycles Auto > None > Thin > Medium > Thick."""
    FIELD = "outline"

    def __init__(self, guild_id: int, clone_id, invoker_id, outline: str = "auto"):
        super().__init__(guild_id, clone_id, invoker_id, outline)

    def label_text(self, outline="auto", *_):
        return f"\U0001F58B\uFE0F Outline: {outline.title()}"

    async def callback(self, interaction: discord.Interaction):
        if not await _check_access(interaction, self.invoker_id, self.guild_id):
            return
        await interaction.response.defer()
        cur = (await _current_opts(self.guild_id, self.clone_id, interaction.user.id))["text_outline"]
        order = ("auto",) + tuple(ULTRA_OUTLINES)
        nxt = order[(order.index(cur) + 1) % len(order)] if cur in order else "auto"
        await _save_option(self.guild_id, self.clone_id, interaction.user.id, text_outline=nxt)
        await _rerender(interaction, self.guild_id, self.clone_id, self.invoker_id)


class CardFrameColorButton(_Btn, discord.ui.DynamicItem[discord.ui.Button], template=_id_pattern("framecolor")):
    """Cycles the frame color through the same short color list as the text."""
    FIELD = "framecolor"

    def __init__(self, guild_id: int, clone_id, invoker_id, color: str = "white"):
        super().__init__(guild_id, clone_id, invoker_id, color)

    def label_text(self, color="white", *_):
        return f"\U0001F3A8 Frame color: {str(color).title()}"

    async def callback(self, interaction: discord.Interaction):
        if not await _check_access(interaction, self.invoker_id, self.guild_id):
            return
        await interaction.response.defer()
        cur = (await _current_opts(self.guild_id, self.clone_id, interaction.user.id))["frame_color"]
        order = tuple(ULTRA_TEXT_COLORS)
        nxt = order[(order.index(cur) + 1) % len(order)] if cur in order else "white"
        await _save_option(self.guild_id, self.clone_id, interaction.user.id, frame_color=nxt)
        await _rerender(interaction, self.guild_id, self.clone_id, self.invoker_id)


# ── Phase 2e: replace one picture in place ───────────────────────────────

async def _replace_picture(bot, guild, guild_id: int, clone_id, slot: int, data: bytes):
    """Swap the picture in `slot` for a new one (same slot number, same place in the order). Returns (ok, reason).
    Never raises. Counts toward the daily upload limit like an Add does; the old hosted post is deleted best-effort."""
    from modules import welcome_gallery as wg, image_host
    try:
        rows = await db.gallery_list(guild_id, clone_id)
        if slot not in {int(r["slot"]) for r in rows}:
            return False, f"there is no picture {slot} in the gallery"
        if wg.uploads_left(guild_id, clone_id, await db.gallery_count_recent(guild_id, clone_id)) <= 0:
            return False, f"daily limit of {wg.DAILY_UPLOAD_LIMIT} pictures reached, try tomorrow"
        small = await asyncio.to_thread(wg.prepare_picture, data)
        if small is None:
            return False, "not a readable image"
        gname = getattr(guild, "name", "?")
        hosted = await image_host.post_to_host(bot, small, f"Studio picture {slot} (replaced) — guild `{guild_id}` ({gname})")
        if hosted is None:
            return False, "picture hosting is not available right now"
        old = await db.gallery_replace(guild_id, clone_id, slot, hosted[0], hosted[1], hosted[2])
        if old is None:   # the slot was removed while we were hosting: do not leave an orphan
            try:
                ch = bot.get_channel(int(hosted[0]))
                if ch is not None:
                    await ch.get_partial_message(int(hosted[1])).delete()
            except Exception:
                pass
            return False, f"there is no picture {slot} in the gallery"
        wg.note_upload(guild_id, clone_id)
        if old[0] and old[1]:
            try:
                ch = bot.get_channel(int(old[0]))
                if ch is not None:
                    await ch.get_partial_message(int(old[1])).delete()
            except Exception:
                pass   # already gone, or no permission: the owner reviews the hosting channel anyway
        return True, ""
    except Exception:
        logger.exception("[cardwz] replacing picture %s failed for guild %s", slot, guild_id)
        return False, "something went wrong, try again"


class CardPictureReplaceModal(discord.ui.Modal, title="Replace a picture"):
    def __init__(self, guild_id: int, clone_id, invoker_id):
        super().__init__()
        self.guild_id, self.clone_id, self.invoker_id = guild_id, clone_id, invoker_id
        self.number = discord.ui.TextInput(style=discord.TextStyle.short, required=True, max_length=2, placeholder="1 to 10")
        self.upload = discord.ui.FileUpload(required=False, min_values=0, max_values=1)
        self.url = discord.ui.TextInput(
            style=discord.TextStyle.short, required=False, max_length=500, placeholder="https://…/image.png",
        )
        self.add_item(discord.ui.Label(text="Picture number", description="It keeps its place in the order.", component=self.number))
        self.add_item(discord.ui.Label(text="New picture", description="png or jpg, 8MB", component=self.upload))
        self.add_item(discord.ui.Label(text="…or paste a direct image link", component=self.url))

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        from discord_bot.cogs.welcome import (
            CUSTOM_BG_ALLOWED_CONTENT_TYPES, CUSTOM_BG_MAX_BYTES, _fetch_custom_bg_bytes,
        )
        from modules import welcome_gallery as wg
        from modules.welcome_studio import studio_unlocked, LOCKED_LINE
        try:
            n = int(str(self.number.value or "").strip())
        except ValueError:
            n = 0
        if not 1 <= n <= wg.MAX_PICTURES:
            await interaction.followup.send(f"⚠️ Type a number from 1 to {wg.MAX_PICTURES}.", ephemeral=True)
            return
        files = list(self.upload.values or [])[:1]
        url = str(self.url.value or "").strip()
        data, why = None, None
        if files:
            f = files[0]
            ct = (f.content_type or "").split(";")[0].strip().lower()
            if ct not in CUSTOM_BG_ALLOWED_CONTENT_TYPES:
                why = "not a png/jpg"
            elif f.size > CUSTOM_BG_MAX_BYTES:
                why = "over 8MB"
            else:
                try:
                    data = await f.read()
                except Exception:
                    why = "could not be read"
        elif url:
            async with aiohttp.ClientSession() as session:
                data, why = await _fetch_custom_bg_bytes(session, url)
        else:
            await interaction.followup.send("⚠️ Upload a picture or paste a link first.", ephemeral=True)
            return
        if data is None:
            await interaction.followup.send(f"⚠️ Couldn't use that picture ({why or 'could not be read'}).", ephemeral=True)
            return
        if not await studio_unlocked(self.guild_id, self.clone_id):
            # Preview only: show the new picture on the card in memory. Nothing is hosted or stored.
            small = await asyncio.to_thread(_shrink_bg, data)
            if small is None:
                await interaction.followup.send("⚠️ Couldn't read that picture. Use png or jpg under 8MB.", ephemeral=True)
                return
            _get_draft(self.guild_id, self.clone_id, interaction.user.id, create=True)["bg"] = small
            await interaction.followup.send(
                f"\U0001F512 Preview only: {LOCKED_LINE} Pictures are stored and managed with Premium.", ephemeral=True)
            await _rerender(interaction, self.guild_id, self.clone_id, self.invoker_id)
            return
        ok, reason = await _replace_picture(
            interaction.client, interaction.guild or interaction.client.get_guild(self.guild_id),
            self.guild_id, self.clone_id, n, data)
        if not ok:
            await interaction.followup.send(f"⚠️ Picture {n} was not replaced: {reason}.", ephemeral=True)
            return
        await interaction.followup.send(f"✅ Picture {n} replaced.", ephemeral=True)
        await _rerender(interaction, self.guild_id, self.clone_id, self.invoker_id)


class CardPictureReplaceButton(_Btn, discord.ui.DynamicItem[discord.ui.Button], template=_id_pattern("picreplace")):
    FIELD, LABEL = "picreplace", "\U0001F501 Replace a picture"

    async def callback(self, interaction: discord.Interaction):
        if not await _check_access(interaction, self.invoker_id, self.guild_id):
            return
        try:
            await interaction.response.send_modal(CardPictureReplaceModal(self.guild_id, self.clone_id, self.invoker_id))
        except discord.HTTPException as e:
            if e.code != 40060:  # double-click: modal already open
                raise


class CardStudioPremiumButton(_Btn, discord.ui.DynamicItem[discord.ui.Button], template=_id_pattern("studiogoprem")):
    FIELD, STYLE = "studiogoprem", discord.ButtonStyle.primary

    def label_text(self, *_):
        from modules.welcome_studio import UNLOCK_BUTTON_LABEL
        return f"\U0001F48E {UNLOCK_BUTTON_LABEL}"

    async def callback(self, interaction: discord.Interaction):
        if not await _check_access(interaction, self.invoker_id, self.guild_id):
            return
        from discord_bot.cogs._views_premium import send_premium_pitch
        await interaction.response.defer(ephemeral=True)
        await send_premium_pitch(interaction, self.guild_id, self.clone_id)


class CardTextPosButton(_Btn, discord.ui.DynamicItem[discord.ui.Button], template=_id_pattern("textpos")):
    """Studio 3d: cycles the text block through Automatic, then the nine cells of a 3x3 grid (top left ... bottom right).
    A button, not a select: the Text step is already at the 5-select / 40-component budget in More mode."""
    FIELD = "textpos"

    def __init__(self, guild_id: int, clone_id, invoker_id, pos: str = "auto"):
        super().__init__(guild_id, clone_id, invoker_id, pos)

    def label_text(self, pos="auto", *_):
        return f"\U0001F4CD Text spot: {_TEXT_POS_LABELS.get(pos, 'Automatic')}"

    async def callback(self, interaction: discord.Interaction):
        if not await _check_access(interaction, self.invoker_id, self.guild_id):
            return
        await interaction.response.defer()
        cur = (await _current_opts(self.guild_id, self.clone_id, interaction.user.id))["text_pos"]
        order = tuple(_TEXT_POS_LABELS)
        nxt = order[(order.index(cur) + 1) % len(order)] if cur in order else "auto"
        await _save_option(self.guild_id, self.clone_id, interaction.user.id, text_pos=nxt)
        await _rerender(interaction, self.guild_id, self.clone_id, self.invoker_id)


class _NudgeButton(_Btn):
    """Studio 3c: moves the text block by _NUDGE_STEP px per tap. The stored value is clamped to +-300; the renderer's
    safe-area guard then keeps the text on the card and off the avatar."""
    AXIS, DELTA = "text_dx", 0

    async def callback(self, interaction: discord.Interaction):
        if not await _check_access(interaction, self.invoker_id, self.guild_id):
            return
        await interaction.response.defer()
        cur = (await _current_opts(self.guild_id, self.clone_id, interaction.user.id))[self.AXIS]
        new = max(-ULTRA_NUDGE_LIMIT, min(ULTRA_NUDGE_LIMIT, cur + self.DELTA))
        if new != cur:
            await _save_option(self.guild_id, self.clone_id, interaction.user.id, **{self.AXIS: new})
        await _rerender(interaction, self.guild_id, self.clone_id, self.invoker_id)


class CardNudgeLeftButton(_NudgeButton, discord.ui.DynamicItem[discord.ui.Button], template=_id_pattern("nudgeleft")):
    FIELD, LABEL, AXIS, DELTA = "nudgeleft", "\u25C0", "text_dx", -_NUDGE_STEP


class CardNudgeUpButton(_NudgeButton, discord.ui.DynamicItem[discord.ui.Button], template=_id_pattern("nudgeup")):
    FIELD, LABEL, AXIS, DELTA = "nudgeup", "\u25B2", "text_dy", -_NUDGE_STEP


class CardNudgeDownButton(_NudgeButton, discord.ui.DynamicItem[discord.ui.Button], template=_id_pattern("nudgedown")):
    FIELD, LABEL, AXIS, DELTA = "nudgedown", "\u25BC", "text_dy", _NUDGE_STEP


class CardNudgeRightButton(_NudgeButton, discord.ui.DynamicItem[discord.ui.Button], template=_id_pattern("nudgeright")):
    FIELD, LABEL, AXIS, DELTA = "nudgeright", "\u25B6", "text_dx", _NUDGE_STEP


class CardNudgeCenterButton(_Btn, discord.ui.DynamicItem[discord.ui.Button], template=_id_pattern("nudgecenter")):
    """Puts the text back where the layout puts it."""
    FIELD, LABEL = "nudgecenter", "\u21BA Reset"

    async def callback(self, interaction: discord.Interaction):
        if not await _check_access(interaction, self.invoker_id, self.guild_id):
            return
        await interaction.response.defer()
        cur = await _current_opts(self.guild_id, self.clone_id, interaction.user.id)
        if cur["text_dx"] or cur["text_dy"]:
            await _save_option(self.guild_id, self.clone_id, interaction.user.id, text_dx=0, text_dy=0)
        await _rerender(interaction, self.guild_id, self.clone_id, self.invoker_id)


DYNAMIC_ITEMS = (
    CardBannerSelect, CardDimSelect, CardSideSelect, CardColorSelect, CardShapeSelect, CardStyleSelect,
    CardLayoutSelect, CardFontSelect, CardBracketSelect, CardFocusSelect, CardRatioSelect,
    CardBackgroundButton, CardClearBackgroundButton, CardUnlockButton,
    CardHeadingButton, CardNumberToggleButton, CardResetButton, CardPreviewButton, CardDoneButton,
    CardTabButton, CardPresetSelect,
    CardMoreButton, CardUndoButton, CardStepResetButton, CardTestSendButton,
    CardRotationSelect,
    CardPictureRemoveButton, CardPictureFirstButton, CardStudioPremiumButton, CardPictureAddButton,
    CardPictureReplaceButton,
    CardPanLeftButton, CardPanUpButton, CardPanDownButton, CardPanRightButton, CardZoomButton, CardTextSizeButton,
    CardFrameButton, CardFrameColorButton, CardAvatarPosButton, CardEffectButton, CardHeadingFontButton, CardOutlineButton, CardBrightnessButton,
    CardDesignSelect, CardDesignSaveButton, CardDesignRenameButton, CardDesignDeleteButton, CardDesignScheduleButton,
    CardTextPosButton,
    CardNudgeLeftButton, CardNudgeUpButton, CardNudgeDownButton, CardNudgeRightButton, CardNudgeCenterButton,
)
