"""Part D: member Drop Box and friends. Friend request first, same-server + web-user checks, no web-account leak,
limits, block/report, Scam Shield, owner kill switch, session-only identity, isolation between members."""
import asyncio
import importlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from api import dash, dash_msg
from modules import member_msg as M
from tests.unit.test_dash_api import call, env  # noqa: F401

A, B, C, D = "111111111111111111", "222222222222222222", "333333333333333333", "444444444444444444"
G1, G2 = "900100000000000001", "900200000000000002"


class Store:
    """In-memory twin of the msg_* Database methods (same rules as the SQL; the SQL itself is checked on real Postgres)."""

    def __init__(self):
        self.web, self.prefs, self.friends, self.msgs, self.reports = set(), {}, {}, [], []
        self.guilds, self.audit, self.next_id = {}, [], 1

    def key(self, a, b):
        return M.pair(a, b)

    async def msg_prefs_get(self, uid):
        return {"allow_requests": True, "dm_notify": False, "msg_banned": False, **self.prefs.get(str(uid), {})}

    async def msg_prefs_set(self, uid, allow_requests=None, dm_notify=None):
        p = self.prefs.setdefault(str(uid), {})
        if allow_requests is not None:
            p["allow_requests"] = allow_requests
        if dm_notify is not None:
            p["dm_notify"] = dm_notify

    async def msg_ban_set(self, uid, banned):
        self.prefs.setdefault(str(uid), {})["msg_banned"] = bool(banned)

    async def msg_eligible_ids(self, ids):
        out = set()
        for i in ids:
            p = await self.msg_prefs_get(i)
            if str(i) in self.web and p["allow_requests"] and not p["msg_banned"]:
                out.add(str(i))
        return out

    async def msg_guild_ids(self, uid, limit=25):
        return [{"guild_id": g, "name": "Server " + g} for g in self.guilds.get(str(uid), [])][:limit]

    async def msg_friend_get(self, a, b):
        r = self.friends.get(self.key(a, b))
        return dict(r) if r else None

    async def msg_friend_request(self, a, b, by):
        k = self.key(a, b)
        if k in self.friends:
            return False
        self.friends[k] = {"user_a": k[0], "user_b": k[1], "requested_by": str(by), "status": "pending", "blocked_by": None}
        return True

    async def msg_friend_accept(self, a, b, acceptor):
        r = self.friends.get(self.key(a, b))
        if r and r["status"] == "pending" and r["requested_by"] != str(acceptor):
            r["status"] = "accepted"
            return True
        return False

    async def msg_friend_block(self, a, b, by):
        k = self.key(a, b)
        r = self.friends.setdefault(k, {"user_a": k[0], "user_b": k[1], "requested_by": str(by)})
        r.update(status="blocked", blocked_by=str(by))

    async def msg_friend_delete(self, a, b, only_blocked_by=None, only_requested_by_not=None):
        k = self.key(a, b)
        r = self.friends.get(k)
        if not r:
            return False
        if only_blocked_by is not None and not (r["status"] == "blocked" and r["blocked_by"] == str(only_blocked_by)):
            return False
        if only_requested_by_not is not None and not (r["status"] == "pending" and r["requested_by"] != str(only_requested_by_not)):
            return False
        del self.friends[k]
        return True

    async def msg_friends_of(self, uid, limit=300):
        out = []
        for (x, y), r in self.friends.items():
            if str(uid) in (x, y):
                out.append({**r, "other": y if x == str(uid) else x})
        return out

    async def msg_request_counts(self, uid):
        uid = str(uid)
        rows = [r for r in await self.msg_friends_of(uid)]
        return {"sent_today": sum(r["requested_by"] == uid for r in rows),
                "pending_out": sum(r["requested_by"] == uid and r["status"] == "pending" for r in rows),
                "pending_in": sum(r["requested_by"] != uid and r["status"] == "pending" for r in rows),
                "friends": sum(r["status"] == "accepted" for r in rows)}

    async def msg_insert(self, sender, recipient, body):
        m = {"id": self.next_id, "sender_id": str(sender), "recipient_id": str(recipient), "body": body,
             "created_at": datetime.now(timezone.utc), "read_at": None}
        self.next_id += 1
        self.msgs.append(m)
        return {"id": m["id"], "created_at": m["created_at"]}

    async def msg_send_counts(self, sender, recipient):
        now, s = datetime.now(timezone.utc), str(sender)
        mine = [m for m in self.msgs if m["sender_id"] == s]
        return {"minute": sum(now - m["created_at"] < timedelta(minutes=1) for m in mine),
                "day": sum(now - m["created_at"] < timedelta(days=1) for m in mine),
                "unread_to_them": sum(m["recipient_id"] == str(recipient) and m["read_at"] is None for m in mine)}

    async def msg_thread(self, a, b, limit=50):
        pair = {str(a), str(b)}
        rows = [dict(m) for m in self.msgs if {m["sender_id"], m["recipient_id"]} == pair]
        return rows[-limit:]

    async def msg_mark_read(self, reader, other):
        n = 0
        for m in self.msgs:
            if m["recipient_id"] == str(reader) and m["sender_id"] == str(other) and m["read_at"] is None:
                m["read_at"] = datetime.now(timezone.utc)
                n += 1
        return n

    async def msg_unread_by_sender(self, uid):
        out = {}
        for m in self.msgs:
            if m["recipient_id"] == str(uid) and m["read_at"] is None:
                out[m["sender_id"]] = out.get(m["sender_id"], 0) + 1
        return out

    async def msg_thread_delete(self, a, b):
        pair = {str(a), str(b)}
        keep = [m for m in self.msgs if {m["sender_id"], m["recipient_id"]} != pair]
        n, self.msgs = len(self.msgs) - len(keep), keep
        return n

    async def msg_report_insert(self, reporter, reported, snapshot):
        if any(r["reporter_id"] == str(reporter) and r["reported_id"] == str(reported) and r["status"] == "new" for r in self.reports):
            return None
        r = {"id": len(self.reports) + 1, "reporter_id": str(reporter), "reported_id": str(reported), "snapshot": snapshot,
             "status": "new", "created_at": datetime.now(timezone.utc)}
        self.reports.append(r)
        return r["id"]

    async def msg_reports_today(self, reporter):
        return sum(r["reporter_id"] == str(reporter) for r in self.reports)

    async def msg_reports_new(self, limit=25):
        new = [dict(r) for r in self.reports if r["status"] == "new"][:limit]
        counts = {}
        for r in self.reports:
            counts[r["status"]] = counts.get(r["status"], 0) + 1
        return new, counts

    async def msg_report_get(self, rid):
        return next((dict(r) for r in self.reports if r["id"] == int(rid)), None)

    async def msg_report_resolve(self, rid, status, by):
        r = next((r for r in self.reports if r["id"] == int(rid) and r["status"] == "new"), None)
        if not r:
            return False
        r["status"] = status
        return True


