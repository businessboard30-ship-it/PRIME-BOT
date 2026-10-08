"""Owner area Phase 5: helpers, clones, named database cleanup. Permissions, step-up, typed confirm,
fail-closed audit, validation, and that a bot token never reaches the audit table, a response or the logs."""
import importlib
import json
import logging
import time
from datetime import datetime, timezone

import pytest

from api import dash

from tests.unit.test_dash_api import env, call  # noqa: F401
from tests.unit.test_dash_owner import own, OWNER_ID  # noqa: F401

BIG = 1534574875274903562
NOW = datetime.now(timezone.utc)
TOKEN = "-".join(["fake", "x" * 20, "y" * 20, "z" * 20])   # built at runtime: a literal would trip secret scanning
HELPER = 1234567890123456789          # NB: BIG is the real hardcoded owner id, so it can never be a helper
CLONE = {"clone_id": 3, "owner_id": OWNER_ID, "bot_user_id": BIG, "bot_username": "CloneBot", "application_id": BIG,
         "status": "active", "last_heartbeat": NOW, "created_at": NOW, "parent_clone_id": None}


@pytest.fixture
def p5(own, monkeypatch):
    ac = importlib.import_module("modules.admin_controls")
    acl = importlib.import_module("modules.admin_clones")
    ops = importlib.import_module("modules.admin_ops")
    cs = importlib.import_module("discord_clone_service")
    crypto = importlib.import_module("utils.crypto")
    calls, st = [], {"helpers": [{"user_id": HELPER, "sections": {"audit", "logs"}, "added_by": 1, "created_at": NOW, "updated_at": NOW}],
                     "clone": dict(CLONE), "owned": []}

    async def list_helpers(limit=25): return [dict(r) for r in st["helpers"]]
    async def set_helper(uid, sections, by): calls.append(("set_helper", uid, sorted(sections), by)); return set(sections)
    async def remove_helper(uid): calls.append(("remove_helper", uid)); return True
    monkeypatch.setattr(ac, "list_helpers", list_helpers)
    monkeypatch.setattr(ac, "set_helper", set_helper)
    monkeypatch.setattr(ac, "remove_helper", remove_helper)

    async def list_clones(limit=100): return [dict(st["clone"])]
    monkeypatch.setattr(acl, "list_clones", list_clones)

    class FakeDb:
        async def get_discord_clones_by_owner(self, owner): return list(st["owned"])
        async def get_discord_clone(self, cid): return dict(st["clone"]) if cid == 3 else None
        async def set_discord_clone_status(self, cid, status): calls.append(("status", cid, status)); st["clone"]["status"] = status
        async def create_discord_clone(self, **kw): calls.append(("create", {k: v for k, v in kw.items() if k != "bot_token_encrypted"})); return 9
        async def relink_discord_clone(self, **kw): calls.append(("relink", kw["clone_id"], kw["owner_id"])); return True
    monkeypatch.setattr(acl, "_db", lambda: FakeDb())

    async def validate(token): return {"ok": token == TOKEN, "error": "Discord rejected this token.", "bot_user_id": BIG, "bot_username": "CloneBot", "application_id": BIG}
    async def install(token): return {"ok": True}
    monkeypatch.setattr(cs, "validate_bot_token", validate)
    monkeypatch.setattr(cs, "set_default_install_params", install)
    monkeypatch.setattr(crypto.secret_manager, "encrypt", lambda t: "ENC:" + str(len(t)), raising=False)

    async def overview(): return {"pool": {"size": 5}, "counts": [("users", 10), ("payment_logs", 4)], "approx": {"payment_logs"}, "stale_payments": 2}
    async def cleanup():
        calls.append(("cleanup",)); return 2
    monkeypatch.setattr(ops, "db_overview", overview)
    monkeypatch.setattr(ops, "run_stale_payment_cleanup", cleanup)
    own.calls, own.st = calls, st
    own.sessions["owner"]["fresh_until"] = 0
    return own


def fresh(m):
    m.sessions["owner"]["fresh_until"] = time.time() + 300


