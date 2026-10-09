# path: api/dash_msg.py
"""Member Drop Box and friends (Part D, #/me/messages).

Rules enforced HERE, on the server, on every request (the browser decides nothing):
  * the session user's id is the only "me"; the other person is `other_id`, validated as a snowflake
  * both people must have used the web (dash_web_users) and the other must allow requests
  * they must share a server a bot is in (the main bot OR an active clone), confirmed with Discord as THAT bot (its own token,
    its own server list and caches), fail closed
  * a friend request must be accepted before anything can be sent; block ends the friendship
  * no response ever says whether a given Discord user has a web account: a friend request to anyone who
    cannot receive it gets the SAME reply as one that was delivered
  * text only, Scam Shield rules applied, per-minute / per-day / unread limits, owner kill switch `messaging`,
    messages deleted after 30 days

ROUTES[action] = handler(uid, q, db); WRITES[action] = handler(uid, body, db). Errors are {"_status", "message", "code"?}.
"""
import asyncio
import contextlib
import logging
from urllib.parse import quote

from modules import member_msg as M

logger = logging.getLogger(__name__)

GENERIC_SENT = "If that person can receive requests, yours has been sent."
OFF_MESSAGE = "Messaging is switched off right now."
BANNED_MESSAGE = "Messaging is turned off for your account."
PAUSED_MESSAGE = "Messaging with this person is paused right now."


def _dash():
    from api import dash                      # late import: dash imports this module's routes
    return dash


def _err(status, message, code=None):
    out = {"_status": status, "message": message}
    if code:
        out["code"] = code
    return out


# ───────────────────────── guards ─────────────────────────

async def _switched_off() -> bool:
    from modules import admin_controls
    return M.SWITCH in await admin_controls.current_switches()


async def _guard(uid, db, writes=True):
    """None when this person may use messaging, else the error. `writes` also honours the owner kill switch."""
    from modules import admin_controls
    if (await db.msg_prefs_get(uid)).get("msg_banned") or await admin_controls.user_blocked(uid):
        return _err(403, BANNED_MESSAGE, "banned")
    if writes and await _switched_off():
        return _err(503, OFF_MESSAGE, "messaging_off")
    return None


def _other(src, uid, key="other_id"):
    other = M.snowflake(src(key) if callable(src) else (src or {}).get(key))
    if other is None:
        return None, _err(422, "That person isn't valid.")
    if other == str(uid):
        return None, _err(422, "That's you.")
    return other, None


# ───────────────────────── Discord lookups (cached, never from the client) ─────────────────────────

def _clone_of(row):
    """The clone id of a candidate-server row, or None for the main bot."""
    c = (row or {}).get("clone_id")
    return int(c) if c not in (None, "", 0) else None


@contextlib.asynccontextmanager
async def _as_bot(clone_id):
    """Run every Discord call inside the block as ONE bot (None = main bot, else that ACTIVE clone with its own token).
    Yields False, and sets nothing, when the clone is inactive or its token is missing: callers treat that as 'not shared'.
    The previous bot context is always restored on exit, so a later call can never run as the wrong bot."""
    d = _dash()
    if clone_id is None:
        marker = d._BOT.set((None, None))
    else:
        row = await d._clone_row(int(clone_id))                 # None unless the clone is active
        token = d._clone_token(row) if row else ""
        if not token:
            yield False
            return
        marker = d._BOT.set((int(clone_id), token))
    try:
        yield True
    finally:
        d._BOT.reset(marker)


def _by_bot(rows):
    """Candidate servers grouped per bot, in the order of the rows: [(clone_id|None, [row, ...]), ...]."""
    groups = {}
    for r in rows:
        groups.setdefault(_clone_of(r), []).append(r)
    return list(groups.items())


async def _present_rows(rows):
    """[(row, present)] for servers the owning bot is really in. A clone that is inactive, tokenless or erroring
    contributes nothing (fail closed); the others still count. Cached per bot by dash._cached/_store."""
    d = _dash()
    out, failed = [], False
    for cid, group in _by_bot(rows):
        try:
            async with _as_bot(cid) as ok:
                if not ok:
                    continue
                present = await d._bot_guild_ids()
        except Exception:
            failed = True
            logger.warning("messaging: server list unavailable for one bot", exc_info=True)
            continue
        out.extend(g for g in group if g["guild_id"] in present)
    return out, failed