@pytest.fixture
def msg(env, monkeypatch):
    fake, _ = env
    st = Store()
    for n in [n for n in dir(Store) if n.startswith("msg_")]:
        monkeypatch.setattr(fake, n, getattr(st, n), raising=False)
    for name, uid in (("A", A), ("B", B), ("C", C), ("D", D)):
        fake.sessions[name] = {"kind": "dash", "user": {"id": uid, "username": "u" + name, "avatar_url": ""}, "guilds": []}
    st.web = {A, B, C, D}
    st.guilds = {A: [G1], B: [G1], C: [G1], D: [G2]}
    members = {G1: {A, B, C}, G2: {D}}
    names = {A: "Alice", B: "Bob", C: "Cara", D: "Dan"}
    state = {"st": st, "members": members, "names": names, "switches": set(), "blacklist": set(), "audit": [], "discord_down": False}

    async def bot_get(path):
        if state["discord_down"]:
            raise dash.DiscordError(500)
        if path.startswith("/users/@me/guilds"):
            return [{"id": g} for g in members]
        if path.startswith("/guilds/") and "/members/search" in path:
            gid = path.split("/")[2]
            term = path.split("query=")[1].split("&")[0].lower()
            return [{"user": {"id": u, "username": names[u], "global_name": names[u]}} for u in members.get(gid, ()) if names[u].lower().startswith(term)]
        if path.startswith("/guilds/") and "/members/" in path:
            gid, uid = path.split("/")[2], path.rsplit("/", 1)[1]
            if uid not in members.get(gid, ()):
                raise dash.DiscordError(404)
            return {"user": {"id": uid}}
        if path.startswith("/users/"):
            uid = path.rsplit("/", 1)[1]
            return {"id": uid, "username": names.get(uid, "x"), "global_name": names.get(uid)}
        raise dash.DiscordError(404)
    monkeypatch.setattr(dash, "_bot_get", bot_get)

    ac = importlib.import_module("modules.admin_controls")

    async def switches():
        return set(state["switches"])

    async def blocked(uid):
        return int(uid) in state["blacklist"]

    async def audit(admin_id, action, guild_id, details):
        state["audit"].append((admin_id, action, details))
    monkeypatch.setattr(ac, "current_switches", switches)
    monkeypatch.setattr(ac, "user_blocked", blocked)
    monkeypatch.setattr(ac, "record_audit", audit)

    from modules import scam_shield

    async def noload(force=False):
        return None
    monkeypatch.setattr(scam_shield, "load", noload)
    monkeypatch.setattr(scam_shield._c, "domains", [(1, "evil.example")])
    monkeypatch.setattr(scam_shield._c, "words", [(2, "freenitro")])
    dash._cache.clear()
    dash._owner_hits.clear()
    return state


def get(token, action, **q):
    dash._owner_hits.clear()
    return call("GET", {"action": action, **q}, token=token)


def post(token, action, **body):
    dash._owner_hits.clear()
    return call("POST", None, token=token, body={"action": action, **body})


def befriend(token_a="A", token_b="B", a=A, b=B):
    assert post(token_a, "friend_request", other_id=b)[0] == 200
    assert post(token_b, "friend_respond", other_id=a, accept=True)[0] == 200


# ───────────────────────── pure rules ─────────────────────────
def test_snowflake_validation():
    assert M.snowflake(A) == A and M.snowflake(" 123456789012345678 ") == "123456789012345678"
    for bad in ("6", "abc", "", None, "12345678901234", "1" * 21, "-1" + "1" * 17, 1.5, "0" * 18):
        assert M.snowflake(bad) is None


def test_pair_is_canonical_by_integer_value():
    assert M.pair("9", "10") == ("9", "10") and M.pair("10", "9") == ("9", "10")
    assert M.pair(A, B) == M.pair(B, A)


