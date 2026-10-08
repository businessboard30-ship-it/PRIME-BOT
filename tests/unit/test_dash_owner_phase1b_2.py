"""Owner area Phase 1b (worker snapshot) + Phase 2 (safe controls): permissions, step-up, confirm,
fail-closed audit, input validation, secret masking."""
import importlib
import time
from datetime import datetime, timedelta, timezone

import pytest

from tests.unit.test_dash_api import env, call  # noqa: F401
from tests.unit.test_dash_owner import own, OWNER_ID  # noqa: F401

BIG = 1534574875274903562
NOW = datetime.now(timezone.utc)


@pytest.fixture
def ctl(own, monkeypatch):
    ac = importlib.import_module("modules.admin_controls")
    calls = []
    engaged = set()
    async def get_engaged(): return set(engaged)
    async def set_switch(sw, eng, by): calls.append(("switch", sw, eng, by)); (engaged.add if eng else engaged.discard)(sw)
    async def bl_add(kind, tid, reason, by): calls.append(("bl_add", kind, tid, reason, by))
    async def bl_rm(kind, tid): calls.append(("bl_rm", kind, tid)); return True
    async def bl_list(limit=25): return [{"kind": "user", "target_id": BIG, "reason": "r", "added_by": 1, "created_at": NOW}]
    async def prem_list(limit=25): return [{"guild_id": BIG, "clone_id": None, "expires_at": NOW, "guild_name": "G"}]
    async def revoke(gid, clone): calls.append(("revoke", gid, clone)); return True
    async def grant(gid, by, days, clone): calls.append(("grant", gid, by, days, clone)); return NOW + timedelta(days=days)
    async def baudit(before=None, action=None, guild_id=None, admin_id=None, limit=50):
        calls.append(("baudit", before, action, guild_id, admin_id))
        return [{"id": 9, "admin_id": BIG, "action": "premium.grant", "guild_id": BIG, "details": "x" * 900, "created_at": NOW}]
    monkeypatch.setattr(ac, "grant_premium", grant)
    monkeypatch.setattr(ac, "list_bot_audit", baudit)
    for k, v in dict(get_engaged_switches=get_engaged, set_switch=set_switch, add_blacklist=bl_add, remove_blacklist=bl_rm,
                     list_blacklist=bl_list, list_premium=prem_list, revoke_premium=revoke).items():
        monkeypatch.setattr(ac, k, v)
    own.calls = calls
    own.sessions["owner"]["fresh_until"] = 0
    own.snaps = {}
    async def snap_get(key): return own.snaps.get(key)
    own.bot_snapshot_get = snap_get
    return own


def fresh(ctl):
    ctl.sessions["owner"]["fresh_until"] = time.time() + 300


READS = [{"action": a} for a in ("owner_botaudit", "owner_logs", "owner_config", "owner_controls", "owner_blacklist", "owner_premium", "owner_feedback")]


@pytest.mark.parametrize("query", READS)
def test_reads_permission_matrix(ctl, query):
    assert call("GET", query, token=None)[0] == 401
    assert call("GET", query, token="sid")[0] == 403
    assert call("GET", query, token="stale")[0] == 401
    assert call("GET", query, token="owner")[0] == 200


WRITES = [{"action": "owner_switch", "switch": "ai", "engaged": True},
          {"action": "owner_blacklist_add", "kind": "user", "target_id": str(BIG)},
          {"action": "owner_blacklist_remove", "kind": "user", "target_id": str(BIG)},
          {"action": "owner_premium_revoke", "guild_id": str(BIG), "confirm": "REVOKE"},
          {"action": "owner_premium_grant", "guild_id": str(BIG), "days": "30", "confirm": "GRANT"},
          {"action": "owner_announce", "title": "t", "body": "b"},
          {"action": "owner_announce_delete", "id": "5"}]


@pytest.mark.parametrize("body", WRITES)
def test_writes_permission_matrix(ctl, body):
    assert call("POST", token=None, body=body)[0] == 401
    assert call("POST", token="nope", body=body)[0] == 401
    assert call("POST", token="sid", body=body)[0] == 403
    assert call("POST", token="stale", body=body)[0] == 401
    assert ctl.calls == [] and ctl.audit_rows == []        # nothing ran, nothing audited


def test_helper_cannot_write_on_web(ctl, monkeypatch):
    ctl.sessions["helper"] = {"kind": "dash", "iat": int(time.time()), "guilds": [], "user": {"id": "88", "username": "h"}}
    ac = importlib.import_module("modules.admin_controls")
    monkeypatch.setitem(ac._helpers, "map", {88: {"blacklist", "premium"}})
    assert call("POST", token="helper", body={"action": "owner_blacklist_add", "kind": "user", "target_id": str(BIG)})[0] == 403


