"""Owner area Phase 3 (database-only money actions): permissions, step-up, typed confirm, validation, audit."""
import importlib
import time
from datetime import datetime, timedelta, timezone

import pytest

from tests.unit.test_dash_api import env, call  # noqa: F401
from tests.unit.test_dash_owner import own, OWNER_ID  # noqa: F401

BIG = 1534574875274903562
NOW = datetime.now(timezone.utc)
ROW = {"payment_id": 42, "paystack_reference": "gum_premium_1_ab12", "user_id": BIG, "amount": 10, "status": "completed",
       "payment_type": "premium", "provider": "gumroad", "chat_id": BIG, "clone_id": None, "created_date": NOW,
       "api_secret": "MUST-NOT-LEAK"}


@pytest.fixture
def mon(own, monkeypatch):
    am = importlib.import_module("modules.admin_money")
    calls = []
    rows = {ROW["paystack_reference"]: dict(ROW)}
    async def find(ref): return rows.get(ref.strip())
    async def reverse(pid, by): calls.append(("reverse", pid, by)); return {"ok": True, "changed": True, "days": 30, "expires_at": NOW + timedelta(days=1)}
    async def create(code, pct, uses, days, by): calls.append(("create", code, pct, uses, days, by)); return code != "DUPE"
    async def toggle(code, active): calls.append(("toggle", code, active)); return True
    async def dismiss(fid, by): calls.append(("dismiss", fid, by)); return True
    async def clear(hours=6): calls.append(("clear",)); return 4
    async def lst(limit=20): return [{"code": "A1", "percent_off": 10, "max_uses": 5, "uses": 5, "expires_at": None, "active": True}]
    for k, v in dict(find_payment=find, reverse_payment=reverse, create_coupon=create, set_coupon_active=toggle,
                     dismiss_failure=dismiss, clear_old_pending=clear, list_coupons=lst).items():
        monkeypatch.setattr(am, k, v)
    own.calls, own.rows = calls, rows
    own.sessions["owner"]["fresh_until"] = 0
    return own


def fresh(m):
    m.sessions["owner"]["fresh_until"] = time.time() + 300


WRITES = [{"action": "owner_payment_reverse", "reference": ROW["paystack_reference"], "confirm": "REVERSE"},
          {"action": "owner_coupon_create", "code": "SAVE10", "percent": "10"},
          {"action": "owner_coupon_toggle", "code": "SAVE10", "active": False},
          {"action": "owner_failure_dismiss", "id": "3"},
          {"action": "owner_pending_clear", "confirm": "CLEAR"}]
READS = [{"action": "owner_payment", "reference": ROW["paystack_reference"]}, {"action": "owner_coupons"}]


@pytest.mark.parametrize("body", WRITES)
def test_write_permission_matrix(mon, body):
    assert call("POST", token=None, body=body)[0] == 401
    assert call("POST", token="sid", body=body)[0] == 403
    assert call("POST", token="stale", body=body)[0] == 401
    assert mon.calls == [] and mon.audit_rows == []


@pytest.mark.parametrize("query", READS)
def test_read_permission_matrix(mon, query):
    assert call("GET", query, token=None)[0] == 401
    assert call("GET", query, token="sid")[0] == 403
    assert call("GET", query, token="owner")[0] == 200


def test_payment_lookup_returns_only_safe_fields(mon):
    _, p, _ = call("GET", {"action": "owner_payment", "reference": ROW["paystack_reference"]}, token="owner")
    assert p["can_reverse"] is True and p["payment"]["user_id"] == str(BIG) and "api_secret" not in p["payment"]


@pytest.mark.parametrize("ref", ["", "a", "bad ref!", "x" * 200, "'; drop table payment_logs;--"])
def test_payment_lookup_rejects_bad_reference(mon, ref):
    assert call("GET", {"action": "owner_payment", "reference": ref}, token="owner")[0] == 422


def test_payment_lookup_not_found(mon):
    assert call("GET", {"action": "owner_payment", "reference": "gum_nothing_here"}, token="owner")[0] == 404


def test_reverse_needs_stepup_then_typed_confirm_then_runs(mon):
    body = {"action": "owner_payment_reverse", "reference": ROW["paystack_reference"]}
    assert call("POST", token="owner", body={**body, "confirm": "REVERSE"})[1]["code"] == "stepup_required"
    fresh(mon)
    assert call("POST", token="owner", body=body)[0] == 422
    assert call("POST", token="owner", body={**body, "confirm": "reverse"})[0] == 422
    assert mon.calls == []
    st, p, _ = call("POST", token="owner", body={**body, "confirm": "REVERSE"})
    assert st == 200 and p["changed"] is True and mon.calls == [("reverse", 42, OWNER_ID)]
    a = mon.audit_rows[-1]
    assert (a["section"], a["action"], a["target"], a["result"]) == ("money", "owner_payment_reverse", ROW["paystack_reference"], "ok")


