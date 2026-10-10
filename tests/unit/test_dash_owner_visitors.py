"""Owner-only Visitors metric: who used the dashboard today, trend, sign-ups, plans."""
import asyncio
import datetime as dt
from pathlib import Path

import pytest

import database as dbmod
from api import dash
from modules import admin_access
from tests.unit.test_dash_api import call, env  # noqa: F401
from tests.unit.test_dash_owner import own  # noqa: F401

TODAY = dt.date(2026, 10, 9)
NOW = dt.datetime(2026, 10, 9, 14, 0, tzinfo=dt.timezone.utc)


@pytest.fixture
def vis(own, monkeypatch):
    fake = own
    dash._visit_seen.clear()
    dash._visit_pruned[0] = None
    fake.touched, fake.pruned = [], 0

    async def touch(uid):
        fake.touched.append(uid)

    async def prune():
        fake.pruned += 1
        return 0

    async def report(days=14, limit=200):
        return {"today": TODAY,
                "visitors": [
                    {"user_id": "111", "first_at": NOW, "last_at": NOW, "touches": 5, "display_name": "Ama",
                     "new_today": True, "earlier_days": 0, "plans": []},
                    {"user_id": "222", "first_at": NOW, "last_at": NOW, "touches": 1, "display_name": None,
                     "new_today": False, "earlier_days": 3, "plans": ["dev_yearly", "dev_monthly", "card_plan"]}],
                "series": {TODAY: 2, TODAY - dt.timedelta(days=1): 9, TODAY - dt.timedelta(days=3): 4},
                "unique_7d": 12, "unique_30d": 30, "total_users": 80, "new_today": 1, "new_7d": 6, "paying": 5}
    fake.dash_visit_touch, fake.dash_visit_prune, fake.dash_visits_report = touch, prune, report
    return fake


def test_only_the_owner_can_read_it(vis):
    q = {"action": "owner_visitors"}
    assert call("GET", q, token=None)[0] == 401
    assert call("GET", q, token="nope")[0] == 401
    assert call("GET", q, token="sid")[0] == 403              # normal guild admin


def test_helpers_never_get_it_even_when_granted_other_sections(vis, monkeypatch):
    import time
    from modules import admin_controls
    vis.sessions["helper"] = {"kind": "dash", "iat": int(time.time()), "guilds": [], "user": {"id": "88", "username": "h"}}
    monkeypatch.setitem(admin_controls._helpers, "map", {88: {"access", "audit", "inspect"}})
    assert call("GET", {"action": "owner_visitors"}, token="helper")[0] == 403
    assert dash._owner_routes()["owner_visitors"][0] == "access"
    assert "access" in admin_access.OWNER_ONLY


def test_owner_gets_today_list_series_and_counts(vis):
    st, p, _ = call("GET", {"action": "owner_visitors", "days": "7"}, token="owner")
    assert st == 200 and p["timezone"] == "UTC" and p["as_of_day"] == "2026-10-09"
    assert p["today_count"] == 2 and p["yesterday"] == 9 and p["returning_today"] == 1
    assert [r["visitors"] for r in p["series"]] == [0, 0, 0, 4, 0, 9, 2] and len(p["series"]) == 7
    a, b = p["visitors"]
    assert (a["user_id"], a["name"], a["new_today"], a["returning"]) == ("111", "Ama", True, False)
    assert b["name"] is None and b["returning"] is True and b["plans"] == ["card_plan", "dev_monthly", "dev_yearly"]
    assert (p["unique_7d"], p["unique_30d"], p["total_users"], p["new_today"], p["new_7d"], p["paying"]) == (12, 30, 80, 1, 6, 5)


@pytest.mark.parametrize("days", ["0", "31", "abc", "-1"])
def test_bad_range_is_rejected(vis, days):
    assert call("GET", {"action": "owner_visitors", "days": days}, token="owner")[0] == 422


