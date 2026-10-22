"""Welcome Card Studio: the one gate every studio feature goes through (plan section 6).

Studio features (gallery, rotation, frames, positioning, effects, borders, saved designs)
belong to Premium and Premium Plus. The one-time Ultra pack and the 5-day Ultra trial do
NOT unlock them; they keep the old single-picture Ultra look exactly as before.

Rules (see docs/WELCOME_CARD_STUDIO_PLAN.md):
- Checked on the server (Save/Apply and the join render), not only by hiding buttons.
- Fails closed, softly: any lookup error means "not unlocked", so the join renders the plain
  Ultra look with picture 1 and never fails.
- Per (guild_id, clone_id): a clone never inherits the main bot's Premium.
- Nothing is deleted when Premium lapses; callers just stop using studio-only options.
"""
from __future__ import annotations

import datetime as _dt
import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)

# Shown on locked studio steps: one line and one button, never a repeated pop-up.
LOCKED_LINE = "Studio is part of Premium."
UNLOCK_BUTTON_LABEL = "Unlock with Premium"


async def studio_unlocked(guild_id: Optional[int], clone_id: Optional[int] = None) -> bool:
    """True when this server (for this clone) has active Premium or Premium Plus.

    Premium Plus keeps Premium active, so db.is_guild_premium_active covers both, including the
    existing grace period and lifetime plans. Never raises.
    """
    if not guild_id:
        return False
    try:
        from database import db
        return bool(await db.is_guild_premium_active(int(guild_id), clone_id))
    except Exception:
        logger.exception("[welcome-studio] premium lookup failed for guild %s", guild_id)
        return False


# Option keys (inside ultra_card_json) that only Premium/Plus servers may save or render. Add each new studio key here.
STUDIO_KEYS = frozenset({"zoom", "pan_x", "pan_y", "text_size", "frame", "frame_color", "text_dx", "text_dy", "avatar_pos", "effect", "heading_font", "text_outline", "brightness", "text_pos"})
STUDIO_DEFAULTS = {"zoom": 100, "pan_x": 0, "pan_y": 0, "text_size": "medium", "frame": "none", "frame_color": "white",
                   "text_dx": 0, "text_dy": 0, "avatar_pos": "auto", "effect": "none", "heading_font": "same", "text_outline": "auto", "brightness": "normal", "text_pos": "auto"}


def uses_studio_options(raw) -> bool:
    """True when the stored options differ from the studio defaults. Never raises; untouched servers return False
    so the join render does not even need a Premium lookup for them."""
    try:
        from modules.welcome_card import parse_ultra_options
        o = parse_ultra_options(raw)
        return any(o.get(k) != v for k, v in STUDIO_DEFAULTS.items())
    except Exception:
        return False


# ---- Phase 7a: scheduled designs. Decided at render time from today's date (UTC): no loop, nothing to miss. ----
_SCHED_CACHE: dict = {}          # (guild, clone_key, today) -> (expires_monotonic, design dict or None)
SCHED_TTL = 60.0                 # seconds; a raid of joins asks the database once a minute, not once per join
_SCHED_CACHE_MAX = 500


def _today_utc() -> _dt.date:
    return _dt.datetime.now(_dt.timezone.utc).date()


def forget_schedule_cache(guild_id=None, clone_id=None) -> None:
    """Drop the cached answer for one server (after its schedules change). No arguments clears everything."""
    try:
        if guild_id is None:
            _SCHED_CACHE.clear()
            return
        ck = -1 if clone_id is None else int(clone_id)
        for k in [k for k in _SCHED_CACHE if k[0] == int(guild_id) and k[1] == ck]:
            _SCHED_CACHE.pop(k, None)
    except Exception:
        _SCHED_CACHE.clear()


async def scheduled_design(config: dict, today: Optional[_dt.date] = None) -> Optional[dict]:
    """The saved design whose schedule covers today, for a Premium/Plus server only; None otherwise. Never raises.

    Servers that never scheduled anything have `design_schedule_on` False and cost nothing here (no Premium lookup, no
    designs query). A lapsed Premium, a lookup error or a bad row all mean None: the live look is used, nothing is deleted."""
    try:
        if not config or not config.get("design_schedule_on"):
            return None
        gid, cid = config.get("guild_id"), config.get("clone_id")
        if not gid:
            return None
        today = today or _today_utc()
        key = (int(gid), -1 if cid is None else int(cid), today)
        hit = _SCHED_CACHE.get(key)
        now = time.monotonic()
        if hit and hit[0] > now:
            return hit[1]
        result = None
        if await studio_unlocked(gid, cid):
            from database import db
            from modules.welcome_schedule import pick_scheduled
            picked = pick_scheduled(await db.design_list(int(gid), cid), today)
            result = dict(picked) if picked else None
        if len(_SCHED_CACHE) >= _SCHED_CACHE_MAX:
            _SCHED_CACHE.clear()
        _SCHED_CACHE[key] = (now + SCHED_TTL, result)
        return result
    except Exception:
        logger.exception("[welcome-studio] scheduled design lookup failed, using the live look")
        return None


async def options_for_render(config: dict):
    """What to hand the card renderer at join time: the stored options, with studio-only keys dropped unless the server
    has Premium/Plus (fails closed, never raises). Untouched servers get their stored value back unchanged.

    Phase 7a: while a scheduled design covers today, its look replaces the stored one (Premium/Plus only)."""
    raw = (config or {}).get("ultra_card_json")
    try:
        sched = await scheduled_design(config)
        if sched:
            from modules.welcome_designs import restore_look
            return restore_look(sched.get("options_json"), sched.get("avatar_shape"))[0]
        if not uses_studio_options(raw):
            return raw
        if await studio_unlocked((config or {}).get("guild_id"), (config or {}).get("clone_id")):
            return raw
        from modules.welcome_card import parse_ultra_options
        return effective_studio_options(parse_ultra_options(raw), False, STUDIO_KEYS)
    except Exception:
        logger.exception("[welcome-studio] option gate failed, using the stored options without studio keys")
        try:
            from modules.welcome_card import parse_ultra_options
            return effective_studio_options(parse_ultra_options(raw), False, STUDIO_KEYS)
        except Exception:
            return None


async def avatar_shape_for_render(config: dict) -> str:
    """The avatar shape to hand the card renderer: the stored one (never raises, unknown values fall back to circle).

    Every place that draws a welcome card passes this next to `options_for_render`, so the shape picked in the wizard is
    the shape that is drawn (join, /welcome preview, nudge post, test post, setup TV). Phase 7a: a scheduled design that
    covers today brings its own shape, the same way it brings its own options."""
    try:
        from modules.welcome_card import AVATAR_SHAPES, DEFAULT_AVATAR_SHAPE
        sched = await scheduled_design(config)
        shape = (sched or {}).get("avatar_shape") or (config or {}).get("avatar_shape")
        return shape if shape in AVATAR_SHAPES else DEFAULT_AVATAR_SHAPE
    except Exception:
        return "circle"


def effective_studio_options(options: dict, unlocked: bool, studio_keys: frozenset = frozenset()) -> dict:
    """Return `options` unchanged when unlocked, else a copy without the studio-only keys.

    `studio_keys` is filled in by later phases as studio options are added, so one function
    decides what a lapsed or Ultra-only server may render. Today the set is empty, so the
    default look of every server is untouched.
    """
    if unlocked or not studio_keys:
        return options
    return {k: v for k, v in options.items() if k not in studio_keys}