READS = [{"action": "owner_helpers"}, {"action": "owner_clones"}, {"action": "owner_database"}]
WRITES = [{"action": "owner_helper_set", "user_id": str(HELPER), "sections": ["audit"]},
          {"action": "owner_helper_remove", "user_id": str(HELPER), "confirm": "REMOVE"},
          {"action": "owner_clone_register", "token": TOKEN, "confirm": "REGISTER"},
          {"action": "owner_clone_relink", "clone": "3", "token": TOKEN, "confirm": "RELINK"},
          {"action": "owner_clone_stop", "clone": "3", "confirm": "STOP"},
          {"action": "owner_db_cleanup_stale", "confirm": "CLEANUP"}]
NEEDS_STEPUP = [w for w in WRITES if w["action"] != "owner_helper_set"]


@pytest.mark.parametrize("query", READS)
def test_read_matrix(p5, query):
    assert call("GET", query, token=None)[0] == 401
    assert call("GET", query, token="sid")[0] == 403           # normal guild admin
    assert call("GET", query, token="stale")[0] == 401         # owner session too old
    assert call("GET", query, token="owner")[0] == 200


@pytest.mark.parametrize("body", WRITES)
def test_write_matrix(p5, body):
    assert call("POST", token=None, body=body)[0] == 401
    assert call("POST", token="sid", body=body)[0] == 403
    assert call("POST", token="stale", body=body)[0] == 401


@pytest.mark.parametrize("body", NEEDS_STEPUP)
def test_stale_signin_is_rejected(p5, body):
    s, payload, _ = call("POST", token="owner", body=body)
    assert s == 403 and payload["code"] == "stepup_required"
    assert p5.calls == []


@pytest.mark.parametrize("body", NEEDS_STEPUP)
def test_typed_confirmation_required(p5, body):
    fresh(p5)
    for bad in ("", "nope", body["confirm"].lower()):
        dash._owner_hits.clear()
        assert call("POST", token="owner", body={**body, "confirm": bad})[0] == 422
    assert p5.calls == []


@pytest.mark.parametrize("body", WRITES)
def test_audit_failure_blocks_the_write(p5, body):
    fresh(p5)
    p5.fail_audit = True
    assert call("POST", token="owner", body=body)[0] == 503
    assert p5.calls == []


@pytest.mark.parametrize("body", WRITES)
def test_success_writes_one_audit_row(p5, body):
    fresh(p5)
    s, payload, _ = call("POST", token="owner", body=body)
    assert s == 200 and payload["ok"] is True
    assert [r["action"] for r in p5.audit_rows] == [body["action"]] and p5.audit_rows[0]["result"] == "ok"


# ── helpers ──
def test_helper_validation(p5):
    for b in ({"user_id": "5", "sections": ["audit"]},                       # not a full id
              {"user_id": str(HELPER), "sections": []},
              {"user_id": str(HELPER), "sections": ["access"]},                 # owner-only: never grantable
              {"user_id": str(HELPER), "sections": ["audit", "config"]},
              {"user_id": str(HELPER), "sections": "audit"},
              {"user_id": str(OWNER_ID), "sections": ["audit"]},             # an owner
              {"user_id": str(BIG), "sections": ["audit"]}):                 # the hardcoded owner
        dash._owner_hits.clear()
        assert call("POST", token="owner", body={"action": "owner_helper_set", **b})[0] == 422
    assert p5.calls == []


def test_helper_set_and_list(p5):
    assert call("POST", token="owner", body={"action": "owner_helper_set", "user_id": str(HELPER), "sections": ["logs", "audit"]})[0] == 200
    assert p5.calls == [("set_helper", HELPER, ["audit", "logs"], OWNER_ID)]
    _, j, _ = call("GET", {"action": "owner_helpers"}, token="owner")
    assert j["rows"][0]["sections"] == ["audit", "logs"] and set(j["grantable"]) == {"audit", "blacklist", "premium", "logs"}
    assert "access" not in j["grantable"]


# ── clones ──
def test_clone_list_has_no_secrets(p5):
    _, j, _ = call("GET", {"action": "owner_clones"}, token="owner")
    blob = json.dumps(j)
    assert "token" not in blob.lower() and j["rows"][0]["clone_id"] == "3"