async def _shared(db, uid, other) -> bool:
    """True when both people are members of a server a bot is in (main bot or an active clone), confirmed with Discord AS
    THAT BOT. Fails CLOSED: an inactive clone, a missing token, a Discord error or a 404 for either person means 'no'."""
    d = _dash()
    lo, hi = M.pair(uid, other)
    key = ("msg_shared", lo, hi)
    hit = d._cached(key, 60)
    if hit is not None:
        return hit
    errored = False
    try:
        rows, failed = await _present_rows(await db.msg_guild_ids(uid, 25))
        errored = failed
        found = False
        for cid, group in _by_bot(rows[:10]):                   # the existing cap: 10 member checks per pair, across all bots
            try:
                async with _as_bot(cid) as ok:
                    if not ok:
                        continue
                    for g in group:
                        for who in (other, uid):
                            try:
                                await d._bot_get(f"/guilds/{g['guild_id']}/members/{who}")
                            except d.DiscordError as e:
                                if e.status == 404:
                                    break
                                raise
                        else:
                            found = True
                            break
            except Exception:
                errored = True
                logger.warning("messaging: shared-server check failed for one bot; treating as not shared", exc_info=True)
            if found:
                return d._store(key, 60, True)                  # stored OUTSIDE the clone context, so the lookup above finds it
        if errored:
            return False                                       # do not cache a result that was affected by an error
        return d._store(key, 30, False)
    except Exception:
        logger.warning("messaging: shared-server check failed; treating as not shared", exc_info=True)
        return False


def _avatar(user):
    uid, h = str(user.get("id") or ""), user.get("avatar")
    if uid and h:
        return f"https://cdn.discordapp.com/avatars/{uid}/{h}.png?size=64"
    return f"https://cdn.discordapp.com/embed/avatars/{(int(uid) >> 22) % 6 if uid.isdigit() else 0}.png"


async def _name(uid):
    d = _dash()
    key = ("msg_name", uid)
    hit = d._cached(key, 600)
    if hit is not None:
        return hit
    try:
        u = await d._bot_get(f"/users/{uid}")
        val = {"name": M.clip_name(u.get("global_name") or u.get("username"), "Member " + uid[-4:]), "avatar": _avatar(u)}
        return d._store(key, 600, val)
    except Exception:
        return d._store(key, 30, {"name": "Member " + uid[-4:], "avatar": _avatar({"id": uid})})


async def _names(ids):
    ids = list(dict.fromkeys(ids))[:60]
    got = await asyncio.gather(*[_name(i) for i in ids])
    return dict(zip(ids, got))


def _person(uid, names, **extra):
    n = names.get(uid) or {"name": "Member " + uid[-4:], "avatar": ""}
    return {"user_id": uid, "name": n["name"], "avatar": n["avatar"], **extra}


# ───────────────────────── reads ─────────────────────────

async def friends_list(uid, q, db):
    prefs = await db.msg_prefs_get(uid)
    rows = await db.msg_friends_of(uid)
    unread = await db.msg_unread_by_sender(uid)
    ids = [r["other"] for r in rows if M.relation(r, uid) != "none"] + list(unread)
    names = await _names(ids)
    friends, incoming, outgoing, blocked = [], [], [], []
    for r in rows:
        rel, other = M.relation(r, uid), r["other"]
        if rel == "friends":
            friends.append(_person(other, names, unread=unread.get(other, 0)))
        elif rel == "pending_in":
            incoming.append(_person(other, names))
        elif rel == "pending_out":
            outgoing.append(_person(other, names))
        elif rel == "blocked_by_me":
            blocked.append(_person(other, names))
    servers = []
    try:
        shown, _failed = await _present_rows(await db.msg_guild_ids(uid, 25))
        for g in shown:
            row = {"guild_id": g["guild_id"], "name": g["name"]}
            if _clone_of(g) is not None:                       # main-bot rows keep their old shape
                row["clone"] = _clone_of(g)
                row["bot"] = M.clip_name(g.get("bot_username"), "Custom bot")
            servers.append(row)
    except Exception:
        logger.warning("messaging: server list unavailable", exc_info=True)
    friends.sort(key=lambda p: (-p["unread"], p["name"].lower()))
    return {"friends": friends, "incoming": incoming, "outgoing": outgoing, "blocked": blocked, "servers": servers,
            "unread_total": sum(unread.get(f["user_id"], 0) for f in friends),
            "settings": {"allow_requests": bool(prefs.get("allow_requests", True)), "dm_notify": bool(prefs.get("dm_notify", False))},
            "banned": bool(prefs.get("msg_banned")), "messaging_off": await _switched_off(),
            "limits": {"max_chars": M.MAX_BODY, "requests_per_day": M.REQUESTS_PER_DAY, "retention_days": M.RETENTION_DAYS}}


