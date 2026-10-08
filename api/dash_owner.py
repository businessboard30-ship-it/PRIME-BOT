# path: api/dash_owner.py
"""Owner area. Called from api/dash.py's router.

ROUTES   (GET)  read-only pages. Handlers take `q` and return a dict; they never write.
WRITES   (POST) Phase 2 safe controls. Each entry is (section, rate_per_min, prepare). `prepare(sess, body)`
                validates and returns a plan {target, detail, fn, fresh?, confirm?}; dash.py then enforces the
                section, rate limit, step-up and typed confirmation and runs `fn` through the FAIL-CLOSED audit
                (dash._owner_write). Nothing here decides access.

Every handler reuses the same modules/admin_*.py logic the Discord Owner panel uses.

The web service is a separate process from the bot worker, so live bot state (uptime, latency, log ring
buffer, the worker's config) comes from bot_status_snapshots, which the worker publishes every ~60 s
(modules/admin_snapshot.py). The web only reads it and says how old it is.
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
    live = await _live("main", now)
    return {"db_ping_ms": None if ping is None else round(ping, 1),
            "servers": counts,
            "clones_active": None if beats is None else len(beats),
            "clones_quiet": None if quiet is None else [
                {"clone_id": r["clone_id"], "bot_username": r.get("bot_username"), "last_heartbeat": r.get("last_heartbeat")}
                for r in quiet],
            "heartbeat_stale_minutes": ai.HEARTBEAT_STALE_MIN,
            "live_bot": live,          # None until the worker has published once
            "as_of": now}


def _db():
    from api import dash          # lazy: dash imports this module; tests fake dash.db
    return dash.db


async def _snapshot(key: str):
    try:
        return await _db().bot_snapshot_get(key)
    except Exception:
        logger.exception("owner: snapshot read failed (%s)", key)
        return None


def _freshness(row, now=None) -> dict:
    from modules import admin_snapshot as sn
    age = sn.age_seconds(row["updated_at"], now)
    return {"age_s": round(age), "stale": age > sn.STALE_AFTER_S, "updated_at": row["updated_at"]}


async def _live(key: str, now=None):
    row = await _snapshot(key)
    if not row:
        return None
    return {**{k: v for k, v in row["payload"].items() if k != "published_at"}, **_freshness(row, now)}


async def logs(q) -> dict:
    row = await _snapshot("logs")
    if not row:
        return {"lines": [], "available": False}
    lines = row["payload"].get("lines", [])
    level = (q("level") or "").upper()
    if level in ("ERROR", "WARNING"):
        lines = [l for l in lines if l.get("level") == level]
    return {"lines": lines, "available": True, **_freshness(row)}


async def config_view(q) -> dict:
    row = await _snapshot("config")
    if not row:
        return {"entries": [], "available": False}
    needle = (q("q") or "").strip().lower()[:40]
    entries = [e for e in row["payload"].get("entries", []) if not needle or needle in e.get("name", "").lower()]
    return {"entries": entries, "available": True, "source": "bot worker", **_freshness(row)}


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


# ───────────────────────── Phase 2: safe controls ─────────────────────────

async def controls(q) -> dict:
    from modules import admin_controls as ac
    engaged = await ac.get_engaged_switches()
    rows = [{"key": ac.MAINTENANCE, "label": "Maintenance mode (blocks every command for everyone)",
             "kind": "maintenance", "engaged": ac.MAINTENANCE in engaged}]
    rows += [{"key": k, "label": label, "kind": "feature", "engaged": k in engaged} for k, (label, _) in ac.FEATURES.items()]
    rows += [{"key": k, "label": label, "kind": "opt_in", "engaged": k in engaged} for k, label in ac.OPT_IN.items()]
    return {"switches": rows, "note": "Features: engaged means turned OFF. Opt-in: engaged means turned ON. The bot picks changes up within about 20 seconds."}


async def blacklist(q) -> dict:
    from modules import admin_controls as ac
    return {"rows": await ac.list_blacklist(100)}


async def premium(q) -> dict:
    from modules import admin_controls as ac
    return {"rows": await ac.list_premium(100)}


async def bot_audit(q) -> dict:
    """The bot's own admin_panel_audit (Discord panel actions), with filters. The web trail is owner_audit."""
    from modules import admin_controls as ac
    before, gid, aid = q("before"), q("guild_id"), q("admin_id")
    action = (q("what") or "").strip()[:100] or None
    for v in (before, gid, aid):
        if v and not str(v).isdigit():
            return {"_error": (422, "Bad filter.")}
    if gid and _snowflake(gid) is None or aid and _snowflake(aid) is None:
        return {"_error": (422, "Ids must be full ids.")}
    rows = await ac.list_bot_audit(int(before) if before else None, action,
                                   int(gid) if gid else None, int(aid) if aid else None, 50)
    return {"rows": [{**r, "details": str(r.get("details") or "")[:500]} for r in rows]}