def test_dashboard_use_is_recorded_once_per_window_per_person(vis):
    call("GET", {"action": "me"}, token="sid")
    call("GET", {"action": "me"}, token="sid")
    call("GET", {"action": "me"}, token="sid")
    assert vis.touched == ["6"] and vis.pruned == 1
    call("GET", {"action": "me"}, token="owner")
    assert vis.touched == ["6", "77"] and vis.pruned == 1


def test_recording_a_visit_never_breaks_a_request(vis):
    async def boom(uid):
        raise RuntimeError("db down")
    vis.dash_visit_touch = boom
    assert call("GET", {"action": "me"}, token="sid")[0] == 200


def test_anonymous_calls_record_nothing(vis):
    call("GET", {"action": "me"}, token=None)
    call("GET", {"action": "me"}, token="nope")
    assert vis.touched == []


def test_visit_throttle_expires(vis, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(dash.time, "monotonic", lambda: clock[0])
    call("GET", {"action": "me"}, token="sid")
    clock[0] += dash.VISIT_THROTTLE_S - 1
    call("GET", {"action": "me"}, token="sid")
    clock[0] += 2
    call("GET", {"action": "me"}, token="sid")
    assert vis.touched == ["6", "6"]


# ── database layer, against a fake pool ──────────────────────────────────

class _Conn:
    def __init__(self):
        self.sql = []

    async def execute(self, q, *a):
        self.sql.append((q, a))
        return "DELETE 4"

    async def fetchval(self, q, *a):
        self.sql.append((q, a))
        return TODAY if "NOW() AT TIME ZONE 'UTC')::date" in q and "COUNT" not in q else 7

    async def fetch(self, q, *a):
        self.sql.append((q, a))
        if "FROM dash_visits v" in q:
            return [{"user_id": "5", "first_at": NOW, "last_at": NOW, "touches": 2, "display_name": "X",
                     "first_seen": NOW, "new_today": False, "earlier_days": 1}]
        if "user_entitlements" in q:
            return [{"user_id": "5", "product": "card_plan"}]
        return [{"day": TODAY, "n": 3}]


class _Pool:
    def __init__(self, c):
        self.c = c

    def acquire(self):
        c = self.c

        class _Cm:
            async def __aenter__(s):
                return c

            async def __aexit__(s, *a):
                return False
        return _Cm()


@pytest.fixture
def realdb(monkeypatch):
    conn = _Conn()

    async def pool():
        return _Pool(conn)
    monkeypatch.setattr(dbmod, "get_pool", pool)
    return dbmod.Database.__new__(dbmod.Database), conn


def test_touch_upserts_one_row_per_user_per_utc_day(realdb):
    d, conn = realdb
    asyncio.run(d.dash_visit_touch(123))
    q, a = conn.sql[0]
    assert "ON CONFLICT (day, user_id)" in q and "'UTC'" in q and a == ("123",)


def test_prune_keeps_90_days(realdb):
    d, conn = realdb
    assert asyncio.run(d.dash_visit_prune()) == 4
    assert conn.sql[0][1] == (90,)


def test_report_shape_and_active_plans_only(realdb):
    d, conn = realdb
    r = asyncio.run(d.dash_visits_report(14))
    assert r["today"] == TODAY and r["visitors"][0]["plans"] == ["card_plan"]
    assert r["series"] == {TODAY: 3} and r["unique_7d"] == 7
    plan_sql = [q for q, _ in conn.sql if "user_entitlements" in q and "ANY" in q][0]
    assert "status = 'active'" in plan_sql and "expires_at" in plan_sql


def test_report_clamps_days(realdb):
    d, conn = realdb
    asyncio.run(d.dash_visits_report(9999))
    assert any(a[-1] == 30 for q, a in conn.sql if "GROUP BY day" in q)


def test_delete_my_data_removes_visits_and_policy_is_documented():
    import inspect
    src = inspect.getsource(dbmod.Database.member_data_delete)
    assert "DELETE FROM dash_visits WHERE user_id = $1" in src
    assert "web_registry" in src


def test_front_end_page_is_owner_gated_and_safe():
    js = Path("dashboard/assets/owner.js").read_text()
    assert 'id: "visitors"' in js and 'need: "access"' in js and "owner_visitors" in js
    assert "innerHTML" not in js
