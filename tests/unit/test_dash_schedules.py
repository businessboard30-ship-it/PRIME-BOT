from datetime import datetime, timedelta, timezone

import pytest

from api import dash
from utils import dash_schema as S
from tests.unit.test_dash_api import env, call, GUILD  # noqa: F401

NOW = datetime(2026, 10, 8, 10, 0, tzinfo=timezone.utc)
CH = {"100"}


def ok(**kw):
    raw = {"channel_id": "100", "content": "hello", "mode": "once", "minutes": 30}
    raw.update(kw)
    return S.validate_schedule(raw, CH, NOW)


def test_once_interval_daily():
    c, e = ok()
    assert e is None and c["run_at"] == NOW + timedelta(minutes=30) and c["interval_seconds"] is None
    c, e = ok(mode="interval", minutes=60)
    assert e is None and c["interval_seconds"] == 3600
    c, e = ok(mode="daily", time_utc="09:00")                  # already passed today -> tomorrow
    assert e is None and c["run_at"] == datetime(2026, 10, 9, 9, 0, tzinfo=timezone.utc) and c["interval_seconds"] == 86400
    c, e = ok(mode="daily", time_utc="23:30")
    assert c["run_at"] == datetime(2026, 10, 8, 23, 30, tzinfo=timezone.utc)


@pytest.mark.parametrize("kw", [
    {"channel_id": "999"}, {"channel_id": "x"}, {"content": "   "}, {"content": "a" * 2001}, {"mode": "weekly"},
    {"minutes": 0}, {"minutes": "abc"}, {"minutes": True}, {"mode": "interval", "minutes": 4},
    {"mode": "daily", "time_utc": "24:00"}, {"mode": "daily", "time_utc": "9"}])
def test_rejects_bad_input(kw):
    assert ok(**kw)[1]


class SchedDB:
    def __init__(self, base):
        self.base, self.rows, self.audit, self.next = base, [], [], 1
    def __getattr__(self, n):
        return getattr(self.base, n)
    async def list_scheduled_messages(self, gid, cid=None):
        return list(self.rows)
    async def create_scheduled_message(self, gid, channel_id, content, run_at, interval, by, clone_id=None):
        row = {"id": self.next, "guild_id": gid, "channel_id": channel_id, "content": content, "next_run_at": run_at,
               "interval_seconds": interval, "created_by": by, "enabled": True}
        self.next += 1; self.rows.append(row); return row
    async def delete_scheduled_message(self, gid, mid, clone_id=None):
        n = len(self.rows); self.rows = [r for r in self.rows if r["id"] != mid]; return len(self.rows) < n
    async def dash_audit_add(self, *a, **k):
        self.audit.append(a)


@pytest.fixture
def sdb(env, monkeypatch):
    fake, _ = env
    db = SchedDB(fake)
    monkeypatch.setattr(dash, "db", db)
    return db


def add(**kw):
    body = {"action": "schedule_add", "guild_id": str(GUILD), "channel_id": "100", "content": "hi", "mode": "once", "minutes": 10}
    body.update(kw)
    return call("POST", body=body)


def test_add_list_delete_roundtrip(sdb):
    st, p, _ = add()
    assert st == 200 and sdb.rows[0]["created_by"] == 6 and sdb.audit
    st, p, _ = call("GET", {"action": "schedules", "guild_id": str(GUILD)})
    assert st == 200 and len(p["schedules"]) == 1 and p["schedules"][0]["content"] == "hi"
    assert call("POST", body={"action": "schedule_delete", "guild_id": str(GUILD), "id": "1"})[0] == 200
    assert call("POST", body={"action": "schedule_delete", "guild_id": str(GUILD), "id": "1"})[0] == 404
    assert call("POST", body={"action": "schedule_delete", "guild_id": str(GUILD), "id": "q"})[0] == 400


def test_channel_must_belong_to_server_and_auth_enforced(sdb):
    assert add(channel_id="555")[0] == 422
    assert call("POST", body={"action": "schedule_add", "guild_id": "999"})[0] == 403
    assert call("POST", body={"action": "schedule_add", "guild_id": str(GUILD)}, token=None)[0] == 401
    assert not sdb.rows


def test_disabled_jobs_are_hidden_and_cap_enforced(sdb):
    sdb.rows.append({"id": 90, "guild_id": GUILD, "channel_id": 100, "content": "done", "next_run_at": NOW,
                     "interval_seconds": None, "created_by": 6, "enabled": False})
    st, p, _ = call("GET", {"action": "schedules", "guild_id": str(GUILD)})
    assert p["schedules"] == []
    for _ in range(S.SCHEDULE_MAX_ACTIVE):
        assert add()[0] == 200
    assert add()[0] == 422