# ---------- switches ----------
def test_feature_switch_is_audited_and_runs(ctl):
    st, p, _ = call("POST", token="owner", body={"action": "owner_switch", "switch": "ai", "engaged": True})
    assert st == 200 and ctl.calls == [("switch", "ai", True, OWNER_ID)]
    assert [(r["section"], r["action"], r["target"], r["result"]) for r in ctl.audit_rows] == [("controls", "owner_switch", "ai", "ok")]


@pytest.mark.parametrize("bad", [{"switch": "nope", "engaged": True}, {"switch": "ai", "engaged": "yes"}, {"switch": 5, "engaged": True}, {"engaged": True}])
def test_switch_validation(ctl, bad):
    assert call("POST", token="owner", body={"action": "owner_switch", **bad})[0] == 422
    assert ctl.calls == []


def test_maintenance_needs_stepup_and_typed_confirm(ctl):
    body = {"action": "owner_switch", "switch": "maintenance", "engaged": True}
    st, p, _ = call("POST", token="owner", body=body)
    assert st == 403 and p["code"] == "stepup_required"
    fresh(ctl)
    assert call("POST", token="owner", body=body)[0] == 422
    assert call("POST", token="owner", body={**body, "confirm": "maintenance"})[0] == 422     # case-sensitive
    assert ctl.calls == []
    assert call("POST", token="owner", body={**body, "confirm": "MAINTENANCE"})[0] == 200
    assert ctl.calls == [("switch", "maintenance", True, OWNER_ID)]


def test_releasing_maintenance_needs_no_stepup(ctl):
    assert call("POST", token="owner", body={"action": "owner_switch", "switch": "maintenance", "engaged": False})[0] == 200


def test_opening_build_bot_needs_stepup(ctl):
    body = {"action": "owner_switch", "switch": "build_bot_public", "engaged": True, "confirm": "OPEN"}
    assert call("POST", token="owner", body=body)[0] == 403
    fresh(ctl)
    assert call("POST", token="owner", body=body)[0] == 200


def test_controls_lists_every_switch(ctl):
    _, p, _ = call("GET", {"action": "owner_controls"}, token="owner")
    keys = {s["key"] for s in p["switches"]}
    assert {"maintenance", "ai", "build_bot_public"} <= keys


# ---------- fail-closed audit ----------
def test_audit_failure_blocks_the_action(ctl):
    ctl.fail_audit = True
    st, _, _ = call("POST", token="owner", body={"action": "owner_switch", "switch": "ai", "engaged": True})
    assert st == 503 and ctl.calls == []


# ---------- blacklist ----------
def test_blacklist_add_remove(ctl):
    fresh(ctl)
    st, p, _ = call("POST", token="owner", body={"action": "owner_blacklist_add", "kind": "guild", "target_id": str(BIG), "reason": "spam\x00\n bad", "confirm": "BLOCK"})
    assert st == 200 and p["target_id"] == str(BIG)          # ids come back as strings
    assert ctl.calls[0][:3] == ("bl_add", "guild", BIG) and "\x00" not in ctl.calls[0][3]
    assert call("POST", token="owner", body={"action": "owner_blacklist_remove", "kind": "guild", "target_id": str(BIG)})[1]["removed"] is True


@pytest.mark.parametrize("bad", [{"kind": "role", "target_id": str(BIG)}, {"kind": "user", "target_id": "12"}, {"kind": "user", "target_id": "abc" * 5}, {"kind": "user"}])
def test_blacklist_validation(ctl, bad):
    assert call("POST", token="owner", body={"action": "owner_blacklist_add", **bad})[0] == 422


def test_cannot_blacklist_an_owner(ctl, monkeypatch):
    import config
    monkeypatch.setattr(config, "DASH_OWNER_IDS", {OWNER_ID, 5555555555})
    assert call("POST", token="owner", body={"action": "owner_blacklist_add", "kind": "user", "target_id": "5555555555"})[0] == 422
    fresh(ctl)
    assert call("POST", token="owner", body={"action": "owner_blacklist_add", "kind": "guild", "target_id": "5555555555", "confirm": "BLOCK"})[0] == 200  # a server with that number is fine
    assert [c[0] for c in ctl.calls] == ["bl_add"]