async def feedback(q) -> dict:
    rows = await _db().get_discord_user_feedback(50)
    return {"rows": [{**r, "message": str(r.get("message") or "")[:1000]} for r in rows]}


def _owner_ids() -> set:
    import config
    return set(config.DASH_OWNER_IDS) | set(config.DISCORD_CLONE_ADMIN_IDS) | set(config.DISCORD_OWNER_BROADCAST_IDS)


def _actor(sess) -> int:
    return int(sess["user"]["id"])


def _clean_reason(raw) -> str:
    return "".join(ch for ch in str(raw or "") if ch.isprintable())[:300].strip()


def _flag(body, key):
    v = body.get(key)
    return v if isinstance(v, bool) else None


async def prep_switch(sess, body) -> dict:
    from modules import admin_controls as ac
    key, engaged = body.get("switch"), _flag(body, "engaged")
    if not isinstance(key, str) or engaged is None or (key != ac.MAINTENANCE and key not in ac.FEATURES and key not in ac.OPT_IN):
        return {"_error": (422, "Unknown switch.")}
    plan = {"target": key, "detail": {"engaged": engaged},
            "fn": lambda: _do_switch(ac, key, engaged, _actor(sess))}
    if engaged and key == ac.MAINTENANCE:
        plan.update(fresh=True, confirm="MAINTENANCE")      # takes the whole bot offline for users
    elif engaged and key in ac.OPT_IN:
        plan.update(fresh=True, confirm="OPEN")             # opens a restricted feature to everyone
    return plan


async def _do_switch(ac, key, engaged, by):
    await ac.set_switch(key, engaged, by)
    return {"switch": key, "engaged": engaged}


async def prep_blacklist_add(sess, body) -> dict:
    from modules import admin_controls as ac
    kind, tid = body.get("kind"), _snowflake(str(body.get("target_id") or ""))
    if kind not in ("user", "guild") or tid is None:
        return {"_error": (422, "Pick user or server and give a full id.")}
    if kind == "user" and tid in _owner_ids():
        return {"_error": (422, "You can't blacklist an owner.")}
    reason = _clean_reason(body.get("reason"))
    plan = {"target": f"{kind}:{tid}", "detail": {"has_reason": bool(reason)},
            "fn": lambda: _do_bl_add(ac, kind, tid, reason, _actor(sess))}
    if kind == "guild":
        plan.update(fresh=True, confirm="BLOCK")      # cuts a whole server off: step-up + typed confirm
    return plan


async def _do_bl_add(ac, kind, tid, reason, by):
    await ac.add_blacklist(kind, tid, reason, by)
    return {"kind": kind, "target_id": tid}


async def prep_blacklist_remove(sess, body) -> dict:
    from modules import admin_controls as ac
    kind, tid = body.get("kind"), _snowflake(str(body.get("target_id") or ""))
    if kind not in ("user", "guild") or tid is None:
        return {"_error": (422, "Pick user or server and give a full id.")}
    async def fn():
        return {"removed": bool(await ac.remove_blacklist(kind, tid))}
    return {"target": f"{kind}:{tid}", "fn": fn}


async def prep_premium_revoke(sess, body) -> dict:
    from modules import admin_controls as ac, admin_inspect as ai
    gid = _snowflake(str(body.get("guild_id") or ""))
    ok, clone = ai.parse_clone(str(body.get("clone") or ""))
    if gid is None or not ok:
        return {"_error": (422, "Give a full server id (and a clone number, or leave it empty for the main bot).")}
    async def fn():
        return {"revoked": bool(await ac.revoke_premium(gid, clone))}
    return {"target": f"{gid}/{clone or 'main'}", "fn": fn, "fresh": True, "confirm": "REVOKE"}


