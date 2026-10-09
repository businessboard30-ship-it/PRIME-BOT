# path: api/dash_owner_safety.py
"""Owner area, Phase 4: Abuse watchlist, Report queue, Status editor, Honeypot overview, Scam Shield.

Same contract as api/dash_owner.py (ROUTES = GET, WRITES = (section, per-minute limit, prepare)). All logic
is the SAME module code the Discord Owner panel calls: modules/admin_safety.py and modules/scam_shield.py.

Cache note (shown on the pages): the web service is a different process from the bot worker, so it cannot
clear the worker's in-memory caches. Scam Shield rules reach the bot within scam_shield.CACHE_SECONDS (5 min)
and custom statuses within admin_safety.STATUS_CACHE_TTL (60 s). Discord's own panel is instant because it
runs inside the worker.

NOT built: adding a scam IMAGE rule. Discord does it from a message link by downloading the attachment
through the live bot and hashing it; that needs the owner_jobs queue.
"""
import logging

logger = logging.getLogger(__name__)

REPORT_LIMIT = 25
RULE_MIN_LEN = 4                      # same floor as Discord: a tiny word would delete normal messages


def _safety():
    from modules import admin_safety
    return admin_safety


def _ss():
    from modules import scam_shield
    return scam_shield


def _clip(v, n):
    return None if v is None else "".join(ch for ch in str(v) if ch.isprintable() or ch in "\n ")[:n]


def _int(raw, lo=1, hi=2 ** 62):
    s = str(raw if raw is not None else "").strip()
    return int(s) if s.isdigit() and lo <= int(s) <= hi else None


def _actor(sess) -> int:
    return int(sess["user"]["id"])


# ── reads ────────────────────────────────────────────────────────────────

async def watchlist(q) -> dict:
    s = _safety()
    kind = (q("kind") or "user").strip()
    if kind not in ("user", "guild"):
        return {"_error": (422, "Kind must be user or guild.")}
    days = _int(q("days") or s.WATCH_WINDOWS[0], 1, 365)
    if days not in s.WATCH_WINDOWS:
        days = s.WATCH_WINDOWS[0]
    return {"kind": kind, "days": days, "windows": list(s.WATCH_WINDOWS), "rows": await s.watchlist(kind, days, 25),
            "note": "Counts the auto-mod actions already logged. Read-only."}


async def reports(q) -> dict:
    s = _safety()
    rows = await s.list_new_reports(REPORT_LIMIT)
    for r in rows:
        r["reason"] = _clip(r.get("reason"), 500)      # written by strangers; shown with textContent only
    return {"counts": await s.report_counts(), "rows": rows, "limit": REPORT_LIMIT}


async def msg_reports(q) -> dict:
    """Member-messaging reports with the last messages at the time of the report. Written by strangers: textContent only."""
    from database import db
    from modules import member_msg
    rows, counts = await db.msg_reports_new(REPORT_LIMIT)
    out = []
    for r in rows:
        at = r.get("created_at")
        out.append({"id": r["id"], "reporter_id": str(r["reporter_id"]), "reported_id": str(r["reported_id"]),
                    "created_at": at.isoformat() if hasattr(at, "isoformat") else None,
                    "messages": member_msg.parse_snapshot(r.get("snapshot"))})
    return {"counts": counts, "rows": out, "limit": REPORT_LIMIT,
            "actions": [{"id": "reviewed", "label": "Reviewed"}, {"id": "dismiss", "label": "Dismiss"},
                        {"id": "remove_friendship", "label": "Remove friendship"}, {"id": "ban_sender", "label": "Block sender from messaging"}]}


async def status(q) -> dict:
    s = _safety()
    return {"entries": [{**e, "text": _clip(e.get("text"), s.STATUS_TEXT_MAX)} for e in await s.list_status_entries()],
            "presence": await s.get_presence(), "kinds": s.STATUS_KINDS, "presences": s.PRESENCES,
            "max_entries": s.STATUS_MAX_ENTRIES, "max_text": s.STATUS_TEXT_MAX,
            "note": "Changes reach the bot within about a minute. {servers} and {members} are filled in live."}


