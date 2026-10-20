# path: modules/anime_plus.py

"""
Anime feed D5: the upgrade ("two brains").

  has_anime_plus(user, guild, clone) = the SERVER is premium  OR  the PERSON has a personal, unexpired entitlement.

The personal entitlement is its own thing: it never makes a server premium. Exclusive features (more than 5 follows, DM alerts,
quiet hours) all go through this one check. Checks fail safe to False (a database hiccup never grants anything), and a positive
answer is cached for 30 s so a busy card does not hit the database per tap. The upgrade is a monthly SUBSCRIPTION (product "anime_plus" in modules/user_subs.py, stored in user_entitlements like the other
per-user plans); the price is config.ANIME_PLUS_FEE_USD. Days bought under the earlier one-off checkout are still honoured.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

PLUS_DAYS = 30                      # days a legacy one-off payment bought (the upgrade is now a monthly subscription, see modules/user_subs.py)
from modules.anime_feed import FREE_FOLLOW_LIMIT  # noqa: E402  (one source for the free limit)
PLUS_FOLLOW_LIMIT = 50
CACHE_TTL = 30.0
_cache: Dict[Tuple[int, int], Tuple[float, Optional[datetime], str]] = {}      # (user, clone) -> (valid until, expires_at or None, state)

FEATURES = ("Follow up to 50 anime (free is 5)", "DM alerts", "Quiet hours")


def _clone(clone_id) -> int:
    return int(clone_id or 0)


async def _pool():
    from database import get_pool
    return await get_pool()


async def _subscription_row(user_id: int):
    """The person's anime_plus row in user_entitlements (the shared per-user subscription table), or None."""
    from database import db
    return await db.entitlement_get(str(int(user_id)), "anime_plus")


async def _legacy_expiry(user_id: int, clone_id: int) -> Optional[datetime]:
    """Days bought under the earlier one-off checkout (anime_plus_entitlements). Still honoured until they run out; nothing writes it any more."""
    pool = await _pool()
    async with pool.acquire() as conn:
        return await conn.fetchval("SELECT expires_at FROM anime_plus_entitlements WHERE user_id = $1 AND clone_id = $2 AND status = 'active' AND expires_at > NOW()",
                                   int(user_id), int(clone_id))


def _later(a: Optional[datetime], b: Optional[datetime]) -> Optional[datetime]:
    if a is None or b is None:
        return a or b
    try:
        return max(a, b)
    except TypeError:                                                         # naive vs aware: compare as UTC
        aa = a if a.tzinfo else a.replace(tzinfo=timezone.utc)
        bb = b if b.tzinfo else b.replace(tzinfo=timezone.utc)
        return a if aa >= bb else b


async def _personal(user_id: int, clone_id=None, fresh: bool = False) -> Tuple[Optional[datetime], str]:
    """(access ends, state) for the person's own upgrade. state: 'active' (renews), 'cancelled' (ends at the date, will not renew),
    'past_due' (a renewal failed; short grace), 'legacy' (old one-off days), or 'none'. Never raises; fails safe to none, uncached."""
    from modules import entitlements as ent
    key = (int(user_id), _clone(clone_id))
    hit = _cache.get(key)
    if hit and hit[0] > time.monotonic() and not fresh:
        return hit[1], hit[2]
    failed = False
    sub_exp, state = None, "none"
    try:
        row = await _subscription_row(key[0])
        if row:
            eff = ent.effective(dict(row))
            if eff["access"]:
                sub_exp, state = row.get("expires_at"), eff["state"]
    except Exception:
        failed = True
        logger.debug("[anime-plus] couldn't read the subscription", exc_info=True)
    old_exp = None
    try:
        old_exp = await _legacy_expiry(*key)
    except Exception:
        failed = True
        logger.debug("[anime-plus] couldn't read the old entitlement", exc_info=True)
    exp = _later(sub_exp, old_exp)
    if exp is None:
        state = "none"
    elif sub_exp is None:
        state = "legacy"
    if not failed:                                                            # a failure is never cached
        _cache[key] = (time.monotonic() + CACHE_TTL, exp, state)
    return exp, state


async def personal_expiry(user_id: int, clone_id=None) -> Optional[datetime]:
    """When the person's personal upgrade ends, or None when they have none (or it ended). Never raises."""
    return (await _personal(user_id, clone_id))[0]


async def personal_state(user_id: int, clone_id=None, fresh: bool = False) -> str:
    return (await _personal(user_id, clone_id, fresh))[1]


def forget(user_id: int, clone_id=None) -> None:
    _cache.pop((int(user_id), _clone(clone_id)), None)


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


# ── checkout rows ───────────────────────────────────────────────────────

async def start_payment(user_id: int, clone_id, reference: str, provider: str, days: int = PLUS_DAYS) -> None:
    pool = await _pool()
    async with pool.acquire() as conn:
        await conn.execute("INSERT INTO anime_plus_payments (user_id, clone_id, payment_reference, provider, days) VALUES ($1, $2, $3, $4, $5) ON CONFLICT (payment_reference) DO NOTHING",
                           int(user_id), _clone(clone_id), reference, str(provider or "paystack"), int(days))


async def payment_status(reference: str) -> Optional[str]:
    pool = await _pool()
    async with pool.acquire() as conn:
        return await conn.fetchval("SELECT status FROM anime_plus_payments WHERE payment_reference = $1", reference)


async def latest_payment(user_id: int, clone_id=None) -> Optional[dict]:
    pool = await _pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM anime_plus_payments WHERE user_id = $1 AND clone_id = $2 ORDER BY id DESC LIMIT 1", int(user_id), _clone(clone_id))
    return dict(row) if row else None


async def complete_payment(reference: str) -> Optional[dict]:
    """Marks a PENDING payment completed and grants (or extends) the upgrade, in ONE transaction. Only a row still 'pending' completes,
    so the webhook and the person's Verify tap can both run and the days are added exactly once. Returns
    {'user_id', 'clone_id', 'expires_at'} for the caller that completed it, or None when there was nothing pending."""
    pool = await _pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow("UPDATE anime_plus_payments SET status = 'completed', completed_at = NOW() WHERE payment_reference = $1 AND status = 'pending' "
                                      "RETURNING user_id, clone_id, days", reference)
            if not row:
                return None
            exp = await conn.fetchval(
                "INSERT INTO anime_plus_entitlements (user_id, clone_id, status, payment_reference, started_at, expires_at) "
                "VALUES ($1, $2, 'active', $3, NOW(), NOW() + make_interval(days => $4)) "
                "ON CONFLICT (user_id, clone_id) DO UPDATE SET status = 'active', payment_reference = EXCLUDED.payment_reference, "
                "expires_at = GREATEST(anime_plus_entitlements.expires_at, NOW()) + make_interval(days => $4) RETURNING expires_at",
                int(row["user_id"]), int(row["clone_id"]), reference, int(row["days"]))
    _cache.pop((int(row["user_id"]), int(row["clone_id"])), None)           # the new upgrade shows on the next tap
    return {"user_id": int(row["user_id"]), "clone_id": int(row["clone_id"]), "expires_at": exp}
