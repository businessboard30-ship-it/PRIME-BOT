"""Owner area, Phase 1: read-only pages. Permission matrix, id/number safety, input validation."""
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from api import dash_owner
import importlib
from tests.unit.test_dash_api import env, call  # noqa: F401
from tests.unit.test_dash_owner import own  # noqa: F401  (owner session fixtures)

BIG = 1534574875274903562          # > 2**53: must reach the browser as a string
NOW = datetime(2026, 10, 8, tzinfo=timezone.utc)

READ_ROUTES = [
    {"action": "owner_health"},
    {"action": "owner_servers"},
    {"action": "owner_server", "guild_id": str(BIG)},
    {"action": "owner_user", "user_id": str(BIG)},
    {"action": "owner_payments", "view": "pending"},
    {"action": "owner_expiries", "days": "7"},
]


@pytest.fixture
def data(own, monkeypatch):
    # Other test modules drop/re-import modules.admin_*; resolve them now so we patch the live ones.
    ai = importlib.import_module("modules.admin_inspect")
    am = importlib.import_module("modules.admin_money")
    async def ping(): return 3.21
    async def counts(): return {"main": 5, "clones": 2}
    async def beats(): return [{"clone_id": 9, "bot_username": "c", "last_heartbeat": None}]
    async def slist(query="", clone=None, active_only=True, page=0, per_page=25):
        slist.args = (query, clone, active_only, page)
        return {"rows": [{"guild_id": BIG, "clone_id": None, "guild_name": "G", "member_count": 10, "joined_at": NOW}], "total": 1}
    async def srows(gid): return [{"guild_id": gid, "guild_name": "G"}] if gid == BIG else []
    async def sextra(gid): return {"blocked": None, "premium": [], "reports": {}}
    async def ucard(uid): return {"user_id": uid, "payments": [{"status": "completed", "n": 1, "total": Decimal("4.50")}]}
    async def pending(limit=25): return {"rows": [{"payment_id": 1, "user_id": BIG, "amount": Decimal("10.00"), "created_date": NOW}], "total": 1}
    async def failures(limit=25): return []
    async def rev(period, n, now=None): rev.args = (period, n); return {"GHS": [], "USD": []}
    async def expiries(days, limit=40): return []
    for mod, pairs in ((ai, dict(db_ping=ping, guild_counts=counts, clone_heartbeats=beats, server_list=slist,
                                 server_rows=srows, server_extras=sextra, user_card=ucard)),
                       (am, dict(list_pending=pending, list_failures=failures, revenue_series=rev, upcoming_expiries=expiries))):
        for k, v in pairs.items():
            monkeypatch.setattr(mod, k, v)
    own.slist, own.rev = slist, rev
    return own


@pytest.mark.parametrize("query", READ_ROUTES)
def test_read_routes_permission_matrix(data, query):
    assert call("GET", query, token=None)[0] == 401
    assert call("GET", query, token="nope")[0] == 401
    assert call("GET", query, token="sid")[0] == 403          # normal guild admin
    assert call("GET", query, token="stale")[0] == 401        # owner, but session too old
    assert call("GET", query, token="owner")[0] == 200


def test_ids_reach_the_browser_as_strings(data):
    _, p, _ = call("GET", {"action": "owner_servers"}, token="owner")
    row = p["rows"][0]
    assert row["guild_id"] == str(BIG) and isinstance(row["guild_id"], str)
    assert row["joined_at"].startswith("2026-10-08") and row["member_count"] == 10
    _, p, _ = call("GET", {"action": "owner_payments", "view": "pending"}, token="owner")
    assert p["rows"][0]["user_id"] == str(BIG) and p["rows"][0]["amount"] == 10.0
    _, p, _ = call("GET", {"action": "owner_user", "user_id": str(BIG)}, token="owner")
    assert p["user_id"] == str(BIG) and p["payments"][0]["total"] == 4.5


def test_health_reports_db_facts_and_no_fake_live_stats(data):
    _, p, _ = call("GET", {"action": "owner_health"}, token="owner")
    assert p["db_ping_ms"] == 3.2 and p["servers"] == {"main": 5, "clones": 2}
    assert p["live_bot"] is None and p["clones_quiet"][0]["clone_id"] == "9"


def test_input_validation(data):
    g = lambda **k: call("GET", dict({"action": "owner_servers"}, **k), token="owner")[0]
    assert g(page="-1") == 422 and g(page="abc") == 422 and g(page="999999") == 422
    assert g(clone="x'; DROP") == 422
    assert g(clone="main", page="2") == 200
    assert data.slist.args == ("", "main", True, 2)
    assert call("GET", {"action": "owner_server", "guild_id": "nope"}, token="owner")[0] == 422
    assert call("GET", {"action": "owner_server", "guild_id": "123456789012345678"}, token="owner")[0] == 404
    assert call("GET", {"action": "owner_user", "user_id": "-3"}, token="owner")[0] == 422
    assert call("GET", {"action": "owner_payments", "view": "evil"}, token="owner")[0] == 422
    assert call("GET", {"action": "owner_expiries", "days": "x"}, token="owner")[0] == 422


def test_revenue_range_is_clamped(data):
    call("GET", {"action": "owner_payments", "view": "revenue", "period": "day", "n": "99999"}, token="owner")
    assert data.rev.args == ("day", 90)
    call("GET", {"action": "owner_payments", "view": "revenue", "period": "week", "n": "0"}, token="owner")
    assert data.rev.args == ("week", 1)
    assert call("GET", {"action": "owner_payments", "view": "revenue", "period": "year"}, token="owner")[0] == 422


def test_reads_are_rate_limited(data):
    codes = [call("GET", {"action": "owner_health"}, token="owner")[0] for _ in range(61)]
    assert codes[:60] == [200] * 60 and codes[60] == 429


def test_read_routes_never_write(data):
    # a POST to a read action is not routed to the owner handlers
    assert call("POST", token="owner", body={"action": "owner_servers"})[0] != 200


def test_jsonable_primitives():
    assert dash_owner.jsonable({"a": {"owner_id": 7, "n": 7, "big": 2 ** 60}}) == {"a": {"owner_id": "7", "n": 7, "big": str(2 ** 60)}}
    assert dash_owner.jsonable({"ok": True, "flag_id": True}) == {"ok": True, "flag_id": True}