async def honeypot(q) -> dict:
    totals, rows = await _safety().honeypot_overview(25)
    return {"totals": totals, "rows": rows, "note": "Only settings and counters exist; individual catches are not recorded."}


async def scamshield(q) -> dict:
    ss = _ss()
    await ss.load(force=True)            # web process's own copy; only used for the on/off flag
    rules = await ss.list_rules()
    hits = await ss.recent_hits(15)
    total = await ss.hit_total()
    counts = {k: sum(1 for r in rules if r["kind"] == k) for k in ss.KINDS}
    return {"enabled": ss.is_enabled(), "rules": [{**r, "pattern": r["pattern"] if r["kind"] != "image" else r["pattern"][:8],
                                                 "note": _clip(r.get("note"), 200)} for r in rules],
            "counts": counts, "max_rules": ss.MAX_RULES, "hits": [{k: v for k, v in h.items() if k != "_total"} for h in hits],
            "hit_total": total, "cache_seconds": ss.CACHE_SECONDS,
            "note": f"Rule changes made here reach the bot within about {ss.CACHE_SECONDS // 60} minutes."}


# ── writes ───────────────────────────────────────────────────────────────

async def prep_report_resolve(sess, body) -> dict:
    s = _safety()
    rid, to = _int(body.get("report_id")), str(body.get("status") or "")
    if rid is None or to not in (s.REPORT_REVIEWED, s.REPORT_DISMISSED):
        return {"_error": (422, "Pick a report and reviewed or dismissed.")}
    async def fn():
        ok = bool(await s.resolve_report(rid, to, _actor(sess)))
        return {"changed": ok, "message": "Done." if ok else "Already handled by someone else."}
    return {"target": str(rid), "detail": {"status": to}, "fn": fn}


MSG_REPORT_ACTIONS = ("reviewed", "dismiss", "remove_friendship", "ban_sender")


async def prep_msg_report_resolve(sess, body) -> dict:
    """reviewed | dismiss close the report. remove_friendship deletes the friendship and the conversation.
    ban_sender stops the reported person from using member messaging at all (reversible below); both then close it."""
    from database import db
    rid, action = _int(body.get("report_id")), str(body.get("action") or "")
    if rid is None or action not in MSG_REPORT_ACTIONS:
        return {"_error": (422, "Pick a report and an action.")}
    async def fn():
        rep = await db.msg_report_get(rid)
        if not rep or rep.get("status") != "new":
            return {"changed": False, "message": "Already handled by someone else."}
        if action in ("remove_friendship", "ban_sender"):
            await db.msg_friend_delete(rep["reporter_id"], rep["reported_id"])
            await db.msg_thread_delete(rep["reporter_id"], rep["reported_id"])
        if action == "ban_sender":
            await db.msg_ban_set(rep["reported_id"], True)
        ok = await db.msg_report_resolve(rid, "dismissed" if action == "dismiss" else "reviewed", str(_actor(sess)))
        return {"changed": bool(ok), "message": "Done." if ok else "Already handled by someone else."}
    return {"target": str(rid), "detail": {"action": action}, "fn": fn}


async def prep_msg_unban(sess, body) -> dict:
    from database import db
    from modules import member_msg
    uid = member_msg.snowflake(body.get("user_id"))
    if uid is None:
        return {"_error": (422, "That isn't a Discord id.")}
    async def fn():
        await db.msg_ban_set(uid, False)
        return {"message": "They can use member messaging again."}
    return {"target": uid, "fn": fn}


async def prep_status_add(sess, body) -> dict:
    s = _safety()
    kind, text = str(body.get("kind") or ""), s.clean_status_text(body.get("text"))
    if kind not in s.STATUS_KINDS:
        return {"_error": (422, "Unknown status type.")}
    if not text:
        return {"_error": (422, "Type the status text.")}
    async def fn():
        ok = await s.add_status_entry(kind, text, _actor(sess))
        return {"added": bool(ok), "message": "Added." if ok else f"The list is full ({s.STATUS_MAX_ENTRIES}). Remove one first."}
    return {"target": kind, "detail": {"text": text[:80]}, "fn": fn}