def test_clean_body_strips_tricks_and_enforces_limits():
    assert M.clean_body("  hi  ") == ("hi", None)
    assert M.clean_body("a\u200bb\u202ec\ufeffd")[0] == "abcd"             # zero-width and bidi controls removed
    assert M.clean_body("a\x00b\x07c")[0] == "abc"
    assert M.clean_body("a\r\n\r\n\r\n\r\nb")[0] == "a\n\nb"
    assert M.clean_body("")[1] and M.clean_body("   ")[1] and M.clean_body("\u200b\u200b")[1] and M.clean_body(None)[1] and M.clean_body(5)[1]
    assert M.clean_body("x" * M.MAX_BODY)[1] is None and M.clean_body("x" * (M.MAX_BODY + 1))[1]


def test_relation_hides_who_blocked_you():
    row = {"status": "blocked", "blocked_by": A}
    assert M.relation(row, A) == "blocked_by_me" and M.relation(row, B) == "none"
    assert M.relation({"status": "pending", "requested_by": A}, A) == "pending_out"
    assert M.relation({"status": "pending", "requested_by": A}, B) == "pending_in"
    assert M.relation({"status": "accepted"}, B) == "friends" and M.relation(None, A) == "none"


def test_snapshot_labels_sides_without_ids_and_roundtrips_safely():
    now = datetime(2026, 10, 8, tzinfo=timezone.utc)
    snap = M.snapshot_json([{"sender_id": A, "body": "hi", "created_at": now}, {"sender_id": B, "body": "yo", "created_at": now}], A)
    parsed = M.parse_snapshot(snap)
    assert [m["from"] for m in parsed] == ["reporter", "reported"] and A not in snap and B not in snap
    assert M.parse_snapshot("not json") == [] and M.parse_snapshot('{"a":1}') == []
    many = M.snapshot_json([{"sender_id": A, "body": str(i), "created_at": now} for i in range(50)], A)
    assert len(M.parse_snapshot(many)) == M.SNAPSHOT_MESSAGES


def test_kill_switch_exists_and_turns_off_no_slash_commands():
    ac = importlib.import_module("modules.admin_controls")
    label, roots = ac.FEATURES["messaging"]
    assert roots == set() and M.SWITCH == "messaging" and "messag" in label.lower()


# ───────────────────────── session / routes ─────────────────────────
ALL_GET = ("friends_list", "friends_search", "messages_thread")
ALL_POST = tuple(dash_msg.WRITES)


def test_every_route_needs_a_session(msg):
    for a in ALL_GET:
        assert call("GET", {"action": a}, token=None)[0] == 401, a
    for a in ALL_POST:
        dash._owner_hits.clear()
        assert call("POST", None, token=None, body={"action": a})[0] == 401, a


def test_routes_do_not_collide_and_take_no_session_identity_from_the_client():
    assert not (set(dash_msg.ROUTES) | set(dash_msg.WRITES)) & (set(dash._owner_routes()) | set(dash._owner_writes()))
    src = Path(dash_msg.__file__).read_text()
    for forbidden in ('"user_id"', '"uid"', '"sender_id"', '"me"'):
        assert f"body.get({forbidden}" not in src and f"q({forbidden}" not in src


# ───────────────────────── friend request flow ─────────────────────────
def test_request_accept_then_message_and_read_flow(msg):
    st, p, _ = post("A", "friend_request", other_id=B)
    assert st == 200 and p["sent"] is True
    lst = get("B", "friends_list")[1]
    assert [x["user_id"] for x in lst["incoming"]] == [A] and lst["incoming"][0]["name"] == "Alice" and lst["friends"] == []
    assert [x["user_id"] for x in get("A", "friends_list")[1]["outgoing"]] == [B]
    assert post("A", "message_send", other_id=B, body="hi")[0] == 403                       # not friends yet
    assert post("B", "friend_respond", other_id=A, accept=True)[1]["accepted"] is True
    st, p, _ = post("A", "message_send", other_id=B, body="hello Bob")
    assert st == 200 and p["message"]["mine"] is True and p["message"]["body"] == "hello Bob"
    lst = get("B", "friends_list")[1]
    assert lst["friends"][0]["unread"] == 1 and lst["unread_total"] == 1
    t = get("B", "messages_thread", other_id=A)[1]
    assert t["can_send"] is True and t["messages"][0]["mine"] is False and t["messages"][0]["body"] == "hello Bob"
    assert post("B", "message_read", other_id=A)[1]["unread_total"] == 0
    assert get("A", "messages_thread", other_id=B)[1]["messages"][0]["read"] is True


def test_request_in_both_directions_becomes_friendship(msg):
    post("A", "friend_request", other_id=B)
    st, p, _ = post("B", "friend_request", other_id=A)            # they already asked me: asking back means yes
    assert st == 200 and p["accepted"] is True
    assert get("A", "friends_list")[1]["friends"][0]["user_id"] == B


def test_decline_removes_the_request_and_cancel_works(msg):
    post("A", "friend_request", other_id=B)
    assert post("B", "friend_respond", other_id=A, accept=False)[1]["declined"] is True
    assert get("A", "friends_list")[1]["outgoing"] == []
    post("A", "friend_request", other_id=B)
    assert post("A", "friend_remove", other_id=B)[0] == 200 and get("B", "friends_list")[1]["incoming"] == []


def test_only_the_recipient_can_accept(msg):
    post("A", "friend_request", other_id=B)
    assert post("A", "friend_respond", other_id=B, accept=True)[0] == 404        # the sender cannot accept their own request
    assert post("C", "friend_respond", other_id=A, accept=True)[0] == 404        # a third person cannot either
    assert post("B", "friend_respond", other_id=A, accept="yes")[0] == 422


