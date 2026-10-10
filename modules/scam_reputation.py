# path: modules/scam_reputation.py
"""Shared Scam Shield memory: has this account been caught posting scams in OTHER servers?

Every catch is already written to scam_shield_hits (one shared database for the main bot and every clone), so
there is nothing new to store: a join just asks "any catches for this user in a different server?". The answer
only ever contains counts, kinds and the most recent time. It never names the other servers and never repeats
what the message said, so one server's moderation isn't exposed to another.

It is a heads-up for the admins, not a verdict: the account may be hacked, nothing is done to the member.
"""
import logging
from datetime import datetime
from typing import List, Optional, TypedDict

logger = logging.getLogger(__name__)

LOOKBACK_DAYS = 180          # older catches stop counting
MIN_CATCHES = 1              # catches in other servers before admins are told

KIND_LABELS = {
    "word": "known scam wording",
    "domain": "a known scam link",
    "image": "a known scam image",
    "vision": "an AI-detected scam image",
}


class Reputation(TypedDict):
    catches: int
    servers: int
    last_at: Optional[datetime]
    kinds: List[str]          # friendly labels, stable order


async def _pool():
    from database import get_pool
    return await get_pool()


def label_kinds(kinds) -> List[str]:
    out: List[str] = []
    for k in sorted({str(k) for k in (kinds or []) if k}):
        lab = KIND_LABELS.get(k, "a scam pattern")
        if lab not in out:
            out.append(lab)
    return out


async def lookup(user_id: int, exclude_guild_id: int) -> Optional[Reputation]:
    """Catches for this user in servers other than `exclude_guild_id`, or None if there are none (or the lookup failed:
    a database hiccup must never block a join or raise a false alarm)."""
    try:
        pool = await _pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT COUNT(*) AS catches, COUNT(DISTINCT guild_id) AS servers, MAX(created_at) AS last_at, "
                "ARRAY_AGG(DISTINCT kind) AS kinds FROM scam_shield_hits "
                "WHERE user_id = $1 AND guild_id <> $2 AND created_at >= NOW() - make_interval(days => $3)",
                int(user_id), int(exclude_guild_id), LOOKBACK_DAYS)
    except Exception:
        logger.debug("[scam-reputation] lookup failed", exc_info=True)
        return None
    if not row or int(row["catches"] or 0) < MIN_CATCHES:
        return None
    return {"catches": int(row["catches"]), "servers": int(row["servers"] or 0), "last_at": row["last_at"],
            "kinds": label_kinds(row["kinds"])}