def test_register_ok_and_token_never_leaks(p5, caplog):
    fresh(p5)
    caplog.set_level(logging.DEBUG)
    s, j, _ = call("POST", token="owner", body={"action": "owner_clone_register", "token": TOKEN, "confirm": "REGISTER"})
    assert s == 200 and j["clone_id"] == "9" and j["bot_username"] == "CloneBot"
    assert p5.calls[0][0] == "create" and p5.calls[0][1]["owner_id"] == OWNER_ID
    assert TOKEN not in json.dumps(j) and TOKEN not in json.dumps(p5.audit_rows) and TOKEN not in caplog.text


def test_register_refuses_second_clone_and_bad_tokens(p5):
    fresh(p5)
    p5.st["owned"] = [dict(CLONE)]
    s, j, _ = call("POST", token="owner", body={"action": "owner_clone_register", "token": TOKEN, "confirm": "REGISTER"})
    assert s == 422 and "relink" in j["message"].lower() and p5.audit_rows[-1]["result"] == "denied"
    p5.st["owned"] = []
    for bad in ("", "short", "has space " * 6, TOKEN + "!"):
        dash._owner_hits.clear()
        assert call("POST", token="owner", body={"action": "owner_clone_register", "token": bad, "confirm": "REGISTER"})[0] == 422
    dash._owner_hits.clear()
    other = "X" * 30 + "." + "Y" * 30                                     # right shape, Discord rejects it
    s, j, _ = call("POST", token="owner", body={"action": "owner_clone_register", "token": other, "confirm": "REGISTER"})
    assert s == 422 and other not in json.dumps(j) and other not in json.dumps(p5.audit_rows)
    assert not [c for c in p5.calls if c[0] == "create"]


def test_relink_only_your_own_clone(p5):
    fresh(p5)
    assert call("POST", token="owner", body={"action": "owner_clone_relink", "clone": "3", "token": TOKEN, "confirm": "RELINK"})[0] == 200
    assert ("relink", 3, OWNER_ID) in p5.calls
    dash._owner_hits.clear()
    p5.st["clone"]["owner_id"] = BIG                                       # someone else's clone
    assert call("POST", token="owner", body={"action": "owner_clone_relink", "clone": "3", "token": TOKEN, "confirm": "RELINK"})[0] == 422
    assert call("POST", token="owner", body={"action": "owner_clone_relink", "clone": "x", "token": TOKEN, "confirm": "RELINK"})[0] == 422


def test_stop_clone(p5):
    fresh(p5)
    s, j, _ = call("POST", token="owner", body={"action": "owner_clone_stop", "clone": "3", "confirm": "STOP"})
    assert s == 200 and ("status", 3, "inactive") in p5.calls and "supervisor" in j["message"]
    dash._owner_hits.clear()
    assert call("POST", token="owner", body={"action": "owner_clone_stop", "clone": "3", "confirm": "STOP"})[0] == 409   # already stopped
    dash._owner_hits.clear()
    assert call("POST", token="owner", body={"action": "owner_clone_stop", "clone": "8", "confirm": "STOP"})[0] == 404
    assert call("POST", token="owner", body={"action": "owner_clone_stop", "clone": "abc", "confirm": "STOP"})[0] == 422


# ── database ──
def test_database_overview_and_cleanup(p5):
    _, j, _ = call("GET", {"action": "owner_database"}, token="owner")
    assert j["stale_payments"] == 2 and {"table": "payment_logs", "rows": 4, "approx": True} in j["counts"]
    fresh(p5)
    s, j, _ = call("POST", token="owner", body={"action": "owner_db_cleanup_stale", "confirm": "CLEANUP"})
    assert s == 200 and j["expired"] == 2 and p5.calls == [("cleanup",)]


def test_no_raw_sql_surface():
    from api import dash_owner_ops
    assert not any("sql" in k or "query" in k for k in dash_owner_ops.WRITES)
    assert call.__name__            # keep import used


def test_actions_do_not_collide():
    from api import dash
    assert {"owner_helpers", "owner_clones", "owner_database"} <= set(dash._owner_routes())
    assert {"owner_clone_stop", "owner_helper_remove", "owner_db_cleanup_stale"} <= set(dash._owner_writes())