def test_request_reply_is_identical_whether_or_not_it_could_be_delivered(msg):
    st = msg["st"]
    ok = post("A", "friend_request", other_id=B)
    st.web.discard(C)                                        # never used the web
    no_web = post("A", "friend_request", other_id=C)
    st.web.add(D)
    no_server = post("A", "friend_request", other_id=D)       # D is in a different server
    st.prefs[B] = {"allow_requests": False}
    off = post("A", "friend_request", other_id=B)             # already requested (pending): idempotent
    for other in (no_web, no_server, off):
        assert other[0] == ok[0] == 200 and other[1] == ok[1]
    rows = {k for k in st.friends}
    assert M.pair(A, B) in rows and M.pair(A, C) not in rows and M.pair(A, D) not in rows


def test_requests_off_banned_blacklisted_and_blocked_targets_look_delivered_but_nothing_is_created(msg):
    st = msg["st"]
    base = post("A", "friend_request", other_id=B)[1]
    st.friends.clear()
    st.prefs[B] = {"allow_requests": False}
    assert post("A", "friend_request", other_id=B)[1] == base and not st.friends
    st.prefs[B] = {"msg_banned": True}
    assert post("A", "friend_request", other_id=B)[1] == base and not st.friends
    st.prefs.clear(); msg["blacklist"].add(int(B))
    assert post("A", "friend_request", other_id=B)[1] == base and not st.friends
    msg["blacklist"].clear()
    post("B", "friend_block", other_id=A)                    # B blocked A
    assert post("A", "friend_request", other_id=B)[1] == base
    assert st.friends[M.pair(A, B)]["blocked_by"] == B and st.friends[M.pair(A, B)]["status"] == "blocked"


def test_request_needs_a_shared_server_checked_with_discord(msg):
    msg["members"][G1].discard(B)                             # B left the server
    dash._cache.clear()
    post("A", "friend_request", other_id=B)
    assert not msg["st"].friends
    msg["members"][G1].add(B); dash._cache.clear()
    post("A", "friend_request", other_id=B)
    assert M.pair(A, B) in msg["st"].friends


def test_shared_server_check_fails_closed_when_discord_is_down(msg):
    msg["discord_down"] = True
    post("A", "friend_request", other_id=B)
    assert not msg["st"].friends


def test_self_and_bad_ids_are_rejected(msg):
    assert post("A", "friend_request", other_id=A)[0] == 422
    for bad in ("6", "abc", "", None):
        assert post("A", "friend_request", other_id=bad)[0] == 422


def test_already_friends_and_blocked_by_me_have_clear_errors(msg):
    befriend()
    assert post("A", "friend_request", other_id=B)[0] == 409
    post("A", "friend_block", other_id=C)
    assert post("A", "friend_request", other_id=C)[0] == 409


def test_daily_request_limit_and_pending_cap_apply_to_the_sender_only(msg):
    st = msg["st"]
    for i in range(M.REQUESTS_PER_DAY):                      # fill the sender's own count with other people
        k = M.pair(A, str(500000000000000000 + i))
        st.friends[k] = {"user_a": k[0], "user_b": k[1], "requested_by": A, "status": "accepted", "blocked_by": None}
    st2, p, _ = post("A", "friend_request", other_id=B)
    assert st2 == 429 and p["code"] == "request_limit"
    assert get("B", "friends_list")[0] == 200 and not any(M.pair(A, B) == k for k in st.friends)


def test_pending_cap_for_one_recipient_drops_extra_requests_silently(msg):
    st = msg["st"]
    for i in range(M.MAX_PENDING_IN):
        o = str(600000000000000000 + i)
        k = M.pair(B, o)
        st.friends[k] = {"user_a": k[0], "user_b": k[1], "requested_by": o, "status": "pending", "blocked_by": None}
    r = post("A", "friend_request", other_id=B)
    assert r[0] == 200 and M.pair(A, B) not in st.friends


# ───────────────────────── search ─────────────────────────
def test_search_finds_only_web_users_who_allow_requests_in_my_servers(msg):
    st = msg["st"]
    r = get("A", "friends_search", guild_id=G1, query="Bo")[1]["results"]
    assert [x["user_id"] for x in r] == [B] and r[0]["relation"] == "none"
    st.web.discard(B)
    assert get("A", "friends_search", guild_id=G1, query="Bo")[1]["results"] == []
    st.web.add(B); st.prefs[B] = {"allow_requests": False}
    assert get("A", "friends_search", guild_id=G1, query="Bo")[1]["results"] == []
    st.prefs.clear()
    assert get("A", "friends_search", guild_id=G1, query="Al")[1]["results"] == []   # never yourself


def test_search_response_has_no_web_account_flag_and_shows_relation_only_for_eligible(msg):
    r = get("A", "friends_search", guild_id=G1, query="Bo")[1]
    assert set(r["results"][0]) == {"user_id", "name", "avatar", "relation"}
    post("A", "friend_request", other_id=B)
    assert get("A", "friends_search", guild_id=G1, query="Bo")[1]["results"][0]["relation"] == "pending_out"


def test_search_excludes_blocked_people_both_ways_and_bots(msg):
    post("B", "friend_block", other_id=A)
    assert get("A", "friends_search", guild_id=G1, query="Bo")[1]["results"] == []
    post("A", "friend_block", other_id=C)
    assert get("A", "friends_search", guild_id=G1, query="Ca")[1]["results"] == []


