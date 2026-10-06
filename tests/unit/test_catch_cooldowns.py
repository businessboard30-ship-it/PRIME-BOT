from datetime import datetime, timedelta, timezone

import pytest

from modules import catch_cooldowns


def test_remaining_and_format_remaining():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert catch_cooldowns.remaining_seconds(now - timedelta(seconds=1), now) == 0
    assert catch_cooldowns.remaining_seconds(now + timedelta(seconds=1.1), now) == 2
    assert catch_cooldowns.format_remaining(0) == "ready"
    assert catch_cooldowns.format_remaining(7) == "7s"
    assert catch_cooldowns.format_remaining(130) == "2m 10s"
    assert catch_cooldowns.format_remaining(3661) == "1h 1m"


def test_duration_defaults_and_validation():
    assert catch_cooldowns._duration("throw", None) == 3
    assert catch_cooldowns._duration("custom", 4.9) == 4
    with pytest.raises(KeyError):
        catch_cooldowns._duration("unknown", None)


class _FakeConnection:
    def __init__(self):
        self.calls = []

    async def fetchval(self, query, *args):
        self.calls.append((query, args))


@pytest.mark.asyncio
async def test_try_start_uses_atomic_upsert(monkeypatch):
    conn = _FakeConnection()

    class _Transaction:
        async def __aenter__(self):
            return conn

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(catch_cooldowns.catch_db, "transaction", lambda provided=None: _Transaction())
    started, ready_at = await catch_cooldowns.try_start(10, None, "throw", seconds=3)
    assert started is False
    assert ready_at is None
    assert len(conn.calls) == 2
    assert "ON CONFLICT" in conn.calls[0][0]
    assert "WHERE catch_cooldowns.ready_at <= now()" in conn.calls[0][0]
