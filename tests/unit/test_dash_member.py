"""Member dashboard B0: entitlement rules, member route isolation, web-user registry, schema tables."""
import asyncio
import importlib
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from api import dash
from modules import entitlements as ent
from tests.unit.test_dash_api import env, call  # noqa: F401

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)
D = timedelta(days=1)


def row(product="card_plan", status="active", exp=NOW + 5 * D, cancel=False):
    return {"product": product, "status": status, "expires_at": exp, "cancel_at_period_end": cancel}


# ---------- pure entitlement rules ----------
def test_active_needs_future_expiry():
    assert ent.effective(row(), NOW) == {"access": True, "state": "active"}
    assert ent.effective(row(exp=NOW - D), NOW)["access"] is False
    assert ent.effective(row(exp=None), NOW)["access"] is False      # bad row fails closed


def test_cancelled_keeps_access_until_period_end_only():
    assert ent.effective(row(status="cancelled"), NOW) == {"access": True, "state": "cancelled"}
    assert ent.effective(row(status="cancelled", exp=NOW - D), NOW)["access"] is False


def test_past_due_has_three_day_grace():
    assert ent.effective(row(status="past_due", exp=NOW - 2 * D), NOW) == {"access": True, "state": "past_due"}
    assert ent.effective(row(status="past_due", exp=NOW - 4 * D), NOW) == {"access": False, "state": "expired"}


def test_expired_unknown_and_missing_never_grant():
    assert ent.effective(row(status="expired"), NOW)["access"] is False
    assert ent.effective(row(status="banana"), NOW)["access"] is False
    assert ent.effective({}, NOW)["access"] is False
    assert ent.has_access([], ent.DEV_PRODUCTS, NOW) is False


def test_naive_datetimes_are_treated_as_utc():
    assert ent.effective(row(exp=(NOW + D).replace(tzinfo=None)), NOW)["access"] is True


def test_products_do_not_cross_unlock():
    rows = [row("card_plan")]
    assert ent.has_access(rows, ent.DEV_PRODUCTS, NOW) is False       # card plan never unlocks Developer mode
    assert ent.summary(rows, NOW)["dev_unlocked"] is False and ent.summary(rows, NOW)["card_plan"] is True
    assert ent.summary([row("dev_yearly")], NOW)["dev_unlocked"] is True


def test_export_grace_is_seven_days_after_developer_plan_ends():
    ended = [row("dev_monthly", "expired", NOW - 6 * D)]
    assert ent.export_allowed(ended, NOW) is True
    assert ent.export_allowed([row("dev_monthly", "expired", NOW - 8 * D)], NOW) is False
    assert ent.export_allowed([row("card_plan", "expired", NOW - D)], NOW) is False


def test_summary_is_whitelisted_and_ignores_unknown_products():
    rows = [dict(row(), secret="x", subscription_id="sub_1"), row("mystery")]
    out = ent.summary(rows, NOW)
    assert [e["product"] for e in out["entitlements"]] == ["card_plan"]
    assert set(out["entitlements"][0]) == {"product", "state", "access", "expires_at", "cancel_at_period_end"}


# ---------- member route ----------
@pytest.fixture
def mem(env, monkeypatch):
    fake, _ = env
    fake.sessions["sid"]["iat"] = 1
    fake.sessions["other"] = {"kind": "dash", "user": {"id": "7777777777", "username": "o", "avatar_url": ""}, "guilds": []}
    data = {"6": [row("card_plan")], "7777777777": [row("dev_monthly")]}
    seen = []

    async def entitlements_list(uid):
        seen.append(uid)
        return list(data.get(uid, []))
    monkeypatch.setattr(fake, "entitlements_list", entitlements_list, raising=False)
    dash._owner_hits.clear()
    return fake, seen


def test_member_status_requires_a_session():
    dash._owner_hits.clear()
    status, payload, _ = call("GET", {"action": "member_status"}, token=None)
    assert status == 401


def test_member_status_is_keyed_to_the_session_user_not_the_client(mem):
    fake, seen = mem
    status, payload, _ = call("GET", {"action": "member_status", "user_id": "7777777777", "uid": "7777777777"})
    assert status == 200 and seen == ["6"]                       # session id 6, client-supplied ids ignored
    assert payload["card_plan"] is True and payload["dev_unlocked"] is False
    status, payload, _ = call("GET", {"action": "member_status"}, token="other")
    assert seen[-1] == "7777777777" and payload["dev_unlocked"] is True and payload["card_plan"] is False


def test_member_status_does_not_leak_internal_fields(mem):
    _, _ = mem
    status, payload, _ = call("GET", {"action": "member_status"})
    assert "subscription_id" not in str(payload) and "source" not in str(payload)


def test_member_status_is_rate_limited(mem):
    for _ in range(60):
        assert call("GET", {"action": "member_status"})[0] == 200
    assert call("GET", {"action": "member_status"})[0] == 429


def test_me_reports_member_true(mem, monkeypatch):
    fake, _ = mem

    async def present():
        return set()
    monkeypatch.setattr(dash, "_bot_guild_ids", present)

    async def clones(ids):
        return {}
    monkeypatch.setattr(dash, "_clone_presence", clones)

    async def dropbox_list(uid):
        return []
    monkeypatch.setattr(fake, "dropbox_list", dropbox_list, raising=False)
    status, payload, _ = call("GET", {"action": "me"})
    assert status == 200 and payload["member"] is True


def test_no_member_route_accepts_a_user_id():
    from api import dash_member
    import inspect
    for name, fn in dash_member.ROUTES.items():
        params = list(inspect.signature(fn).parameters)
        assert params[:2] == ["uid", "q"], name
    src = Path(dash_member.__file__).read_text()
    assert not re.search(r'q\(\s*["\'](user_id|uid|member)', src)


def test_member_actions_do_not_collide_with_owner_actions():
    assert not (set(dash._member_routes()) & set(dash._owner_routes()) | set(dash._member_routes()) & set(dash._owner_writes()))


# ---------- sign-in registry + schema ----------
def test_signin_registers_the_web_user_and_survives_a_registry_failure():
    src = Path(dash.__file__).read_text()
    i = src.index("dash_web_user_touch")
    assert "except Exception" in src[i:i + 200] and "_back(\"session=\"" in src[i:i + 300]
    j = src.index("stepup_sid:")                       # the step-up branch returns before the registry write
    assert j < i


def test_schema_has_both_tables_and_entitlement_writer_is_not_a_route():
    db_src = Path(importlib.import_module("database").__file__).read_text()
    assert "CREATE TABLE IF NOT EXISTS user_entitlements" in db_src
    assert "CREATE TABLE IF NOT EXISTS dash_web_users" in db_src
    assert 'SCHEMA_VERSION = "70"' in db_src
    assert "entitlement_upsert" not in Path(dash.__file__).read_text()
    assert "entitlement_upsert" not in (Path(dash.__file__).parent / "dash_member.py").read_text()