def test_search_needs_my_server_membership_and_a_real_query(msg):
    assert get("A", "friends_search", guild_id=G2, query="Da")[0] == 403           # not one of A's servers
    assert get("D", "friends_search", guild_id=G1, query="Bo")[0] == 403           # D is not in G1
    msg["members"][G1].discard(A); dash._cache.clear()
    assert get("A", "friends_search", guild_id=G1, query="Bo")[0] == 403           # left the server
    assert get("B", "friends_search", guild_id=G1, query="B")[0] == 422
    assert get("B", "friends_search", guild_id="x", query="Bob")[0] == 422
    assert get("B", "friends_search", guild_id=G1, query="x" * 33)[0] == 422


def test_search_is_rate_limited(msg):
    dash._owner_hits.clear()
    codes = [call("GET", {"action": "friends_search", "guild_id": G1, "query": "Bo"}, token="A")[0] for _ in range(16)]
    assert codes[:15] == [200] * 15 and codes[15] == 429


# ───────────────────────── messaging rules ─────────────────────────
def test_cannot_message_without_an_accepted_friendship(msg):
    assert post("A", "message_send", other_id=B, body="hi")[0] == 403                    # no relationship
    post("A", "friend_request", other_id=B)
    assert post("A", "message_send", other_id=B, body="hi")[0] == 403                    # pending
    assert post("B", "message_send", other_id=A, body="hi")[0] == 403                    # pending, from the other side
    post("B", "friend_block", other_id=A)
    assert post("A", "message_send", other_id=B, body="hi")[0] == 403 and msg["st"].msgs == []


def test_message_validation_and_scam_shield(msg):
    befriend()
    assert post("A", "message_send", other_id=B, body="")[0] == 422
    assert post("A", "message_send", other_id=B, body="x" * 1001)[0] == 422
    for scam in ("get it at https://evil.example/claim", "FREE NITRO here", "free-nitro".replace("-", ""), "see sub.evil.example now"):
        st, p, _ = post("A", "message_send", other_id=B, body=scam)
        assert st == 422 and p["code"] == "blocked_text", scam
    assert msg["st"].msgs == []
    assert post("A", "message_send", other_id=B, body="plain link https://example.org is fine as text")[0] == 200


def test_message_text_is_stored_cleaned(msg):
    befriend()
    post("A", "message_send", other_id=B, body="hi\u200b there\x00")
    assert msg["st"].msgs[0]["body"] == "hi there"


def test_message_rate_limits(msg):
    befriend()
    for i in range(M.MSGS_PER_MINUTE):
        assert post("A", "message_send", other_id=B, body=f"m{i}")[0] == 200
        post("B", "message_read", other_id=A)               # keep unread low so only the minute limit can trip
    st, p, _ = post("A", "message_send", other_id=B, body="one more")
    assert st == 429 and p["code"] == "send_limit"


def test_unread_cap_stops_flooding_one_person(msg):
    befriend()
    for m in range(M.MAX_UNREAD_TO_ONE):
        msg["st"].msgs.append({"id": 1000 + m, "sender_id": A, "recipient_id": B, "body": "x",
                               "created_at": datetime.now(timezone.utc) - timedelta(hours=2), "read_at": None})
    st, p, _ = post("A", "message_send", other_id=B, body="still there?")
    assert st == 429 and "read" in p["message"]
    post("B", "message_read", other_id=A)
    assert post("A", "message_send", other_id=B, body="now ok")[0] == 200


def test_daily_message_limit(msg):
    befriend()
    for m in range(M.MSGS_PER_DAY):
        msg["st"].msgs.append({"id": 2000 + m, "sender_id": A, "recipient_id": C, "body": "x",
                               "created_at": datetime.now(timezone.utc) - timedelta(hours=3), "read_at": datetime.now(timezone.utc)})
    assert post("A", "message_send", other_id=B, body="hi")[0] == 429


def test_messaging_pauses_when_they_no_longer_share_a_server(msg):
    befriend()
    post("A", "message_send", other_id=B, body="before")
    msg["members"][G1].discard(B); dash._cache.clear()
    st, p, _ = post("A", "message_send", other_id=B, body="after")
    assert st == 409 and p["code"] == "paused"
    t = get("A", "messages_thread", other_id=B)[1]
    assert t["can_send"] is False and t["paused"] is True and len(t["messages"]) == 1    # history stays readable
    msg["members"][G1].add(B); dash._cache.clear()
    assert post("A", "message_send", other_id=B, body="back")[0] == 200


def test_messaging_fails_closed_when_discord_is_down(msg):
    befriend()
    dash._cache.clear(); msg["discord_down"] = True
    assert post("A", "message_send", other_id=B, body="hi")[0] == 409
    assert get("A", "messages_thread", other_id=B)[1]["can_send"] is False


def test_banned_or_blacklisted_people_cannot_send_or_request(msg):
    befriend()
    msg["st"].prefs[A] = {"msg_banned": True}
    assert post("A", "message_send", other_id=B, body="hi")[0] == 403
    assert post("A", "friend_request", other_id=C)[0] == 403
    assert get("A", "friends_search", guild_id=G1, query="Bo")[0] == 403
    assert get("A", "friends_list")[1]["banned"] is True
    msg["st"].prefs.clear(); msg["blacklist"].add(int(A))
    assert post("A", "message_send", other_id=B, body="hi")[0] == 403
    msg["blacklist"].clear(); msg["st"].prefs[B] = {"msg_banned": True}                # the OTHER person is banned
    st, p, _ = post("A", "message_send", other_id=B, body="hi")
    assert st == 409 and p["code"] == "paused"


