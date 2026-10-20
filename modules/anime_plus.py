# path: modules/anime_plus.py

"""
Anime feed D5: the upgrade ("two brains").

  has_anime_plus(user, guild, clone) = the SERVER is premium  OR  the PERSON has a personal, unexpired entitlement.

The personal entitlement is its own thing: it never makes a server premium. Exclusive features (more than 5 follows, DM alerts,
quiet hours) all go through this one check. Checks fail safe to False (a database hiccup never grants anything), and a positive
answer is cached for 30 s so a busy card does not hit the database per tap. The price is a config value in USD (config.ANIME_PLUS_FEE_USD).
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

PAYMENT_TYPE = "anime_plus"       # the key in payments_manual.UNLOCK_HANDLERS / gumroad products
PLUS_DAYS = 30                      # one payment buys this many days; renewing extends from the later of now and the current expiry
from modules.anime_feed import FREE_FOLLOW_LIMIT  # noqa: E402  (one source for the free limit)
PLUS_FOLLOW_LIMIT = 50
CACHE_TTL = 30.0
_cache: Dict[Tuple[int, int], Tuple[float, Optional[datetime]]] = {}      # (user, clone) -> (valid until, expires_at or None)

FEATURES = ("Follow up to 50 anime (free is 5)", "DM alerts", "Quiet hours")


def _clone(clone_id) -> int:
    return int(clone_id or 0)


async def _pool():
    from database import get_pool
    return await get_pool()


async def personal_expiry(user_id: int, clone_id=None) -> Optional[datetime]:
    """When the person's personal upgrade ends, or None when they have none (or it ended). Never raises."""
    key = (int(user_id), _clone(clone_id))
    hit = _cache.get(key)
    if hit and hit[0] > time.monotonic():
        return hit[1]
    try:
        pool = await _pool()
        async with pool.acquire() as conn:
            row = await conn.fetchval("SELECT expires_at FROM anime_plus_entitlements WHERE user_id = $1 AND clone_id = $2 AND status = 'active' AND expires_at > NOW()",
                                      key[0], key[1])
    except Exception:
        logger.debug("[anime-plus] couldn't read the entitlement", exc_info=True)
        return None                                                          # fail safe, and a failure is not cached
    _cache[key] = (time.monotonic() + CACHE_TTL, row)
    return row


async def server_premium(guild_id, clone_id=None) -> bool:
    if not guild_id:
        return False
    try:
        from modules import antiraid_pro
        return bool(await antiraid_pro.is_premium(int(guild_id), clone_id))
    except Exception:
        return False


async def status(user_id: int, guild_id=None, clone_id=None) -> Tuple[str, Optional[datetime]]:
    """('server' | 'personal' | 'none', personal expiry). A premium server wins: nobody in it needs to upgrade."""
    exp = await personal_expiry(user_id, clone_id)
    if await server_premium(guild_id, clone_id):
        return "server", exp
    return ("personal", exp) if exp else ("none", None)


async def has_anime_plus(user_id: int, guild_id=None, clone_id=None) -> bool:
    return (await status(user_id, guild_id, clone_id))[0] != "none"


async def follow_limit(user_id: int, guild_id=None, clone_id=None) -> int:
    return PLUS_FOLLOW_LIMIT if await has_anime_plus(user_id, guild_id, clone_id) else FREE_FOLLOW_LIMIT


# ── granting (called by the shared payment system's unlock handler) ──────

async def grant(user_id: int, clone_id, days: int, reference: str) -> Optional[datetime]:
    """Adds `days` of the personal upgrade (renewing extends from the later of now and the current expiry). Runs from
    payments_manual.UNLOCK_HANDLERS['anime_plus'], i.e. after ANY provider (Gumroad ping, Paystack verify, admin approve) confirmed
    the payment; those paths already claim a payment once. The same reference is also never applied twice here. Returns the new expiry."""
    pool = await _pool()
    async with pool.acquire() as conn:
        exp = await conn.fetchval(
            "INSERT INTO anime_plus_entitlements (user_id, clone_id, status, payment_reference, started_at, expires_at) "
            "VALUES ($1, $2, 'active', $3, NOW(), NOW() + make_interval(days => $4)) "
            "ON CONFLICT (user_id, clone_id) DO UPDATE SET status = 'active', payment_reference = EXCLUDED.payment_reference, "
            "expires_at = GREATEST(anime_plus_entitlements.expires_at, NOW()) + make_interval(days => $4) "
            "WHERE anime_plus_entitlements.payment_reference IS DISTINCT FROM EXCLUDED.payment_reference RETURNING expires_at",
            int(user_id), _clone(clone_id), str(reference), int(days))
        if exp is None:                                                        # this reference was already applied
            exp = await conn.fetchval("SELECT expires_at FROM anime_plus_entitlements WHERE user_id = $1 AND clone_id = $2", int(user_id), _clone(clone_id))
    _cache.pop((int(user_id), _clone(clone_id)), None)                         # the upgrade shows on the next tap
    return exp
