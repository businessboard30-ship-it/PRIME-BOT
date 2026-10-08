# path: modules/admin_insights.py
"""Owner-area insights (Phase 6): growth series, alert building, global search helpers.

Read-only. `build_alerts` and `bucket_days` are pure so they can be tested without a database.
"""
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

STALE_PENDING_HOURS = 6
REPORT_BACKLOG = 5
FAILURE_WARN = 1
MAX_GROWTH_DAYS = 90


async def _pool():
    from database import get_pool
    return await get_pool()


def bucket_days(n: int, now: Optional[datetime] = None) -> List[datetime]:
    """UTC midnights for the last `n` days, oldest first (today is the last one)."""
    now = now or datetime.now(timezone.utc)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return [today - timedelta(days=i) for i in range(n - 1, -1, -1)]


async def growth_series(days: int, now: Optional[datetime] = None) -> List[dict]:
    """[{start, joined, left}] per day, oldest first, gaps filled with zero."""
    days = max(1, min(int(days), MAX_GROWTH_DAYS))
    starts = bucket_days(days, now)
    pool = await _pool()
    async with pool.acquire() as conn:
        joined = await conn.fetch(
            "SELECT date_trunc('day', joined_at) AS d, COUNT(*) AS n FROM discord_guilds "
            "WHERE joined_at >= $1 GROUP BY 1", starts[0].replace(tzinfo=None))
        left = await conn.fetch(
            "SELECT date_trunc('day', left_at) AS d, COUNT(*) AS n FROM discord_guilds "
            "WHERE left_at IS NOT NULL AND left_at >= $1 GROUP BY 1", starts[0].replace(tzinfo=None))
    def key(d):
        return d.replace(tzinfo=None, hour=0, minute=0, second=0, microsecond=0)
    j = {key(r["d"]): int(r["n"]) for r in joined if r["d"]}
    l = {key(r["d"]): int(r["n"]) for r in left if r["d"]}
    return [{"start": s, "joined": j.get(key(s), 0), "left": l.get(key(s), 0)} for s in starts]


async def alert_facts(sections) -> Dict[str, object]:
    """Gather only what this user's sections allow. A failed lookup leaves that fact out."""
    from modules import admin_inspect as ai, admin_money as am, admin_safety as sf
    facts: Dict[str, object] = {}
    async def grab(name, coro):
        try:
            facts[name] = await coro
        except Exception:
            logger.exception("owner alerts: %s failed", name)
    if "health" in sections:
        beats = None
        try:
            beats = await ai.clone_heartbeats()
        except Exception:
            logger.exception("owner alerts: heartbeats failed")
        if beats is not None:
            facts["quiet_clones"] = len(ai.quiet_clones(beats))
    if "money" in sections:
        await grab("failures", am.list_failures(50))
        await grab("stale_pending", am.count_clearable_pending(STALE_PENDING_HOURS))
    if "reports" in sections:
        await grab("reports", sf.report_counts())
    return facts


def build_alerts(facts: dict, snapshot_age_s: Optional[float] = None, stale_after_s: int = 180,
                 sections=()) -> List[dict]:
    """Turn raw facts into [{id, level, title, page}]. level: bad > warn. Pure."""
    out: List[dict] = []
    if "health" in sections:
        if snapshot_age_s is None:
            out.append({"id": "worker-silent", "level": "warn", "title": "The bot worker has not published a status yet.", "page": "health"})
        elif snapshot_age_s > stale_after_s:
            out.append({"id": "worker-stale", "level": "bad", "title": "Bot worker status is %d min old. It may be down." % round(snapshot_age_s / 60), "page": "health"})
        q = facts.get("quiet_clones") or 0
        if q:
            out.append({"id": "clones-quiet", "level": "warn", "title": "%d clone(s) have gone quiet." % q, "page": "health"})
    if "money" in sections:
        f = facts.get("failures")
        if f is not None and len(f) >= FAILURE_WARN:
            out.append({"id": "pay-failures", "level": "bad", "title": "%d open payment failure(s)." % len(f), "page": "payments"})
        sp = facts.get("stale_pending") or 0
        if sp:
            out.append({"id": "pay-stale", "level": "warn", "title": "%d checkout(s) pending for over %d hours." % (sp, STALE_PENDING_HOURS), "page": "payments"})
    if "reports" in sections:
        new = int((facts.get("reports") or {}).get("new", 0))
        if new >= REPORT_BACKLOG:
            out.append({"id": "reports-backlog", "level": "warn", "title": "%d reports are waiting for a decision." % new, "page": "reports"})
    return sorted(out, key=lambda a: 0 if a["level"] == "bad" else 1)