# ───────────────────────── block / delete / unfriend ─────────────────────────
def test_block_ends_friendship_deletes_thread_and_stops_requests(msg):
    befriend()
    post("A", "message_send", other_id=B, body="hi")
    assert post("A", "friend_block", other_id=B)[0] == 200
    assert msg["st"].msgs == [] and get("A", "friends_list")[1]["friends"] == []
    assert [x["user_id"] for x in get("A", "friends_list")[1]["blocked"]] == [B]
    assert get("B", "friends_list")[1]["friends"] == [] and get("B", "friends_list")[1]["blocked"] == []   # B cannot tell
    assert post("B", "message_send", other_id=A, body="hello?")[0] == 403
    assert post("B", "friend_request", other_id=A)[0] == 200 and msg["st"].friends[M.pair(A, B)]["status"] == "blocked"
    assert post("A", "friend_unblock", other_id=B)[0] == 200 and M.pair(A, B) not in msg["st"].friends


def test_blocking_someone_who_blocked_you_does_not_replace_their_block(msg):
    post("B", "friend_block", other_id=A)
    post("A", "friend_block", other_id=B)
    assert msg["st"].friends[M.pair(A, B)]["blocked_by"] == B
    post("A", "friend_unblock", other_id=B)                                            # A never owned the block
    assert msg["st"].friends[M.pair(A, B)]["blocked_by"] == B


def test_cannot_unblock_a_block_someone_else_made(msg):
    post("B", "friend_block", other_id=A)
    post("A", "friend_unblock", other_id=B)
    assert M.pair(A, B) in msg["st"].friends


def test_unfriend_and_delete_conversation(msg):
    befriend()
    post("A", "message_send", other_id=B, body="hi")
    assert post("B", "message_thread_delete", other_id=A)[1]["deleted"] == 1
    assert get("A", "friends_list")[1]["friends"][0]["user_id"] == B                  # still friends
    post("A", "message_send", other_id=B, body="again")
    assert post("B", "friend_remove", other_id=A)[0] == 200 and msg["st"].msgs == []
    assert post("B", "friend_remove", other_id=A)[0] == 404


def test_thread_is_only_for_friends(msg):
    assert get("A", "messages_thread", other_id=B)[0] == 403
    post("A", "friend_request", other_id=B)
    assert get("A", "messages_thread", other_id=B)[0] == 403


# ───────────────────────── isolation ─────────────────────────
def test_a_third_member_can_never_read_or_touch_someone_elses_conversation(msg):
    befriend()
    post("A", "message_send", other_id=B, body="private")
    assert get("C", "messages_thread", other_id=A)[0] == 403 and get("C", "messages_thread", other_id=B)[0] == 403
    post("C", "message_read", other_id=A)                                              # reads only C's own rows: none
    assert msg["st"].msgs[0]["read_at"] is None
    assert post("C", "message_send", other_id=B, body="hi")[0] == 403
    assert "private" not in str(get("C", "friends_list")[1])
    assert post("C", "message_report", other_id=A)[0] == 404                           # nothing from A to C to report
    assert post("C", "message_thread_delete", other_id=A)[1]["deleted"] == 0 and len(msg["st"].msgs) == 1


def test_client_supplied_identities_are_ignored(msg):
    befriend()
    st, p, _ = post("A", "message_send", other_id=B, body="hi", user_id=C, uid=C, sender_id=C, me=C, recipient_id=C)
    assert st == 200 and msg["st"].msgs[0]["sender_id"] == A and msg["st"].msgs[0]["recipient_id"] == B
    post("A", "friend_request", other_id=C, requested_by=B, user_a=B, user_b=C)
    assert msg["st"].friends[M.pair(A, C)]["requested_by"] == A


# ───────────────────────── reports ─────────────────────────
def test_report_snapshots_the_conversation_and_is_audited(msg):
    befriend()
    post("B", "message_send", other_id=A, body="you owe me money")
    post("A", "message_send", other_id=B, body="who is this")
    st, p, _ = post("A", "message_report", other_id=B)
    assert st == 200 and p["reported"] is True
    rep = msg["st"].reports[0]
    snap = M.parse_snapshot(rep["snapshot"])
    assert [(m["from"], m["body"]) for m in snap] == [("reported", "you owe me money"), ("reporter", "who is this")]
    assert msg["audit"] == [(int(A), "member_report", f"report=1 reported={B}")]
    assert len(msg["st"].msgs) == 2                                                    # the conversation is kept unless blocked


def test_report_needs_a_message_from_that_person_and_is_not_duplicated(msg):
    befriend()
    assert post("A", "message_report", other_id=B)[0] == 404
    post("B", "message_send", other_id=A, body="spam")
    post("A", "message_report", other_id=B)
    again = post("A", "message_report", other_id=B)
    assert again[0] == 200 and len(msg["st"].reports) == 1 and "already" in again[1]["message"].lower()


def test_report_with_block_keeps_the_snapshot_but_deletes_the_conversation(msg):
    befriend()
    post("B", "message_send", other_id=A, body="spam")
    post("A", "message_report", other_id=B, also_block=True)
    assert msg["st"].msgs == [] and len(msg["st"].reports) == 1
    assert M.pair(A, B) in msg["st"].friends and msg["st"].friends[M.pair(A, B)]["blocked_by"] == A


