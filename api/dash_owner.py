# path: api/dash_owner.py
"""Owner area, Phase 1: READ-ONLY pages. Called from api/dash.py's router.

Every handler reuses the same modules/admin_*.py logic the Discord Owner panel uses. Nothing here
writes. Access is decided by dash._require_section, never by the client.

The web service is a separate process from the bot worker, so live bot state (uptime, latency,
in-memory error counts, log ring buffer) is NOT available here; Health reports what the database
knows (DB round-trip, server counts, clone heartbeats). Live stats need the worker to publish a
snapshot (Phase 1b).
"""
import logging
from datetime import date, datetime, timezone
from decimal import Decimal

logger = logging.getLogger(__name__)

_JS_SAFE = 2 ** 53


def jsonable(v):
    """Make DB rows JSON-safe. Ids and any int beyond 2**53 become strings (Discord snowflakes
    lose precision in JavaScript numbers); datetimes become ISO strings; Decimals become floats."""
    if isinstance(v, dict):
        return {k: (str(x) if isinstance(x, int) and not isinstance(x, bool) and (str(k).endswith("id") or abs(x) >= _JS_SAFE)
                    else jsonable(x)) for k, x in v.items()}
    if isinstance(v, (list, tuple, set)):
        return [jsonable(x) for x in v]
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, int) and not isinstance(v, bool) and abs(v) >= _JS_SAFE:
        return str(v)
    return v


def _snowflake(raw):
    from modules import admin_inspect as ai
    n = ai.parse_snowflake(raw or "")
    return n


async def health() -> dict:
    from modules import admin_inspect as ai
    ping = await ai.db_ping()
    try:
        counts = await ai.guild_counts()
    except Exception:
        logger.exception("owner health: guild counts failed")
        counts = None
    beats = await ai.clone_heartbeats()
    quiet = ai.quiet_clones(beats) if beats is not None else None
    now = datetime.now(timezone.utc)
    return {"db_ping_ms": None if ping is None else round(ping, 1),
            "servers": counts,
            "clones_active": None if beats is None else len(beats),
            "clones_quiet": None if quiet is None else [
                {"clone_id": r["clone_id"], "bot_username": r.get("bot_username"), "last_heartbeat": r.get("last_heartbeat")}
                for r in quiet],
            "heartbeat_stale_minutes": ai.HEARTBEAT_STALE_MIN,
            "live_bot": None,          # uptime/latency/errors need the worker snapshot (Phase 1b)
            "as_of": now}


async def servers(q) -> dict:
    page = q("page") or "0"
    if not page.isdigit() or int(page) > 10_000:
        return {"_error": (422, "Bad page.")}
    clone = q("clone")
    if clone and clone != "main" and not clone.isdigit():
        return {"_error": (422, "Bad clone filter.")}
    from modules import admin_inspect as ai
    res = await ai.server_list(q("q") or "", clone, (q("active") or "1") != "0", int(page))
    return {**res, "per_page": ai.SERVER_PAGE}


async def server(q) -> dict:
    gid = _snowflake(q("guild_id"))
    if gid is None:
        return {"_error": (422, "That doesn't look like a server id.")}
    from modules import admin_inspect as ai
    rows = await ai.server_rows(gid)
    if not rows:
        return {"_error": (404, "The bot has never been in that server.")}
    extras = await ai.server_extras(gid)
    return {"guild_id": gid, "bots": rows, **extras}


async def user(q) -> dict:
    uid = _snowflake(q("user_id"))
    if uid is None:
        return {"_error": (422, "That doesn't look like a user id.")}
    from modules import admin_inspect as ai
    return await ai.user_card(uid)


async def payments(q) -> dict:
    from modules import admin_money as am
    view = q("view") or "pending"
    if view == "pending":
        return await am.list_pending(50)
    if view == "failures":
        return {"rows": await am.list_failures(50)}
    if view == "revenue":
        period = q("period") or "day"
        n = q("n") or ("30" if period == "day" else "12")
        if period not in ("day", "week") or not n.isdigit():
            return {"_error": (422, "Bad revenue range.")}
        n = max(1, min(int(n), 90 if period == "day" else 52))
        return {"period": period, "series": await am.revenue_series(period, n)}
    return {"_error": (422, "Unknown payments view.")}


async def expiries(q) -> dict:
    from modules import admin_money as am
    days = q("days") or "7"
    if not days.isdigit():
        return {"_error": (422, "Bad window.")}
    return {"rows": await am.upcoming_expiries(int(days))}


# action -> (section, handler). Read-only; the router rate-limits and serialises.
ROUTES = {
    "owner_health": ("health", lambda q: health()),
    "owner_servers": ("servers", servers),
    "owner_server": ("inspect", server),
    "owner_user": ("inspect", user),
    "owner_payments": ("money", payments),
    "owner_expiries": ("money", expiries),
}