async def friends_search(uid, q, db):
    """Find people in ONE of your servers. Only people who have used the web and allow requests can appear."""
    d = _dash()
    bad = await _guard(uid, db, writes=False)
    if bad:
        return bad
    gid = M.snowflake(q("guild_id"))
    term = (q("query") or "").strip()
    if gid is None or not M.SEARCH_MIN <= len(term) <= 32:
        return _err(422, f"Pick a server and type at least {M.SEARCH_MIN} letters.")
    raw_clone = q("clone")                                      # NOT clone_id: dash.py reserves that as its scope selector
    clone = None
    if raw_clone not in (None, "", "0", "main"):
        if not str(raw_clone).isdigit() or not 0 < int(str(raw_clone)) < 2 ** 31:
            return _err(422, "Pick one of your servers.")
        clone = int(str(raw_clone))
    denied = _err(403, "Pick one of your servers.")
    try:
        # The server must be one where YOU have XP on THAT bot; then every Discord call below runs as that bot.
        mine = {g["guild_id"] for g in await db.msg_guild_ids(uid, 25) if _clone_of(g) == clone}
        if gid not in mine:
            return denied
        async with _as_bot(clone) as ok:
            if not ok:
                return denied                                   # inactive clone or no token: fail closed
            if gid not in await d._bot_guild_ids():
                return denied
            try:
                await d._bot_get(f"/guilds/{gid}/members/{uid}")
            except d.DiscordError as e:
                if e.status == 404:
                    return denied
                raise
            found = await d._bot_get(f"/guilds/{gid}/members/search?query={quote(term, safe='')}&limit=50")
    except d.DiscordError:
        return _err(502, "Couldn't reach Discord. Try again in a moment.")
    users = {}
    for m in found if isinstance(found, list) else []:
        u = (m or {}).get("user") or {}
        i = M.snowflake(u.get("id"))
        if i and i != str(uid) and not u.get("bot"):
            users[i] = u
    eligible = await db.msg_eligible_ids(list(users))
    rows = {r["other"]: r for r in await db.msg_friends_of(uid)}
    results = []
    for i in users:
        if i not in eligible or (rows.get(i) or {}).get("status") == "blocked":
            continue
        results.append({"user_id": i, "name": M.clip_name(users[i].get("global_name") or users[i].get("username"), "Member " + i[-4:]),
                        "avatar": _avatar(users[i]), "relation": M.relation(rows.get(i), uid)})
        if len(results) >= M.SEARCH_LIMIT:
            break
    return {"results": results}


async def messages_thread(uid, q, db):
    other, err = _other(q, uid)
    if err:
        return err
    bad = await _guard(uid, db, writes=False)
    if bad:
        return bad
    if M.relation(await db.msg_friend_get(uid, other), uid) != "friends":
        return _err(403, "That conversation isn't available.")
    msgs = await db.msg_thread(uid, other, M.THREAD_LIMIT)
    off = await _switched_off()
    can_send = (not off) and await _shared(db, uid, other)
    person = _person(other, await _names([other]))
    return {"with": person, "can_send": can_send, "messaging_off": off,
            "paused": (not off) and not can_send,
            "messages": [{"id": str(m["id"]), "mine": m["sender_id"] == str(uid), "body": m["body"],
                          "at": m["created_at"].isoformat() if m.get("created_at") else None,
                          "read": bool(m.get("read_at"))} for m in msgs]}