async def prep_premium_grant(sess, body) -> dict:
    from modules import admin_controls as ac, admin_inspect as ai
    gid = _snowflake(str(body.get("guild_id") or ""))
    ok, clone = ai.parse_clone(str(body.get("clone") or ""))
    days = str(body.get("days") or "").strip()
    if gid is None or not ok:
        return {"_error": (422, "Give a full server id (and a clone number, or leave it empty for the main bot).")}
    if not days.isdigit() or not 1 <= int(days) <= ac.GRANT_MAX_DAYS:
        return {"_error": (422, f"Days must be a whole number from 1 to {ac.GRANT_MAX_DAYS}.")}
    n = int(days)
    async def fn():
        expires = await ac.grant_premium(gid, _actor(sess), n, clone)
        return {"guild_id": gid, "days": n, "expires_at": expires}
    return {"target": f"{gid}/{clone or 'main'}", "detail": {"days": n}, "fn": fn, "fresh": True, "confirm": "GRANT"}


# ───────────────────────── Phase 3: money (database-only actions) ─────────────────────────
# Approve / reject of manual payments is NOT here: it runs unlock handlers and DMs the buyer through the
# live bot, so it needs the owner_jobs queue (schema 62) and the real-payment test first.

import re as _re
_REF_RE = _re.compile(r"^[A-Za-z0-9_.:-]{3,120}$")
_PAY_FIELDS = ("payment_id", "paystack_reference", "user_id", "amount", "status", "payment_type", "provider",
               "chat_id", "clone_id", "created_date", "reversed_at", "reversed_by")


def _pay_summary(row: dict) -> dict:
    return {k: row.get(k) for k in _PAY_FIELDS}


async def payment(q) -> dict:
    from modules import admin_money as am
    ref = (q("reference") or "").strip()
    if not _REF_RE.match(ref):
        return {"_error": (422, "That doesn't look like a payment reference.")}
    row = await am.find_payment(ref)
    if not row:
        return {"_error": (404, "No payment found with that reference.")}
    problem = am.reversal_problem(row)
    return {"payment": _pay_summary(row), "can_reverse": problem is None, "problem": problem}


async def coupons(q) -> dict:
    from modules import admin_money as am
    return {"rows": [{**c, "state": am.coupon_state(c)} for c in await am.list_coupons(50)]}


async def prep_payment_reverse(sess, body) -> dict:
    from modules import admin_money as am
    ref = str(body.get("reference") or "").strip()
    if not _REF_RE.match(ref):
        return {"_error": (422, "That doesn't look like a payment reference.")}
    row = await am.find_payment(ref)
    problem = am.reversal_problem(row)
    if problem:
        return {"_error": (422, problem.replace("`", ""))}
    pid = row["payment_id"]
    async def fn():
        res = await am.reverse_payment(pid, _actor(sess))
        if not res.get("ok"):
            return {"changed": False, "message": "Nothing changed: already reversed or no longer reversible."}
        return {"changed": True, "days": res["days"], "expires_at": res.get("expires_at"),
                "message": "Reversed. Refund the money at the gateway if needed."}
    return {"target": ref, "detail": {"payment_id": pid, "type": row.get("payment_type")}, "fn": fn,
            "fresh": True, "confirm": "REVERSE"}


async def prep_coupon_create(sess, body) -> dict:
    from modules import admin_money as am
    code = am.normalize_code(str(body.get("code") or ""))
    pct = am.parse_int(str(body.get("percent") or ""), 1, 100)
    uses_raw, days_raw = str(body.get("max_uses") or "").strip(), str(body.get("days") or "").strip()
    uses = am.parse_int(uses_raw, 1, 1_000_000) if uses_raw else None
    days = am.parse_int(days_raw, 1, 3650) if days_raw else None
    if code is None:
        return {"_error": (422, "The code must be 3-24 characters: letters, digits, - or _.")}
    if pct is None:
        return {"_error": (422, "Percent off must be a whole number from 1 to 100.")}
    if uses_raw and uses is None:
        return {"_error": (422, "Max uses must be a whole number, or blank.")}
    if days_raw and days is None:
        return {"_error": (422, "Days must be a whole number up to 3650, or blank.")}
    async def fn():
        return {"created": bool(await am.create_coupon(code, pct, uses, days, _actor(sess))), "code": code}
    plan = {"target": code, "detail": {"percent": pct, "max_uses": uses, "days": days}, "fn": fn}
    if pct >= 50:
        plan.update(fresh=True, confirm="CREATE")        # a deep discount is money left on the table
    return plan