@pytest.mark.parametrize("patch", [{"status": "reversed"}, {"status": "pending"}, {"payment_type": "ad"}])
def test_reverse_refuses_non_reversible(mon, patch):
    mon.rows[ROW["paystack_reference"]].update(patch)
    fresh(mon)
    st, _, _ = call("POST", token="owner", body={"action": "owner_payment_reverse", "reference": ROW["paystack_reference"], "confirm": "REVERSE"})
    assert st == 422 and mon.calls == []


def test_reverse_unknown_reference_and_audit_failure(mon):
    fresh(mon)
    assert call("POST", token="owner", body={"action": "owner_payment_reverse", "reference": "gum_nothing_here", "confirm": "REVERSE"})[0] == 422
    mon.fail_audit = True
    assert call("POST", token="owner", body={"action": "owner_payment_reverse", "reference": ROW["paystack_reference"], "confirm": "REVERSE"})[0] == 503
    assert mon.calls == []                                   # fail closed: money did not move


def test_coupon_create_small_needs_no_stepup(mon):
    st, p, _ = call("POST", token="owner", body={"action": "owner_coupon_create", "code": "save10", "percent": "10", "max_uses": "50", "days": "30"})
    assert st == 200 and p["created"] is True and mon.calls == [("create", "SAVE10", 10, 50, 30, OWNER_ID)]


def test_coupon_create_deep_discount_needs_stepup_and_confirm(mon):
    body = {"action": "owner_coupon_create", "code": "HALF", "percent": "50"}
    assert call("POST", token="owner", body={**body, "confirm": "CREATE"})[1]["code"] == "stepup_required"
    fresh(mon)
    assert call("POST", token="owner", body=body)[0] == 422
    assert call("POST", token="owner", body={**body, "confirm": "CREATE"})[0] == 200


def test_coupon_duplicate_reports_not_created(mon):
    assert call("POST", token="owner", body={"action": "owner_coupon_create", "code": "DUPE", "percent": "5"})[1]["created"] is False


@pytest.mark.parametrize("bad", [{"code": "ab"}, {"code": "has space"}, {"code": "X" * 25}, {"percent": "0"}, {"percent": "101"}, {"percent": "x"},
                                  {"max_uses": "0"}, {"max_uses": "-1"}, {"max_uses": "abc"}, {"days": "3651"}, {"days": "0"}])
def test_coupon_validation(mon, bad):
    assert call("POST", token="owner", body={"action": "owner_coupon_create", "code": "OKCODE", "percent": "10", **bad})[0] == 422
    assert mon.calls == []


def test_coupon_toggle_and_validation(mon):
    assert call("POST", token="owner", body={"action": "owner_coupon_toggle", "code": "SAVE10", "active": False})[0] == 200
    assert mon.calls == [("toggle", "SAVE10", False)]
    for bad in ({"active": "no"}, {"code": "x"}, {}):
        assert call("POST", token="owner", body={"action": "owner_coupon_toggle", **bad})[0] == 422


def test_coupons_list_has_state(mon):
    _, p, _ = call("GET", {"action": "owner_coupons"}, token="owner")
    assert p["rows"][0]["state"] == "used up"


def test_failure_dismiss_validation(mon):
    assert call("POST", token="owner", body={"action": "owner_failure_dismiss", "id": "7"})[0] == 200
    assert call("POST", token="owner", body={"action": "owner_failure_dismiss", "id": "x"})[0] == 422
    assert call("POST", token="owner", body={"action": "owner_failure_dismiss", "id": "9" * 30})[0] == 422


def test_pending_clear_needs_stepup_and_confirm(mon):
    assert call("POST", token="owner", body={"action": "owner_pending_clear", "confirm": "CLEAR"})[1]["code"] == "stepup_required"
    fresh(mon)
    assert call("POST", token="owner", body={"action": "owner_pending_clear"})[0] == 422
    st, p, _ = call("POST", token="owner", body={"action": "owner_pending_clear", "confirm": "CLEAR"})
    assert st == 200 and p["expired"] == 4 and mon.calls == [("clear",)]


def test_money_section_not_open_to_non_owner_even_with_helper_grant(mon, monkeypatch):
    mon.sessions["helper"] = {"kind": "dash", "iat": int(time.time()), "guilds": [], "user": {"id": "88", "username": "h"}}
    ac = importlib.import_module("modules.admin_controls")
    monkeypatch.setitem(ac._helpers, "map", {88: {"premium", "audit"}})
    assert call("POST", token="helper", body=WRITES[0])[0] == 403