# ───────────────────────── friend writes ─────────────────────────

async def _target_can_receive(uid, other, db) -> bool:
    """Every reason a request cannot be delivered, folded into one boolean so the caller can answer identically."""
    from modules import admin_controls
    if other not in await db.msg_eligible_ids([other]):         # never used the web, requests off, or banned
        return False
    if await admin_controls.user_blocked(other):
        return False
    if (await db.msg_request_counts(other))["pending_in"] >= M.MAX_PENDING_IN:
        return False
    return await _shared(db, uid, other)


async def friend_request(uid, body, db):
    bad = await _guard(uid, db)
    if bad:
        return bad
    other, err = _other(body, uid)
    if err:
        return err
    row = await db.msg_friend_get(uid, other)
    rel = M.relation(row, uid)
    if rel == "friends":
        return _err(409, "You're already friends.")
    if rel == "blocked_by_me":
        return _err(409, "You blocked this person. Unblock them first.")
    if row and row.get("status") == "blocked":                   # THEY blocked me: look exactly like a delivered request
        return {"sent": True, "message": GENERIC_SENT}
    if rel == "pending_out":
        return {"sent": True, "message": GENERIC_SENT}
    counts = await db.msg_request_counts(uid)
    if rel == "pending_in":                                      # they already asked me: asking back means yes
        if counts["friends"] >= M.MAX_FRIENDS:
            return _err(429, f"You can have up to {M.MAX_FRIENDS} friends.")
        if await db.msg_friend_accept(uid, other, uid):
            return {"accepted": True, "message": "You're now friends."}
        return {"sent": True, "message": GENERIC_SENT}
    reason = M.request_block_reason(counts)
    if reason:
        return _err(429, reason, "request_limit")
    if await _target_can_receive(uid, other, db):
        await db.msg_friend_request(uid, other, uid)
    return {"sent": True, "message": GENERIC_SENT}


async def friend_respond(uid, body, db):
    other, err = _other(body, uid)
    if err:
        return err
    accept = (body or {}).get("accept")
    if not isinstance(accept, bool):
        return _err(422, "Say accept or decline.")
    if accept:
        bad = await _guard(uid, db)
        if bad:
            return bad
        if (await db.msg_request_counts(uid))["friends"] >= M.MAX_FRIENDS:
            return _err(429, f"You can have up to {M.MAX_FRIENDS} friends.")
        if not await db.msg_friend_accept(uid, other, uid):
            return _err(404, "That request isn't there any more.")
        return {"accepted": True}
    if not await db.msg_friend_delete(uid, other, only_requested_by_not=uid):
        return _err(404, "That request isn't there any more.")
    return {"declined": True}


async def friend_remove(uid, body, db):
    """Unfriend, decline or cancel: removes the relationship and the conversation for both people."""
    other, err = _other(body, uid)
    if err:
        return err
    if M.relation(await db.msg_friend_get(uid, other), uid) in ("none", "blocked_by_me"):
        return _err(404, "There's nothing to remove.")
    await db.msg_friend_delete(uid, other)
    await db.msg_thread_delete(uid, other)
    return {"removed": True}


async def friend_block(uid, body, db):
    """Ends the friendship and the conversation and stops new requests. Always allowed (even with messaging off)."""
    other, err = _other(body, uid)
    if err:
        return err
    row = await db.msg_friend_get(uid, other)
    if not (row and row.get("status") == "blocked" and str(row.get("blocked_by")) != str(uid)):   # keep THEIR block intact
        await db.msg_friend_block(uid, other, uid)
    await db.msg_thread_delete(uid, other)
    return {"blocked": True}


async def friend_unblock(uid, body, db):
    other, err = _other(body, uid)
    if err:
        return err
    await db.msg_friend_delete(uid, other, only_blocked_by=uid)
    return {"unblocked": True}


