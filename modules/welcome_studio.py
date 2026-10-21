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

import logging
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
STUDIO_KEYS = frozenset({"zoom", "pan_x", "pan_y", "text_size", "frame", "frame_color", "text_dx", "text_dy", "avatar_pos", "effect", "heading_font"})
STUDIO_DEFAULTS = {"zoom": 100, "pan_x": 0, "pan_y": 0, "text_size": "medium", "frame": "none", "frame_color": "white",
                   "text_dx": 0, "text_dy": 0, "avatar_pos": "auto", "effect": "none", "heading_font": "same"}


def uses_studio_options(raw) -> bool:
    """True when the stored options differ from the studio defaults. Never raises; untouched servers return False
    so the join render does not even need a Premium lookup for them."""
    try:
        from modules.welcome_card import parse_ultra_options
        o = parse_ultra_options(raw)
        return any(o.get(k) != v for k, v in STUDIO_DEFAULTS.items())
    except Exception:
        return False


async def options_for_render(config: dict):
    """What to hand the card renderer at join time: the stored options, with studio-only keys dropped unless the server
    has Premium/Plus (fails closed, never raises). Untouched servers get their stored value back unchanged."""
    raw = (config or {}).get("ultra_card_json")
    try:
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


def effective_studio_options(options: dict, unlocked: bool, studio_keys: frozenset = frozenset()) -> dict:
    """Return `options` unchanged when unlocked, else a copy without the studio-only keys.

    `studio_keys` is filled in by later phases as studio options are added, so one function
    decides what a lapsed or Ultra-only server may render. Today the set is empty, so the
    default look of every server is untouched.
    """
    if unlocked or not studio_keys:
        return options
    return {k: v for k, v in options.items() if k not in studio_keys}
