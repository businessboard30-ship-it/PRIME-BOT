"""Owner area, Phase 6: global search, alerts, growth series, and the static guarantees of owner.js."""
import importlib
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tests.unit.test_dash_api import env, call  # noqa: F401
from tests.unit.test_dash_owner import own  # noqa: F401

BIG = 1534574875274903562
NOW = datetime(2026, 10, 8, 15, 30, tzinfo=timezone.utc)
ROUTES = [{"action": "owner_search", "q": "prime"}, {"action": "owner_alerts"}, {"action": "owner_growth", "days": "7"}]


@pytest.fixture
def data(own, monkeypatch):
    ai = importlib.import_module("modules.admin_inspect")
    am = importlib.import_module("modules.admin_money")
    sf = importlib.import_module("modules.admin_safety")
    ins = importlib.import_module("modules.admin_insights")
    async def slist(query="", clone=None, active_only=True, page=0, per_page=25):
        slist.args = (query, active_only, per_page)
        return {"rows": [{"guild_id": BIG, "clone_id": None, "guild_name": "Prime HQ", "member_count": 9, "left_at": None, "owner_id": 1}], "total": 1}
    async def find(ref): return {"paystack_reference": ref, "status": "completed", "amount": 5, "payment_type": "premium", "user_id": BIG, "secret": "x"}
    async def beats(): return []
    async def fails(limit=25): return [{"id": 1}, {"id": 2}]
    async def clearable(hours=6): return 3
    async def rcount(): return {"new": 7}
    async def series(days, now=None): return [{"start": NOW, "joined": 3, "left": 1}, {"start": NOW, "joined": 0, "left": 2}]
    monkeypatch.setattr(ai, "server_list", slist); monkeypatch.setattr(ai, "clone_heartbeats", beats)
    monkeypatch.setattr(am, "find_payment", find); monkeypatch.setattr(am, "list_failures", fails)
    monkeypatch.setattr(am, "count_clearable_pending", clearable); monkeypatch.setattr(sf, "report_counts", rcount)
    monkeypatch.setattr(ins, "growth_series", series)
    own.slist = slist
    return own


@pytest.mark.parametrize("query", ROUTES)
def test_permission_matrix(data, query):
    assert call("GET", query, token=None)[0] == 401
    assert call("GET", query, token="nope")[0] == 401
    assert call("GET", query, token="sid")[0] == 403
    assert call("GET", query, token="stale")[0] == 401
    assert call("GET", query, token="owner")[0] == 200


def test_search_finds_servers_user_and_payment_without_leaking(data):
    _, p, _ = call("GET", {"action": "owner_search", "q": str(BIG)}, token="owner")
    assert p["user_id"] == str(BIG) and p["servers"][0]["guild_id"] == str(BIG) and p["servers"][0]["left"] is False
    assert data.slist.args == (str(BIG), False, 8)          # includes left servers, capped
    _, p, _ = call("GET", {"action": "owner_search", "q": "PAY_ref-1"}, token="owner")
    assert p["payment"]["paystack_reference"] == "PAY_ref-1" and "secret" not in p["payment"]


def test_search_validation(data):
    assert call("GET", {"action": "owner_search", "q": "a"}, token="owner")[0] == 422
    assert call("GET", {"action": "owner_search"}, token="owner")[0] == 422


def test_alerts_route_orders_bad_first_and_counts(data):
    _, p, _ = call("GET", {"action": "owner_alerts"}, token="owner")
    ids = [a["id"] for a in p["alerts"]]
    assert "pay-failures" in ids and "pay-stale" in ids and "reports-backlog" in ids
    assert p["alerts"][0]["level"] == "bad" and p["count"] == len(p["alerts"]) and p["bad"] >= 1


def test_growth_route_and_validation(data):
    _, p, _ = call("GET", {"action": "owner_growth", "days": "7"}, token="owner")
    assert p["net"] == 0 and p["series"][0]["joined"] == 3
    for bad in ("0", "91", "x", "-1"):
        assert call("GET", {"action": "owner_growth", "days": bad}, token="owner")[0] == 422


def test_build_alerts_respects_sections_and_thresholds():
    ins = importlib.import_module("modules.admin_insights")
    facts = {"quiet_clones": 2, "failures": [1], "stale_pending": 0, "reports": {"new": 4}}
    assert [a["id"] for a in ins.build_alerts(facts, 10, 180, {"money"})] == ["pay-failures"]
    assert ins.build_alerts(facts, 10, 180, set()) == []
    ids = [a["id"] for a in ins.build_alerts(facts, 600, 180, {"health", "money", "reports"})]
    assert ids[0] in ("worker-stale", "pay-failures") and "clones-quiet" in ids and "reports-backlog" not in ids   # 4 < backlog of 5
    assert ins.build_alerts({}, None, 180, {"health"})[0]["id"] == "worker-silent"
    assert ins.build_alerts({"failures": []}, 1, 180, {"money"}) == []


def test_bucket_days_oldest_first_ends_today():
    ins = importlib.import_module("modules.admin_insights")
    days = ins.bucket_days(3, NOW)
    assert [d.day for d in days] == [6, 7, 8] and days[-1].hour == 0


@pytest.mark.asyncio
async def test_growth_series_fills_gaps(monkeypatch):
    ins = importlib.import_module("modules.admin_insights")
    class Conn:
        calls = 0
        async def fetch(self, sql, *a):
            Conn.calls += 1
            d = datetime(2026, 10, 8)
            return [{"d": d, "n": 4}] if "joined_at >=" in sql else [{"d": datetime(2026, 10, 7), "n": 1}]
    class Acq:
        async def __aenter__(self): return Conn()
        async def __aexit__(self, *a): return False
    class Pool:
        def acquire(self): return Acq()
    async def pool(): return Pool()
    monkeypatch.setattr(ins, "_pool", pool)
    rows = await ins.growth_series(3, NOW)
    assert [(r["joined"], r["left"]) for r in rows] == [(0, 0), (0, 1), (4, 0)]


def test_no_schema_change_needed():
    src = Path("database.py").read_text()
    assert "owner_insights" not in src      # Phase 6 adds no table, so no SCHEMA_VERSION bump


JS = Path(__file__).resolve().parents[2] / "dashboard" / "assets" / "owner.js"


def test_owner_js_never_uses_innerhtml_or_storage():
    t = JS.read_text()
    assert "innerHTML" not in t and "insertAdjacentHTML" not in t and "outerHTML" not in t
    assert "sessionStorage" not in t and "eval(" not in t
    # the only storage touch is the existing "sign out everywhere" clearing the session key; Phase 6 adds none
    assert t.count("localStorage") == 1 and 'localStorage.removeItem("primebot.dash.session")' in t


def test_owner_js_polls_only_while_visible_and_clears_timers():
    t = JS.read_text()
    assert "document.hidden" in t and "function clearTimers" in t and t.count("clearTimers();") >= 1