# ---------- premium ----------
def test_premium_revoke_needs_stepup_and_confirm(ctl):
    body = {"action": "owner_premium_revoke", "guild_id": str(BIG), "clone": "9"}
    r = call("POST", token="owner", body={**body, "confirm": "REVOKE"})
    assert r[1]["code"] == "stepup_required"
    fresh(ctl)
    assert call("POST", token="owner", body=body)[0] == 422
    assert call("POST", token="owner", body={**body, "confirm": "REVOKE"})[0] == 200
    assert ctl.calls == [("revoke", BIG, 9)]
    assert ctl.audit_rows[-1]["target"] == f"{BIG}/9"


def test_premium_revoke_validation(ctl):
    fresh(ctl)
    assert call("POST", token="owner", body={"action": "owner_premium_revoke", "guild_id": "1", "confirm": "REVOKE"})[0] == 422
    assert call("POST", token="owner", body={"action": "owner_premium_revoke", "guild_id": str(BIG), "clone": "x", "confirm": "REVOKE"})[0] == 422


# ---------- announcements ----------
def test_announce_dashboard_only_needs_no_stepup(ctl):
    made = []
    async def create(*a): made.append(a); return 41
    ctl.dropbox_create = create
    st, p, _ = call("POST", token="owner", body={"action": "owner_announce", "title": "Hi", "body": "Maintenance tonight"})
    assert st == 200 and p["id"] == "41" and made and ctl.audit_rows[-1]["section"] == "broadcast"


def test_announce_push_dm_needs_stepup_and_send(ctl):
    async def resolve(aud, mm, tg): return [111, 222]
    async def create(*a): return 7
    async def enqueue(mid, rec, push): return len(rec)
    ctl.dropbox_resolve_recipients, ctl.dropbox_create, ctl.dropbox_enqueue = resolve, create, enqueue
    body = {"action": "owner_announce", "title": "Hi", "body": "x", "push_dm": True}
    assert call("POST", token="owner", body=body)[1]["code"] == "stepup_required"
    fresh(ctl)
    assert call("POST", token="owner", body=body)[0] == 422
    st, p, _ = call("POST", token="owner", body={**body, "confirm": "SEND"})
    assert st == 200 and p["recipients"] == 2


def test_announce_validation_and_no_recipients(ctl):
    assert call("POST", token="owner", body={"action": "owner_announce", "title": "", "body": "b"})[0] == 422
    async def resolve(aud, mm, tg): return []
    ctl.dropbox_resolve_recipients = resolve
    fresh(ctl)
    assert call("POST", token="owner", body={"action": "owner_announce", "title": "t", "body": "b", "push_dm": True, "confirm": "SEND"})[0] == 422


def test_announce_delete_bad_id(ctl):
    assert call("POST", token="owner", body={"action": "owner_announce_delete", "id": "x"})[0] == 400


def test_broadcast_section_follows_broadcast_ids(ctl, monkeypatch):
    import config
    monkeypatch.setattr(config, "DISCORD_OWNER_BROADCAST_IDS", set())      # an owner who is not a broadcast owner
    assert call("POST", token="owner", body={"action": "owner_announce", "title": "t", "body": "b"})[0] == 403
    assert call("GET", {"action": "owner_feedback"}, token="owner")[0] == 403


# ---------- feedback ----------
def test_feedback_truncates_and_stringifies_ids(ctl):
    async def fb(limit): return [{"id": 1, "user_id": BIG, "guild_id": BIG, "message": "x" * 5000, "created_at": NOW}]
    ctl.get_discord_user_feedback = fb
    _, p, _ = call("GET", {"action": "owner_feedback"}, token="owner")
    r = p["rows"][0]
    assert len(r["message"]) == 1000 and r["user_id"] == str(BIG)


# ---------- worker snapshot ----------
def test_health_merges_snapshot_and_flags_stale(ctl, monkeypatch):
    ai = importlib.import_module("modules.admin_inspect")
    async def ping(): return 1.0
    async def counts(): return {"main": 1, "clones": 0}
    async def beats(): return []
    for k, v in dict(db_ping=ping, guild_counts=counts, clone_heartbeats=beats).items():
        monkeypatch.setattr(ai, k, v)
    _, p, _ = call("GET", {"action": "owner_health"}, token="owner")
    assert p["live_bot"] is None
    ctl.snaps["main"] = {"payload": {"uptime": "3h", "latency_ms": 90, "published_at": 1}, "updated_at": datetime.now(timezone.utc) - timedelta(seconds=20)}
    _, p, _ = call("GET", {"action": "owner_health"}, token="owner")
    assert p["live_bot"]["uptime"] == "3h" and p["live_bot"]["stale"] is False and "published_at" not in p["live_bot"]
    ctl.snaps["main"]["updated_at"] = datetime.now(timezone.utc) - timedelta(minutes=10)
    assert call("GET", {"action": "owner_health"}, token="owner")[1]["live_bot"]["stale"] is True


