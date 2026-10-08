# path: api/dash_owner_ops.py
"""Owner area, Phase 5 (owner-only and dangerous): helper access, clones, named database cleanup.

Same contract as api/dash_owner.py: ROUTES = GET, WRITES = (section, per-minute limit, prepare). dash.py enforces
the section, session age, rate limit, step-up and typed confirmation and runs `fn` through the fail-closed audit.
Sections used: access, servers, database. All three are in admin_access.OWNER_ONLY or owner-only on the web in
v1, because the web gives helpers no access yet (dash._owner_sections passes no helper sections).

Reuses: modules/admin_controls.py (helpers), modules/admin_clones.py (clones), modules/admin_ops.py (database).

Deliberate limits:
  * No raw SQL. The only database action is the named stale-payment cleanup Discord also has.
  * Bot tokens are accepted on register/relink only, are never echoed, logged or audited, and clone rows are listed
    without the token column.
  * No owner_jobs queue: clone_manager.py already polls discord_cloned_bots.status, so stopping a clone is a DB write.
  * "Start a stopped clone" is not here (not in the plan). Relinking a clone you own does bring it back online.
"""
import logging
import re

logger = logging.getLogger(__name__)

TOKEN_RE = re.compile(r"^[A-Za-z0-9._\-]{40,120}$")   # cheap shape check; Discord validates for real
CLONE_LIMIT = 100


def _actor(sess) -> int:
    return int(sess["user"]["id"])


def _snowflake(raw):
    from modules import admin_inspect as ai
    return ai.parse_snowflake(raw or "")


# NB: the body field is "clone", not "clone_id": dash.py reserves clone_id as its clone-scope selector.
def _int(raw, lo=1, hi=2 ** 31 - 1):
    s = str(raw if raw is not None else "").strip()
    return int(s) if s.isdigit() and lo <= int(s) <= hi else None


def _owner_ids() -> set:
    from api import dash_owner
    return dash_owner._owner_ids()


def _refuse(status: int, message: str):
    from api import dash           # lazy: dash imports this module
    dash._fail(status, message)


# ───────────────────────── reads ─────────────────────────

async def helpers(q) -> dict:
    from modules import admin_controls as ac
    rows = await ac.list_helpers(100)
    return {"rows": [{**r, "sections": sorted(r["sections"])} for r in rows],
            "grantable": dict(ac.GRANTABLE),
            "note": "Helpers use these sections in Discord. They get no web access yet. "
                    "Changes reach the bot within about 20 seconds."}


async def clones(q) -> dict:
    from modules import admin_clones as acl
    rows = await acl.list_clones(CLONE_LIMIT)
    return {"rows": rows, "limit": CLONE_LIMIT, "stop_note": acl.STOP_NOTE}


async def database(q) -> dict:
    from modules import admin_ops as ops
    info = await ops.db_overview()
    return {"pool": info.get("pool"),
            "counts": [{"table": t, "rows": n, "approx": t in (info.get("approx") or ())}
                       for t, n in (info.get("counts") or [])],
            "stale_payments": info.get("stale_payments", 0), "stale_hours": ops.STALE_HOURS}


# ───────────────────────── helper access ─────────────────────────

async def prep_helper_set(sess, body) -> dict:
    from modules import admin_controls as ac
    uid = _snowflake(str(body.get("user_id") or ""))
    raw = body.get("sections")
    if uid is None or not isinstance(raw, list):
        return {"_error": (422, "Give a full user id and a list of sections.")}
    chosen = {s for s in raw if isinstance(s, str)}
    if not chosen or not chosen <= set(ac.GRANTABLE):
        return {"_error": (422, "Pick at least one of: " + ", ".join(sorted(ac.GRANTABLE)) + ".")}
    if uid in _owner_ids():
        return {"_error": (422, "That person is an owner already. Owners have everything.")}
    async def fn():
        stored = await ac.set_helper(uid, chosen, _actor(sess))
        return {"user_id": uid, "sections": sorted(stored), "message": "Saved. The bot picks it up within about 20 seconds."}
    return {"target": str(uid), "detail": {"sections": sorted(chosen)}, "fn": fn}


async def prep_helper_remove(sess, body) -> dict:
    from modules import admin_controls as ac
    uid = _snowflake(str(body.get("user_id") or ""))
    if uid is None:
        return {"_error": (422, "Give a full user id.")}
    async def fn():
        return {"removed": bool(await ac.remove_helper(uid)), "message": "Removed. The bot picks it up within about 20 seconds."}
    return {"target": str(uid), "fn": fn, "fresh": True, "confirm": "REMOVE"}


# ───────────────────────── clones ─────────────────────────

def _token(body):
    t = str(body.get("token") or "").strip()
    return t if TOKEN_RE.match(t) else None


async def prep_clone_register(sess, body) -> dict:
    tok = _token(body)
    if tok is None:
        return {"_error": (422, "That doesn't look like a Discord bot token.")}
    owner = _actor(sess)
    async def fn():
        from modules import admin_clones as acl
        r = await acl.register_for_owner(tok, owner)
        if not r["ok"]:
            _refuse(422, r["error"])
        return {k: r[k] for k in ("clone_id", "bot_username", "invite_url", "note")}
    # target and detail carry no token (and no bot name: that is only known after Discord validates it)
    return {"target": f"owner:{owner}", "fn": fn, "fresh": True, "confirm": "REGISTER"}


async def prep_clone_relink(sess, body) -> dict:
    cid, tok = _int(body.get("clone")), _token(body)
    if cid is None:
        return {"_error": (422, "Give a clone number.")}
    if tok is None:
        return {"_error": (422, "That doesn't look like a Discord bot token.")}
    owner = _actor(sess)
    async def fn():
        from modules import admin_clones as acl
        r = await acl.relink(cid, owner, tok)
        if not r["ok"]:
            _refuse(422, r["error"])
        return {k: r[k] for k in ("clone_id", "bot_username", "invite_url", "note")}
    return {"target": f"clone:{cid}", "fn": fn, "fresh": True, "confirm": "RELINK"}


async def prep_clone_stop(sess, body) -> dict:
    cid = _int(body.get("clone"))
    if cid is None:
        return {"_error": (422, "Give a clone number.")}
    async def fn():
        from modules import admin_clones as acl
        problem = await acl.stop(cid)
        if problem:
            _refuse(404 if problem.startswith("No clone") else 409, problem)
        return {"clone_id": cid, "message": acl.STOP_NOTE}
    return {"target": f"clone:{cid}", "fn": fn, "fresh": True, "confirm": "STOP"}


# ───────────────────────── named database cleanup ─────────────────────────

async def prep_db_cleanup_stale(sess, body) -> dict:
    async def fn():
        from modules import admin_ops as ops
        n = await ops.run_stale_payment_cleanup()
        return {"expired": n, "older_than_hours": ops.STALE_HOURS,
                "message": f"Marked {n} stale checkout(s) as expired. Rows are kept."}
    return {"target": "stale_payments", "fn": fn, "fresh": True, "confirm": "CLEANUP"}


WRITES = {
    "owner_helper_set": ("access", 10, prep_helper_set),
    "owner_helper_remove": ("access", 10, prep_helper_remove),
    "owner_clone_register": ("servers", 3, prep_clone_register),
    "owner_clone_relink": ("servers", 3, prep_clone_relink),
    "owner_clone_stop": ("servers", 5, prep_clone_stop),
    "owner_db_cleanup_stale": ("database", 2, prep_db_cleanup_stale),
}

ROUTES = {
    "owner_helpers": ("access", helpers),
    "owner_clones": ("servers", clones),
    "owner_database": ("database", database),
}