def test_report_daily_cap(msg):
    for i in range(M.REPORTS_PER_DAY):
        msg["st"].reports.append({"id": 50 + i, "reporter_id": A, "reported_id": str(700000000000000000 + i), "snapshot": "[]",
                                  "status": "reviewed", "created_at": datetime.now(timezone.utc)})
    assert post("A", "message_report", other_id=B)[0] == 429


def test_reporting_works_even_when_messaging_is_switched_off(msg):
    befriend()
    post("B", "message_send", other_id=A, body="spam")
    msg["switches"].add("messaging")
    assert post("A", "message_report", other_id=B)[0] == 200


# ───────────────────────── kill switch ─────────────────────────
def test_kill_switch_stops_requests_accepts_and_sends_but_not_safety_actions(msg):
    befriend()
    post("A", "message_send", other_id=B, body="hi")
    post("C", "friend_request", other_id=A)
    msg["switches"].add("messaging")
    for res in (post("A", "message_send", other_id=B, body="no"), post("A", "friend_request", other_id=D),
                post("A", "friend_respond", other_id=C, accept=True)):
        assert res[0] == 503 and res[1]["code"] == "messaging_off"
    lst = get("A", "friends_list")[1]
    assert lst["messaging_off"] is True and get("A", "messages_thread", other_id=B)[1]["can_send"] is False
    assert post("A", "friend_respond", other_id=C, accept=False)[0] == 200             # decline still works
    assert post("A", "friend_block", other_id=B)[0] == 200 and post("A", "friend_unblock", other_id=B)[0] == 200
    assert post("A", "message_thread_delete", other_id=B)[0] == 200
    assert post("A", "msg_prefs_set", allow_requests=False)[0] == 200


# ───────────────────────── settings ─────────────────────────
def test_settings_default_on_for_requests_and_off_for_dm_notices(msg):
    s = get("A", "friends_list")[1]["settings"]
    assert s == {"allow_requests": True, "dm_notify": False}


def test_settings_are_booleans_only_and_scoped_to_the_session_user(msg):
    assert post("A", "msg_prefs_set", allow_requests="no")[0] == 422
    assert post("A", "msg_prefs_set")[0] == 422
    assert post("A", "msg_prefs_set", dm_notify=True, user_id=B)[1]["settings"]["dm_notify"] is True
    assert msg["st"].prefs.get(B, {}).get("dm_notify") is not True and msg["st"].prefs[A]["dm_notify"] is True
    post("A", "msg_prefs_set", allow_requests=False)
    post("B", "friend_request", other_id=A)
    assert M.pair(A, B) not in msg["st"].friends                                       # requests are off


def test_a_user_cannot_set_their_own_ban(msg):
    post("A", "msg_prefs_set", msg_banned=False, allow_requests=True)
    msg["st"].prefs[A] = {"msg_banned": True}
    post("A", "msg_prefs_set", msg_banned=False, allow_requests=True)
    assert msg["st"].prefs[A]["msg_banned"] is True


# ───────────────────────── header badge + unread ─────────────────────────
def test_me_reports_unread_member_messages(msg, monkeypatch):
    befriend()
    post("A", "message_send", other_id=B, body="hi")

    async def present():
        return set()

    async def clones(ids):
        return {}
    monkeypatch.setattr(dash, "_bot_guild_ids", present)
    monkeypatch.setattr(dash, "_clone_presence", clones)
    dash._owner_hits.clear()
    st, p, _ = call("GET", {"action": "me"}, token="B")
    assert st == 200 and p["msg_unread"] == 1


# ───────────────────────── owner side ─────────────────────────
def test_owner_section_is_registered_and_only_for_owners():
    from modules.admin_access import CLONE_ADMIN_SECTIONS, compute_sections
    assert "msgreports" in CLONE_ADMIN_SECTIONS
    assert "msgreports" in compute_sections(1, [1], []) and "msgreports" not in compute_sections(2, [1], [])
    from api import dash_owner_safety as S
    assert S.ROUTES["owner_msg_reports"][0] == "msgreports" and S.WRITES["owner_msg_report_resolve"][0] == "msgreports"
    assert S.WRITES["owner_msg_unban"][0] == "msgreports"


def _owner_prep(prep, msg, monkeypatch, **body):
    import database
    monkeypatch.setattr(database, "db", type("D", (), {})(), raising=False)
    for n in [n for n in dir(Store) if n.startswith("msg_")]:
        setattr(database.db, n, getattr(msg["st"], n))
    plan = asyncio.run(prep({"user": {"id": "1"}}, body))
    return plan, (asyncio.run(plan["fn"]()) if "fn" in plan else None)


def test_owner_queue_lists_reports_with_snapshots(msg, monkeypatch):
    befriend()
    post("B", "message_send", other_id=A, body="spam spam")
    post("A", "message_report", other_id=B)
    import database
    monkeypatch.setattr(database, "db", type("D", (), {})(), raising=False)
    database.db.msg_reports_new = msg["st"].msg_reports_new
    from api import dash_owner_safety as S
    out = asyncio.run(S.msg_reports(lambda k: None))
    assert out["rows"][0]["messages"][0]["body"] == "spam spam" and out["counts"] == {"new": 1}
    assert {a["id"] for a in out["actions"]} == set(S.MSG_REPORT_ACTIONS)


