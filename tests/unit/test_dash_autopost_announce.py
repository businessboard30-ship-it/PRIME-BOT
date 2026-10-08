"""A3: Auto-post settings page (generic module + DB adapters) and Announcements (extends the Schedules page)."""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from api import dash
from database import Database
from utils import dash_schema as S
from tests.unit.test_dash_api import env, call, GUILD  # noqa: F401

NOW = datetime(2026, 10, 8, 10, 0, tzinfo=timezone.utc)
CH = {"text": {"100", "101"}, "category": {"200"}, "voice": {"300"}}


def run(coro): return asyncio.run(coro)


# ---------- auto-post module ----------
def test_autopost_module_registered_and_validated():
    m = S.BY_ID["autopost"]
    assert m["category"] in S.CATEGORIES and callable(getattr(Database, m["get"])) and callable(getattr(Database, m["set"]))
    ok = lambda v: S.validate_values(m, v, CH, set(), False)
    clean, err = ok({"enabled": True, "channel_id": "101", "interval_hours": "12"})
    assert not err and clean == {"enabled": True, "channel_id": 101, "interval_hours": 12}
    for bad in ({"interval_hours": 0}, {"interval_hours": 721}, {"interval_hours": "x"}, {"channel_id": "999"},
                {"enabled": "yes"}, {"bogus": 1}):
        assert ok(bad)[1]


class FakeSelf:
    def __init__(self, row=None, content=True):
        self.row, self.content, self.calls = row, content, []
    async def get_discord_autopost(self, g, c): return self.row
    async def list_discord_autopost_content(self, active_only=True): return [{"id": 1}] if self.content else []
    async def set_discord_autopost(self, g, c, ch, hours, by): self.calls.append(("set", g, c, ch, hours, by))
    async def disable_discord_autopost(self, g, c): self.calls.append(("disable", g, c))


def test_autopost_get_defaults():
    assert run(Database.get_autopost_settings_config(FakeSelf(None), 5, None)) == {"enabled": False, "channel_id": None, "interval_hours": 24}
    out = run(Database.get_autopost_settings_config(FakeSelf({"enabled": True, "channel_id": 9, "interval_hours": 6}), 5, None))
    assert out == {"enabled": True, "channel_id": 9, "interval_hours": 6}


def test_autopost_set_enable_keeps_configured_by_and_disable_turns_off():
    f = FakeSelf({"enabled": False, "channel_id": 9, "interval_hours": 6, "configured_by": 42})
    run(Database.set_autopost_settings_config(f, 5, 7, enabled=True, interval_hours=12))
    assert f.calls == [("set", 5, 7, 9, 12, 42)]
    f = FakeSelf({"enabled": True, "channel_id": 9, "interval_hours": 6, "configured_by": 42})
    run(Database.set_autopost_settings_config(f, 5, None, enabled=False))
    assert f.calls == [("set", 5, None, 9, 6, 42), ("disable", 5, None)]


def test_autopost_set_rules():
    with pytest.raises(S.ValidationError):                                    # on without a channel
        run(Database.set_autopost_settings_config(FakeSelf(None), 5, None, enabled=True))
    with pytest.raises(S.ValidationError):                                    # channel cleared while on
        run(Database.set_autopost_settings_config(FakeSelf({"enabled": True, "channel_id": 9}), 5, None, channel_id=None))
    with pytest.raises(S.ValidationError):                                    # nothing to rotate through
        run(Database.set_autopost_settings_config(FakeSelf(None, content=False), 5, None, enabled=True, channel_id=9))
    f = FakeSelf(None)                                                        # off + no channel = nothing to write
    run(Database.set_autopost_settings_config(f, 5, None, enabled=False))
    assert f.calls == []
    f = FakeSelf(None)                                                        # first setup, new row
    run(Database.set_autopost_settings_config(f, 5, None, enabled=True, channel_id=9, interval_hours=24))
    assert f.calls == [("set", 5, None, 9, 24, 0)]