async def prep_coupon_toggle(sess, body) -> dict:
    from modules import admin_money as am
    code, active = am.normalize_code(str(body.get("code") or "")), _flag(body, "active")
    if code is None or active is None:
        return {"_error": (422, "Pick a code and on or off.")}
    async def fn():
        return {"updated": bool(await am.set_coupon_active(code, active)), "code": code, "active": active}
    return {"target": code, "detail": {"active": active}, "fn": fn}


async def prep_failure_dismiss(sess, body) -> dict:
    from modules import admin_money as am
    raw = str(body.get("id") or "")
    if not raw.isdigit() or int(raw) > 2 ** 62:
        return {"_error": (422, "Bad failure id.")}
    fid = int(raw)
    async def fn():
        return {"dismissed": bool(await am.dismiss_failure(fid, _actor(sess)))}
    return {"target": raw, "fn": fn}


async def prep_pending_clear(sess, body) -> dict:
    from modules import admin_money as am
    async def fn():
        return {"expired": int(await am.clear_old_pending()), "older_than_hours": am.CLEAR_PENDING_HOURS}
    return {"target": f">{am.CLEAR_PENDING_HOURS}h", "fn": fn, "fresh": True, "confirm": "CLEAR"}


async def prep_announce(sess, body) -> dict:
    from utils import dash_schema as S
    db = _db()
    clean, err = S.validate_dropbox(body)
    if err:
        return {"_error": (422, err)}
    recipients = []
    if clean["audience"] != "all" or clean["push_dm"]:
        recipients = await db.dropbox_resolve_recipients(clean["audience"], clean["min_members"], clean["target_guild_id"])
        if not recipients:
            return {"_error": (422, "No servers match that audience, so nobody would receive it.")}
    async def fn():
        mid = await db.dropbox_create(clean["title"], clean["body"], clean["kind"], clean["announce"],
                                      str(sess["user"]["id"]), clean["expires_hours"], clean["audience"],
                                      clean["min_members"], clean["target_guild_id"], clean["push_dm"])
        queued = await db.dropbox_enqueue(mid, recipients, clean["push_dm"]) if recipients else 0
        return {"id": str(mid), "recipients": queued}
    plan = {"target": clean["audience"], "detail": {"push_dm": clean["push_dm"], "announce": clean["announce"], "title_len": len(clean["title"])}, "fn": fn}
    if clean["push_dm"]:
        plan.update(fresh=True, confirm="SEND")             # mass DMs: step-up + typed confirm
    return plan


async def prep_announce_delete(sess, body) -> dict:
    try:
        mid = int(str(body.get("id")))
    except ValueError:
        return {"_error": (400, "Invalid message.")}
    async def fn():
        return {"deleted": bool(await _db().dropbox_delete(mid))}
    return {"target": str(mid), "fn": fn}


# action -> (section, per-minute limit, prepare). dash.py enforces section, rate, step-up, confirm, audit.
WRITES = {
    "owner_switch": ("controls", 20, prep_switch),
    "owner_blacklist_add": ("blacklist", 20, prep_blacklist_add),
    "owner_blacklist_remove": ("blacklist", 20, prep_blacklist_remove),
    "owner_premium_revoke": ("premium", 10, prep_premium_revoke),
    "owner_premium_grant": ("premium", 10, prep_premium_grant),
    "owner_payment_reverse": ("money", 5, prep_payment_reverse),
    "owner_coupon_create": ("money", 10, prep_coupon_create),
    "owner_coupon_toggle": ("money", 20, prep_coupon_toggle),
    "owner_failure_dismiss": ("money", 30, prep_failure_dismiss),
    "owner_pending_clear": ("money", 3, prep_pending_clear),
    "owner_announce": ("broadcast", 5, prep_announce),
    "owner_announce_delete": ("broadcast", 10, prep_announce_delete),
}

# action -> (section, handler). Read-only; the router rate-limits and serialises.
ROUTES = {
    "owner_health": ("health", lambda q: health()),
    "owner_servers": ("servers", servers),
    "owner_server": ("inspect", server),
    "owner_user": ("inspect", user),
    "owner_payments": ("money", payments),
    "owner_expiries": ("money", expiries),
    "owner_logs": ("logs", logs),
    "owner_config": ("config", config_view),
    "owner_controls": ("controls", controls),
    "owner_blacklist": ("blacklist", blacklist),
    "owner_premium": ("premium", premium),
    "owner_feedback": ("feedback", feedback),
    "owner_botaudit": ("audit", bot_audit),
    "owner_payment": ("money", payment),
    "owner_coupons": ("money", coupons),
}