def test_logs_and_config_served_from_snapshot(ctl):
    assert call("GET", {"action": "owner_logs"}, token="owner")[1]["available"] is False
    ctl.snaps["logs"] = {"payload": {"lines": [{"level": "ERROR", "message": "a"}, {"level": "WARNING", "message": "b"}]}, "updated_at": NOW}
    ctl.snaps["config"] = {"payload": {"entries": [{"name": "FOO_X", "shown": "1", "secret": False}, {"name": "BAR", "shown": "🔒 set", "secret": True}]}, "updated_at": NOW}
    assert len(call("GET", {"action": "owner_logs", "level": "error"}, token="owner")[1]["lines"]) == 1
    _, p, _ = call("GET", {"action": "owner_config", "q": "foo"}, token="owner")
    assert [e["name"] for e in p["entries"]] == ["FOO_X"] and p["source"] == "bot worker"


# ---------- publisher (worker side) ----------
def test_publisher_masks_secrets_and_survives_a_failing_key(monkeypatch):
    import logging
    sn = importlib.import_module("modules.admin_snapshot")
    ops = importlib.import_module("modules.admin_ops")
    ops.install_log_buffer()
    logging.getLogger("t").error("boom token=abc123SECRETVALUE postgres://u:hunter2@h/db")
    lines = sn.build_logs()
    blob = repr(lines)
    assert "abc123SECRETVALUE" not in blob and "hunter2" not in blob
    written = {}

    class DB:
        async def bot_snapshot_put(self, key, payload):
            if key == "logs":
                raise RuntimeError("db hiccup")
            written[key] = payload
    monkeypatch.setattr(sn, "build_main", lambda bot: {"uptime": "1m"})
    import asyncio
    asyncio.run(sn.publish(object(), DB()))
    assert "main" in written and "config" in written and "logs" not in written       # one failure doesn't block the rest
    assert all(not e["shown"].startswith("postgres://u:") for e in written["config"]["entries"])


# ---------- Phase 2 gap fixes ----------
def test_server_blacklist_needs_stepup_and_block_but_user_does_not(ctl):
    body = {"action": "owner_blacklist_add", "kind": "guild", "target_id": str(BIG)}
    assert call("POST", token="owner", body={**body, "confirm": "BLOCK"})[1]["code"] == "stepup_required"
    fresh(ctl)
    assert call("POST", token="owner", body=body)[0] == 422
    assert call("POST", token="owner", body={**body, "confirm": "block"})[0] == 422
    assert ctl.calls == []
    assert call("POST", token="owner", body={**body, "confirm": "BLOCK"})[0] == 200
    ctl.calls.clear()
    assert call("POST", token="owner", body={"action": "owner_blacklist_add", "kind": "user", "target_id": "2222222222"})[0] == 200


def test_premium_grant_stepup_confirm_and_validation(ctl):
    body = {"action": "owner_premium_grant", "guild_id": str(BIG), "days": "30", "clone": "9"}
    assert call("POST", token="owner", body={**body, "confirm": "GRANT"})[1]["code"] == "stepup_required"
    fresh(ctl)
    assert call("POST", token="owner", body=body)[0] == 422
    for bad in ({"days": "0"}, {"days": "3651"}, {"days": "-5"}, {"days": "x"}, {"days": ""}, {"guild_id": "12"}, {"clone": "z"}):
        assert call("POST", token="owner", body={**body, "confirm": "GRANT", **bad})[0] == 422
    assert ctl.calls == []
    st, p, _ = call("POST", token="owner", body={**body, "confirm": "GRANT"})
    assert st == 200 and p["guild_id"] == str(BIG) and ctl.calls == [("grant", BIG, OWNER_ID, 30, 9)]
    assert (ctl.audit_rows[-1]["action"], ctl.audit_rows[-1]["target"]) == ("owner_premium_grant", f"{BIG}/9")


def test_bot_audit_filters_validation_and_truncation(ctl):
    _, p, _ = call("GET", {"action": "owner_botaudit", "what": "x", "guild_id": str(BIG), "admin_id": str(BIG), "before": "50"}, token="owner")
    assert ctl.calls[-1] == ("baudit", 50, "x", BIG, BIG) and len(p["rows"][0]["details"]) == 500 and p["rows"][0]["admin_id"] == str(BIG)
    for bad in ({"guild_id": "abc"}, {"guild_id": "12"}, {"before": "x"}, {"admin_id": "1; drop"}):
        assert call("GET", {"action": "owner_botaudit", **bad}, token="owner")[0] == 422