def test_autopost_save_route_returns_422_for_database_rule(env, monkeypatch):
    fake, _ = env
    async def get_cfg(g, c): return {"enabled": False, "channel_id": None, "interval_hours": 24}
    async def set_cfg(g, c, **kw): raise S.ValidationError("Pick a channel before turning auto-post on.")
    monkeypatch.setattr(fake, "get_autopost_settings_config", get_cfg, raising=False)
    monkeypatch.setattr(fake, "set_autopost_settings_config", set_cfg, raising=False)
    st, p, _ = call("POST", body={"action": "save", "guild_id": str(GUILD), "module": "autopost", "values": {"enabled": True}})
    assert st == 422 and "channel" in p["message"]


# ---------- announcements ----------
def ann(**kw):
    raw = {"channel_id": "100", "content": "hello", "mode": "once", "minutes": 30}
    raw.update(kw)
    return S.validate_announcement(raw, {"100"}, NOW)


def test_announcement_validation():
    c, e = ann()
    assert e is None and c["interval_minutes"] is None and c["run_at"] == NOW + timedelta(minutes=30) and c["message"] == "hello"
    c, e = ann(mode="interval", minutes=90)
    assert e is None and c["interval_minutes"] == 90
    for kw in ({"mode": "daily", "time_utc": "09:00"}, {"channel_id": "5"}, {"content": " "}, {"mode": "interval", "minutes": 4}):
        assert ann(**kw)[1]


class AnnDB:
    def __init__(self, base):
        self.base, self.rows, self.audit, self.next = base, [], [], 1
    def __getattr__(self, n): return getattr(self.base, n)
    async def get_scheduled_announcements(self, gid, clone_id=None): return [r for r in self.rows if r["active"]]
    async def add_scheduled_announcement(self, gid, channel_id, message, next_run_at, created_by, interval_minutes=None, clone_id=None):
        self.rows.append({"id": self.next, "guild_id": gid, "channel_id": channel_id, "message": message, "next_run_at": next_run_at,
                          "interval_minutes": interval_minutes, "active": True, "created_by": created_by})
        self.next += 1
        return self.next - 1
    async def remove_scheduled_announcement(self, gid, aid, clone_id=None):
        hit = [r for r in self.rows if r["id"] == aid and r["active"]]
        for r in hit: r["active"] = False
        return bool(hit)
    async def dash_audit_add(self, *a, **k): self.audit.append(a)


@pytest.fixture
def adb(env, monkeypatch):
    fake, _ = env
    d = AnnDB(fake)
    monkeypatch.setattr(dash, "db", d)
    return d


def add(**kw):
    body = {"action": "announcement_add", "guild_id": str(GUILD), "channel_id": "100", "content": "hi", "mode": "once", "minutes": 10}
    body.update(kw)
    return call("POST", body=body)


def test_announcement_roundtrip_audited(adb):
    st, p, _ = add(mode="interval", minutes=60)
    assert st == 200 and adb.rows[0]["interval_minutes"] == 60 and adb.rows[0]["created_by"] == 6 and adb.audit
    assert p["schedule"]["interval_seconds"] == 3600 and p["schedule"]["id"] == "1"
    st, p, _ = call("GET", {"action": "announcements", "guild_id": str(GUILD)})
    assert st == 200 and len(p["schedules"]) == 1 and p["schedules"][0]["content"] == "hi" and p["limit"] == S.ANNOUNCEMENT_MAX_ACTIVE
    d = lambda i: call("POST", body={"action": "announcement_delete", "guild_id": str(GUILD), "id": i})[0]
    assert d("1") == 200 and d("1") == 404 and d("q") == 400
    assert len(adb.audit) == 2


def test_announcement_auth_channel_cap_and_rate(adb):
    assert add(channel_id="555")[0] == 422 and add(mode="daily", time_utc="09:00")[0] == 422
    assert call("POST", body={"action": "announcement_add", "guild_id": "999"})[0] == 403
    assert call("POST", body={"action": "announcement_add", "guild_id": str(GUILD)}, token=None)[0] == 401
    assert call("GET", {"action": "announcements", "guild_id": "999"})[0] == 403
    assert not adb.rows
    dash._owner_hits.clear()
    for _ in range(S.ANNOUNCEMENT_MAX_ACTIVE):
        assert add()[0] == 200
    assert add()[0] == 422


def test_announcement_rate_limit(adb):
    dash._owner_hits.clear()
    codes = [add(content=f"m{i}")[0] for i in range(32)]
    assert 429 in codes
    dash._owner_hits.clear()
