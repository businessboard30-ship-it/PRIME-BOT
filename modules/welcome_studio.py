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


def effective_studio_options(options: dict, unlocked: bool, studio_keys: frozenset = frozenset()) -> dict:
    """Return `options` unchanged when unlocked, else a copy without the studio-only keys.

    `studio_keys` is filled in by later phases as studio options are added, so one function
    decides what a lapsed or Ultra-only server may render. Today the set is empty, so the
    default look of every server is untouched.
    """
    if unlocked or not studio_keys:
        return options
    return {k: v for k, v in options.items() if k not in studio_keys}
