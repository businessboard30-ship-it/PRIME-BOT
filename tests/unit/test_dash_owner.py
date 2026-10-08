"""Owner area, Phase 0: permission matrix, step-up, fail-closed audit, session age, parity."""
import time
from urllib.parse import parse_qs, urlparse

import pytest

import config
from api import dash
from modules import admin_access
from tests.unit.test_dash_api import env, call  # noqa: F401  (fixture + helper reuse)

OWNER_ID = 77


@pytest.fixture
def own(env, monkeypatch):
    fake, _ = env
    now = int(time.time())
    fake.sessions["owner"] = {"kind": "dash", "iat": now, "guilds": [],
                              "user": {"id": str(OWNER_ID), "username": "boss", "avatar_url": ""}}
    fake.sessions["stale"] = dict(fake.sessions["owner"], iat=now - 3 * 3600)
    fake.sessions["noiat"] = {k: v for k, v in fake.sessions["owner"].items() if k != "iat"}
    fake.sessions["sid"]["iat"] = now          # normal guild admin (id 6)
    monkeypatch.setattr(config, "DASH_OWNER_IDS", {OWNER_ID})
    monkeypatch.setattr(config, "DISCORD_OWNER_BROADCAST_IDS", {OWNER_ID})
    monkeypatch.setattr(config, "DASH_OAUTH_REDIRECT_URI", "https://x/api/dash")
    monkeypatch.setattr(config, "DISCORD_OAUTH_CLIENT_SECRET", "s")
    monkeypatch.setattr(config, "DISCORD_OAUTH_CLIENT_ID", "cid")
    dash._owner_hits.clear()
    fake.audit_rows, fake.states, fake.patched, fake.fail_audit = [], {}, [], False
    fake.next_audit = 1

    async def audit_add(uid, name, section, action, target="", source="web", result="started", detail=None):
        if fake.fail_audit:
            raise RuntimeError("db down")
        i = fake.next_audit; fake.next_audit += 1
        fake.audit_rows.append(dict(id=i, user_id=str(uid), section=section, action=action, target=target, source=source, result=result))
        return i
    async def audit_result(i, result):
        for r in fake.audit_rows:
            if r["id"] == i:
                r["result"] = result
    async def audit_list(before=None, section=None, limit=50):
        return [dict(r, id=str(r["id"])) for r in reversed(fake.audit_rows)]
    async def mk_state(state, return_to=None):
        fake.states[state] = return_to
    async def pop_state(state):
        return {"return_to": fake.states.pop(state)} if state in fake.states else None
    async def patch(sid, p):
        fake.sessions[sid].update(p); fake.patched.append((sid, p)); return True
    async def signout_all(uid):
        gone = [k for k, v in fake.sessions.items() if str(v.get("user", {}).get("id")) == uid]
        for k in gone:
            fake.sessions.pop(k)
        return len(gone)
    fake.owner_audit_add, fake.owner_audit_set_result, fake.owner_audit_list = audit_add, audit_result, audit_list
    fake.create_login_oauth_state, fake.pop_login_oauth_state = mk_state, pop_state
    fake.update_login_session_payload, fake.delete_login_sessions_for_user = patch, signout_all
    return fake


OWNER_ROUTES = [("GET", {"action": "owner_audit"}, None),
                ("POST", None, {"action": "owner_stepup"}),
                ("POST", None, {"action": "owner_signout_all"})]


@pytest.mark.parametrize("method,query,body", OWNER_ROUTES)
def test_permission_matrix(own, method, query, body):
    assert call(method, query, token=None, body=body)[0] == 401          # anonymous
    assert call(method, query, token="nope", body=body)[0] == 401        # unknown session
    assert call(method, query, token="sid", body=body)[0] == 403         # normal guild admin


def test_owner_reads_audit_and_me_lists_sections(own):
    assert call("GET", {"action": "owner_audit"}, token="owner")[0] == 200
    _, p, _ = call("GET", {"action": "me"}, token="owner")
    assert "payments" in p["owner_sections"] and "audit" in p["owner_sections"]
    _, p, _ = call("GET", {"action": "me"}, token="sid")
    assert p["owner_sections"] == []


def test_helper_gets_nothing_on_web_in_v1(own, monkeypatch):
    own.sessions["helper"] = {"kind": "dash", "iat": int(time.time()), "guilds": [], "user": {"id": "88", "username": "h"}}
    from modules import admin_controls
    monkeypatch.setitem(admin_controls._helpers, "map", {88: {"audit", "blacklist"}})
    assert call("GET", {"action": "owner_audit"}, token="helper")[0] == 403


def test_owner_sessions_expire_early(own):
    assert call("GET", {"action": "owner_audit"}, token="stale")[0] == 401
    assert call("GET", {"action": "owner_audit"}, token="noiat")[0] == 401


def test_stepup_start_audited_and_binds_session(own):
    st, p, _ = call("POST", token="owner", body={"action": "owner_stepup"})
    assert st == 200
    q = parse_qs(urlparse(p["url"]).query)
    assert q["prompt"] == ["consent"]
    assert own.states[q["state"][0]] == "dash_stepup:owner"
    assert [(r["section"], r["action"], r["result"]) for r in own.audit_rows] == [("session", "stepup_start", "ok")]