# ───────────────────────── messages ─────────────────────────

async def message_send(uid, body, db):
    bad = await _guard(uid, db)
    if bad:
        return bad
    other, err = _other(body, uid)
    if err:
        return err
    if M.relation(await db.msg_friend_get(uid, other), uid) != "friends":
        return _err(403, "You can only message friends.")
    text, err = M.clean_body((body or {}).get("body"))
    if err:
        return _err(422, err)
    from modules import scam_shield
    await scam_shield.load()
    if scam_shield.match_text(text):
        return _err(422, "That message looks like a scam or a blocked link, so it can't be sent.", "blocked_text")
    reason = M.send_block_reason(await db.msg_send_counts(uid, other))
    if reason:
        return _err(429, reason, "send_limit")
    from modules import admin_controls
    if (await db.msg_prefs_get(other)).get("msg_banned") or await admin_controls.user_blocked(other):
        return _err(409, PAUSED_MESSAGE, "paused")
    if not await _shared(db, uid, other):                       # last, because it can cost Discord calls
        return _err(409, "Messaging is paused: you no longer share a server with this person.", "paused")
    saved = await db.msg_insert(uid, other, text)
    return {"message": {"id": str(saved["id"]), "mine": True, "body": text,
                        "at": saved["created_at"].isoformat() if saved.get("created_at") else None, "read": False}}


async def message_read(uid, body, db):
    other, err = _other(body, uid)
    if err:
        return err
    await db.msg_mark_read(uid, other)
    return {"unread_total": sum((await db.msg_unread_by_sender(uid)).values())}


async def message_thread_delete(uid, body, db):
    """Delete the whole conversation (for both people). The friendship stays."""
    other, err = _other(body, uid)
    if err:
        return err
    return {"deleted": await db.msg_thread_delete(uid, other)}


async def message_report(uid, body, db):
    """Send the last messages to the owner. Needs at least one message FROM that person; the snapshot is taken now."""
    other, err = _other(body, uid)
    if err:
        return err
    if await db.msg_reports_today(uid) >= M.REPORTS_PER_DAY:
        return _err(429, "You've sent a lot of reports today. Try again tomorrow.")
    thread = await db.msg_thread(uid, other, M.SNAPSHOT_MESSAGES)
    if not any(m["sender_id"] == other for m in thread):
        return _err(404, "There's nothing from this person to report.")
    rid = await db.msg_report_insert(uid, other, M.snapshot_json(thread, uid))
    if rid is not None:
        from modules import admin_controls
        await admin_controls.record_audit(int(uid), "member_report", None, f"report={rid} reported={other}")
    if (body or {}).get("also_block") is True:
        await friend_block(uid, {"other_id": other}, db)
    return {"reported": True, "message": "Thanks. The owner will review it." if rid is not None else "You already reported this person. The owner will review it."}


async def msg_prefs_set(uid, body, db):
    allow, notify = (body or {}).get("allow_requests"), (body or {}).get("dm_notify")
    if allow is None and notify is None:
        return _err(422, "Nothing to change.")
    if any(v is not None and not isinstance(v, bool) for v in (allow, notify)):
        return _err(422, "Choose on or off.")
    await db.msg_prefs_set(uid, allow, notify)
    prefs = await db.msg_prefs_get(uid)
    return {"settings": {"allow_requests": bool(prefs["allow_requests"]), "dm_notify": bool(prefs["dm_notify"])}}


async def unread_total(uid, db) -> int:
    """For the header badge on `me`. Never raises."""
    try:
        return sum((await db.msg_unread_by_sender(uid)).values())
    except Exception:
        logger.exception("messaging: unread count failed")
        return 0


ROUTES = {"friends_list": friends_list, "friends_search": friends_search, "messages_thread": messages_thread}
WRITES = {"friend_request": friend_request, "friend_respond": friend_respond, "friend_remove": friend_remove,
          "friend_block": friend_block, "friend_unblock": friend_unblock, "message_send": message_send,
          "message_read": message_read, "message_report": message_report, "message_thread_delete": message_thread_delete,
          "msg_prefs_set": msg_prefs_set}
