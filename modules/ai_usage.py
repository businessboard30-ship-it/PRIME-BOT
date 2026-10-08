# path: modules/ai_usage.py
"""Weekly website-only AI chat allowance. Week = Monday 00:00 UTC. Counters are separate per source so the
card plan's 10 and the Developer plan's 50 never mix. No Discord command uses this allowance."""
from datetime import datetime, timedelta, timezone

LIMITS = {"card_plan": 10, "dev": 50}


def week_start(now=None):
    now = now or datetime.now(timezone.utc)
    return (now - timedelta(days=now.weekday())).date()


def resets_at(now=None) -> datetime:
    ws = week_start(now)
    return datetime(ws.year, ws.month, ws.day, tzinfo=timezone.utc) + timedelta(days=7)


async def status(db, uid, source="card_plan", now=None) -> dict:
    limit = LIMITS[source]
    used = await db.ai_usage_get(str(uid), week_start(now), source)
    return {"used": int(used), "limit": limit, "resets_at": resets_at(now).isoformat()}


async def consume(db, uid, source="card_plan", now=None):
    """One chat. -> (ok, status). The DB increments only while used < limit, so concurrent calls can't overshoot."""
    got = await db.ai_usage_consume(str(uid), week_start(now), source, LIMITS[source])
    st = await status(db, uid, source, now)
    return got is not None, st