def test_stepup_rate_limited(own):
    codes = [call("POST", token="owner", body={"action": "owner_stepup"})[0] for _ in range(6)]
    assert codes == [200] * 5 + [429]


def _fake_discord(monkeypatch, user_id):
    class R:
        def __init__(self, data): self.data = data
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        def raise_for_status(self): pass
        async def json(self): return self.data
    class S:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        def post(self, *a, **k): return R({"access_token": "t"})
        def get(self, url, **k): return R({"id": str(user_id), "username": "x"} if url.endswith("@me") else [])
    monkeypatch.setattr(dash.aiohttp, "ClientSession", S)


def test_stepup_callback_marks_same_session_fresh(own, monkeypatch):
    own.states["st1"] = "dash_stepup:owner"
    _fake_discord(monkeypatch, OWNER_ID)
    st, _, loc = call("GET", {"code": "c", "state": "st1"}, token=None)
    assert st == 302 and loc.endswith("#owner=stepup_ok")
    assert own.sessions["owner"]["fresh_until"] > time.time()
    assert call("POST", token="owner", body={"action": "owner_signout_all", "confirm": "SIGN OUT"})[0] == 200


def test_stepup_callback_rejects_a_different_account(own, monkeypatch):
    own.states["st2"] = "dash_stepup:owner"
    _fake_discord(monkeypatch, 999)                      # someone else signs in
    st, _, loc = call("GET", {"code": "c", "state": "st2"}, token=None)
    assert st == 302 and "error=" in loc and not own.patched


def test_stepup_state_is_single_use(own, monkeypatch):
    own.states["st3"] = "dash_stepup:owner"
    _fake_discord(monkeypatch, OWNER_ID)
    call("GET", {"code": "c", "state": "st3"}, token=None)
    _, _, loc = call("GET", {"code": "c", "state": "st3"}, token=None)
    assert "error=" in loc


def test_destructive_action_needs_fresh_stepup(own):
    st, p, _ = call("POST", token="owner", body={"action": "owner_signout_all"})
    assert st == 403 and p["code"] == "stepup_required" and "owner" in own.sessions
    own.sessions["owner"]["fresh_until"] = time.time() - 1        # expired window
    assert call("POST", token="owner", body={"action": "owner_signout_all"})[0] == 403
    own.sessions["owner"]["fresh_until"] = time.time() + 60
    assert call("POST", token="owner", body={"action": "owner_signout_all"})[0] == 422     # fresh but not typed
    assert "owner" in own.sessions
    st, p, _ = call("POST", token="owner", body={"action": "owner_signout_all", "confirm": "SIGN OUT"})
    # every session of this user goes (active + the two stale fixtures), nobody else's
    assert st == 200 and p["signed_out"] == 3 and not {"owner", "stale", "noiat"} & set(own.sessions)
    assert "sid" in own.sessions
    assert own.audit_rows[-1]["action"] == "signout_all" and own.audit_rows[-1]["result"] == "ok"


def test_audit_failure_blocks_the_write(own):
    own.sessions["owner"]["fresh_until"] = time.time() + 60
    own.fail_audit = True
    assert call("POST", token="owner", body={"action": "owner_signout_all", "confirm": "SIGN OUT"})[0] == 503
    assert "owner" in own.sessions                                  # nothing happened


def test_require_confirm_is_exact():
    dash._require_confirm({"confirm": "REVERSE"}, "REVERSE")
    for bad in ({}, {"confirm": "reverse"}, {"confirm": " REVERSE"}):
        with pytest.raises(dash._Reply) as e:
            dash._require_confirm(bad, "REVERSE")
        assert e.value.status == 422


# ── parity with the Discord panel ─────────────────────────────────────────

def test_compute_sections_rules():
    cs = admin_access.compute_sections
    assert cs(1, {1}, {1}) == admin_access.CLONE_ADMIN_SECTIONS | admin_access.BROADCAST_SECTIONS
    assert cs(2, {1}, {1}) == set()
    assert cs(3, {1}, {3}) == {"broadcast", "feedback"}
    assert cs(4, {1}, {1}, helper_sections={"audit", "logs"}) == {"audit", "logs"}
    assert cs(4, {1}, {1}, helper_sections={"config", "access", "database", "audit"}) == {"audit"}   # owner-only never granted


def test_discord_allowed_sections_matches_shared_logic(monkeypatch):
    import importlib, sys
    vp = importlib.import_module("discord_bot.cogs._views_admin_panel")
    monkeypatch.setattr(vp, "DISCORD_CLONE_ADMIN_IDS", {1})
    monkeypatch.setattr(vp, "DISCORD_OWNER_BROADCAST_IDS", {1, 3})
    from modules import admin_controls
    monkeypatch.setitem(admin_controls._helpers, "map", {})
    for uid in (1, 2, 3):
        assert vp.allowed_sections(uid) == admin_access.compute_sections(uid, {1}, {1, 3})
    # the section list every Discord owner sees is exactly what the refactor started from
    assert {"payments", "servers", "money", "health", "inspect", "scamshield", "referral", "ads",
            "broadcast", "feedback", "config", "database", "access"} <= vp.allowed_sections(1)