def test_owner_ban_sender_removes_friendship_blocks_messaging_and_can_be_undone(msg, monkeypatch):
    befriend()
    post("B", "message_send", other_id=A, body="spam")
    post("A", "message_report", other_id=B)
    from api import dash_owner_safety as S
    plan, res = _owner_prep(S.prep_msg_report_resolve, msg, monkeypatch, report_id="1", action="ban_sender")
    assert res["changed"] is True and msg["st"].prefs[B]["msg_banned"] is True
    assert M.pair(A, B) not in msg["st"].friends and msg["st"].msgs == [] and msg["st"].reports[0]["status"] == "reviewed"
    assert post("B", "friend_request", other_id=C)[0] == 403
    plan, res = _owner_prep(S.prep_msg_unban, msg, monkeypatch, user_id=B)
    assert msg["st"].prefs[B]["msg_banned"] is False and post("B", "friend_request", other_id=C)[0] == 200


def test_owner_actions_are_validated_and_idempotent(msg, monkeypatch):
    from api import dash_owner_safety as S
    plan, _ = _owner_prep(S.prep_msg_report_resolve, msg, monkeypatch, report_id="x", action="ban_sender")
    assert plan["_error"][0] == 422
    plan, _ = _owner_prep(S.prep_msg_report_resolve, msg, monkeypatch, report_id="1", action="delete_everything")
    assert plan["_error"][0] == 422
    plan, _ = _owner_prep(S.prep_msg_unban, msg, monkeypatch, user_id="nope")
    assert plan["_error"][0] == 422
    plan, res = _owner_prep(S.prep_msg_report_resolve, msg, monkeypatch, report_id="9", action="dismiss")
    assert res["changed"] is False


# ───────────────────────── retention + notices ─────────────────────────
def test_retention_is_30_days_and_purge_runs_daily():
    assert M.RETENTION_DAYS == 30
    cron = (Path(dash.__file__).parent / "cron_expire_monetization.py").read_text()
    assert "msg_purge_old(member_msg.RETENTION_DAYS)" in cron
    db_src = (Path(dash.__file__).parent.parent / "database.py").read_text()
    assert "DELETE FROM dash_member_messages WHERE created_at < NOW() - ($1 * INTERVAL '1 day')" in db_src


def test_dm_notice_is_generic_opt_in_and_disables_itself_on_closed_dms(monkeypatch):
    from api import cron_dash_dropbox_dm as cron
    sent, disabled = [], []
    fake = type("F", (), {})()

    async def claim(limit, cooldown_minutes=60):
        return ["111111111111111111", "222222222222222222"]

    async def disable(uid):
        disabled.append(uid)

    async def dm(session, token, uid, text):
        sent.append((uid, text))
        return "dm_failed:403" if uid == 222222222222222222 else None
    fake.msg_notify_claim, fake.msg_dm_disable = claim, disable
    monkeypatch.setattr(cron, "db", fake)
    monkeypatch.setattr(cron, "_dm_user", dm)
    monkeypatch.setattr(cron, "DISCORD_BOT_TOKEN", "t")
    monkeypatch.setattr(cron, "DM_DELAY_SECONDS", 0)
    ac = importlib.import_module("modules.admin_controls")

    async def none():
        return set()
    monkeypatch.setattr(ac, "current_switches", none)
    out = asyncio.run(cron.run_message_notices())
    assert out == {"sent": 1, "failed": 1} and disabled == ["222222222222222222"]
    assert all(t == M.NOTICE_TEXT for _, t in sent)
    assert not any(w in M.NOTICE_TEXT.lower() for w in ("from ", "said", "wrote"))      # no names, no message text

    async def off():
        return {"messaging"}
    monkeypatch.setattr(ac, "current_switches", off)
    assert asyncio.run(cron.run_message_notices())["skipped"] == "messaging_off"


# ───────────────────────── front end ─────────────────────────
def test_messages_page_never_uses_innerhtml_and_never_stores_anything():
    js = (Path(dash.__file__).parent.parent / "dashboard" / "assets" / "dash.js").read_text()
    i = js.index("function meMessages(")
    body = js[i:js.index("function renderMe(", i)]
    assert "innerHTML" not in body and "localStorage" not in body and "sessionStorage" not in body and "outerHTML" not in body
    owner = (Path(dash.__file__).parent.parent / "dashboard" / "assets" / "owner.js").read_text()
    j = owner.index("function msgReports(")
    assert "innerHTML" not in owner[j:owner.index("function statusPage(", j)]


def test_schema_has_the_four_tables_and_indexes():
    src = (Path(dash.__file__).parent.parent / "database.py").read_text()
    for t in ("dash_friends", "dash_member_messages", "dash_member_reports", "dash_member_prefs"):
        assert f"CREATE TABLE IF NOT EXISTS {t}" in src
    assert "CHECK (user_a <> user_b)" in src and "CHECK (status IN ('pending', 'accepted', 'blocked'))" in src
    assert 'SCHEMA_VERSION = "67"' in src


def test_report_snapshots_are_purged_90_days_after_resolution_and_the_privacy_page_says_so():
    assert M.REPORT_RETENTION_DAYS == 90
    root = Path(dash.__file__).parent
    assert "msg_reports_purge(member_msg.REPORT_RETENTION_DAYS)" in (root / "cron_expire_monetization.py").read_text()
    db_src = (root.parent / "database.py").read_text()
    assert "status <> 'new' AND resolved_at IS NOT NULL" in db_src          # open reports are never purged
    from api import legal_pages
    page = legal_pages.PRIVACY_HTML
    assert "30 days" in page and "90 days" in page and "not stored by us" in page and "up to 20" in page