async def prep_status_remove(sess, body) -> dict:
    s = _safety()
    eid = _int(body.get("id"))
    if eid is None:
        return {"_error": (422, "Bad status id.")}
    async def fn():
        ok = bool(await s.remove_status_entry(eid))
        return {"removed": ok, "message": "Removed." if ok else "That status is already gone."}
    return {"target": str(eid), "fn": fn}


async def prep_presence_set(sess, body) -> dict:
    s = _safety()
    value = str(body.get("presence") or "")
    if value not in s.PRESENCES:
        return {"_error": (422, "Unknown presence.")}
    async def fn():
        await s.set_presence(value, _actor(sess))
        return {"presence": value, "message": "Saved."}
    return {"target": value, "fn": fn}


async def prep_status_reset(sess, body) -> dict:
    s = _safety()
    async def fn():
        await s.reset_status(_actor(sess))
        return {"message": "Back to the built-in rotation."}
    # Deletes every custom entry at once and there is no undo.
    return {"target": "all", "fn": fn, "fresh": True, "confirm": "RESET"}


async def prep_scam_toggle(sess, body) -> dict:
    on = body.get("enabled")
    if not isinstance(on, bool):
        return {"_error": (422, "Say on or off.")}
    async def fn():
        await _ss().set_enabled(on)
        return {"enabled": on, "message": "Saved. The bot picks it up within a few minutes."}
    plan = {"target": "on" if on else "off", "fn": fn}
    if not on:                             # switching protection OFF for every server is the risky direction
        plan.update(fresh=True, confirm="DISABLE")
    return plan


async def prep_scam_add(sess, body) -> dict:
    ss = _ss()
    kind, pattern = ss.classify(str(body.get("text") or "")[:100])
    if kind not in ("word", "domain") or len(pattern) < RULE_MIN_LEN:
        return {"_error": (422, f"Use at least {RULE_MIN_LEN} characters. A tiny word would delete normal messages.")}
    async def fn():
        rid = await ss.add_rule(kind, pattern, _actor(sess), "added from web dashboard")
        if rid is None:
            return {"added": False, "message": "Nothing added: it already exists, or the list is full."}
        return {"added": True, "id": rid, "kind": kind, "message": f"Added {kind} rule #{rid}. Live within a few minutes."}
    return {"target": f"{kind}:{pattern[:60]}", "detail": {"kind": kind}, "fn": fn}


async def prep_scam_remove(sess, body) -> dict:
    rid = _int(body.get("id"))
    if rid is None:
        return {"_error": (422, "Bad rule number.")}
    async def fn():
        ok = bool(await _ss().remove_rule(rid))
        return {"removed": ok, "message": f"Removed rule #{rid}." if ok else f"No rule #{rid}."}
    return {"target": str(rid), "fn": fn}


WRITES = {
    "owner_report_resolve": ("reports", 30, prep_report_resolve),
    "owner_msg_report_resolve": ("msgreports", 30, prep_msg_report_resolve),
    "owner_msg_unban": ("msgreports", 10, prep_msg_unban),
    "owner_status_add": ("status", 10, prep_status_add),
    "owner_status_remove": ("status", 20, prep_status_remove),
    "owner_presence_set": ("status", 10, prep_presence_set),
    "owner_status_reset": ("status", 3, prep_status_reset),
    "owner_scam_toggle": ("scamshield", 6, prep_scam_toggle),
    "owner_scam_add": ("scamshield", 15, prep_scam_add),
    "owner_scam_remove": ("scamshield", 15, prep_scam_remove),
}

ROUTES = {
    "owner_watchlist": ("watchlist", watchlist),
    "owner_reports": ("reports", reports),
    "owner_msg_reports": ("msgreports", msg_reports),
    "owner_status": ("status", status),
    "owner_honeypot": ("honeypot", honeypot),
    "owner_scamshield": ("scamshield", scamshield),
}
