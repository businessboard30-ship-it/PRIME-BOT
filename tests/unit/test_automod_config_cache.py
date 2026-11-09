"""get_automod_config: single-flight loads, stale-while-revalidate, race-free invalidation, bounded cache, DB-failure fallback."""
import asyncio
import time

import pytest

import database
from database import Database


class _Conn:
    def __init__(self, pool):
        self.pool = pool

    async def fetchrow(self, sql, guild_id, clone_id):
        self.pool.calls += 1
        snapshot = self.pool.rows.get((guild_id, clone_id))      # value as of when the query ran
        await self.pool.gate.wait()
        if self.pool.fail:
            raise ConnectionError("db down")
        return None if snapshot is None else dict(snapshot)


class _Acq:
    def __init__(self, pool):
        self.pool = pool

    async def __aenter__(self):
        return _Conn(self.pool)

    async def __aexit__(self, *a):
        return False


class _Pool:
    def __init__(self):
        self.calls = 0
        self.rows = {}
        self.fail = False
        self.gate = asyncio.Event()
        self.gate.set()

    def acquire(self):
        return _Acq(self)


@pytest.fixture
def env(monkeypatch):
    pool = _Pool()

    async def gp():
        return pool
    monkeypatch.setattr(database, "get_pool", gp)
    d = Database()
    d._automod_config_cache.clear()
    d._automod_flights.clear()
    d._automod_stats.clear()
    return d, pool


def _row(gid, clone=None, **kw):
    return {"guild_id": gid, "clone_id": clone, "banned_words": [], "anti_invite_enabled": False, **kw}


def test_concurrent_misses_share_one_db_load(env):
    d, pool = env

    async def go():
        pool.rows[(1, None)] = _row(1)
        res = await asyncio.gather(*[d.get_automod_config(1) for _ in range(50)])
        assert pool.calls == 1 and all(r["guild_id"] == 1 for r in res)
        await d.get_automod_config(1)
        assert pool.calls == 1                                    # now a pure cache hit
    asyncio.run(go())


def test_missing_row_returns_defaults_and_is_cached(env):
    d, pool = env

    async def go():
        c = await d.get_automod_config(5, clone_id=3)
        assert c["guild_id"] == 5 and c["clone_id"] == 3 and c["anti_invite_enabled"] is False
        await d.get_automod_config(5, clone_id=3)
        assert pool.calls == 1
    asyncio.run(go())


def test_returned_config_is_a_copy(env):
    d, pool = env

    async def go():
        pool.rows[(1, None)] = _row(1, banned_words=["x"])
        a = await d.get_automod_config(1)
        a["banned_words"].append("evil")
        a["anti_invite_enabled"] = True
        b = await d.get_automod_config(1)
        assert b["banned_words"] == ["x"] and b["anti_invite_enabled"] is False
    asyncio.run(go())


def test_expired_entry_is_served_stale_and_refreshed_in_background(env):
    d, pool = env

    async def go():
        pool.rows[(1, None)] = _row(1)
        await d.get_automod_config(1)
        pool.rows[(1, None)] = _row(1, anti_invite_enabled=True)
        d._automod_config_cache[(1, None)] = (d._automod_config_cache[(1, None)][0], time.monotonic() - 10_000)
        pool.gate.clear()                                         # DB is slow: the message must not wait for it
        got = await asyncio.wait_for(d.get_automod_config(1), 0.2)
        assert got["anti_invite_enabled"] is False
        for _ in range(20):                                       # many messages during the refresh -> still one query
            await d.get_automod_config(1)
        pool.gate.set()
        await asyncio.sleep(0.05)
        assert pool.calls == 2
        assert (await d.get_automod_config(1))["anti_invite_enabled"] is True
    asyncio.run(go())


def test_write_during_inflight_read_is_not_overwritten_by_the_old_read(env):
    d, pool = env

    async def go():
        pool.rows[(1, None)] = _row(1)
        pool.gate.clear()
        reader = asyncio.ensure_future(d.get_automod_config(1))   # query started, sees the OLD row, blocked
        await asyncio.sleep(0.01)
        pool.rows[(1, None)] = _row(1, anti_invite_enabled=True)  # admin edit commits...
        d.invalidate_automod_config(1)                            # ...and invalidates
        pool.gate.set()
        await reader
        await asyncio.sleep(0.01)
        assert (1, None) not in d._automod_config_cache           # the pre-write result must not be cached
        assert (await d.get_automod_config(1))["anti_invite_enabled"] is True
    asyncio.run(go())


def test_set_automod_config_takes_effect_immediately(env):
    d, pool = env

    class W(_Conn):
        async def execute(self, sql, *args):
            pool.rows[(args[0], args[1])] = _row(args[0], args[1], anti_invite_enabled=args[7])
    pool.acquire = lambda: type("A", (_Acq,), {"__aenter__": lambda s: _co(W(pool))})(pool)

    async def _co(x):
        return x

    async def go():
        pool.rows[(1, None)] = _row(1)
        assert (await d.get_automod_config(1))["anti_invite_enabled"] is False
        merged_defaults = {k: None for k in ("action", "timeout_minutes", "log_channel_id", "word_filter_enabled",
                                             "anti_mention_enabled", "anti_mention_threshold", "spam_enabled",
                                             "spam_flood_threshold", "spam_flood_window_seconds", "min_account_age_hours",
                                             "log_channel_auto_created", "log_channel_notice_count",
                                             "log_channel_last_notice_at", "wordfilter_notice_count",
                                             "wordfilter_last_notice_at", "wizard_channel_id", "wizard_message_id",
                                             "wizard_invoker_id", "log_server_enabled", "log_channels_enabled",
                                             "log_roles_enabled", "log_members_enabled", "log_moderation_enabled",
                                             "log_voice_enabled", "log_invites_enabled", "modlog_wizard_channel_id",
                                             "modlog_wizard_message_id", "modlog_wizard_invoker_id")}
        pool.rows[(1, None)].update(merged_defaults)
        await d.set_automod_config(1, anti_invite_enabled=True)
        assert (await d.get_automod_config(1))["anti_invite_enabled"] is True
    asyncio.run(go())


def test_db_failure_serves_last_known_config_then_recovers(env):
    d, pool = env

    async def go():
        pool.rows[(1, None)] = _row(1, anti_invite_enabled=True)
        await d.get_automod_config(1)
        d._automod_config_cache[(1, None)] = (d._automod_config_cache[(1, None)][0], time.monotonic() - 10_000)
        pool.fail = True
        got = await d.get_automod_config(1)                       # stale served, refresh fails in background
        await asyncio.sleep(0.05)
        assert got["anti_invite_enabled"] is True
        calls = pool.calls
        for _ in range(10):
            assert (await d.get_automod_config(1))["anti_invite_enabled"] is True
        assert pool.calls == calls                                # no hammering while the DB is down
        pool.fail = False
        d._automod_config_cache[(1, None)] = (d._automod_config_cache[(1, None)][0], time.monotonic() - 10_000)
        await d.get_automod_config(1)
        await asyncio.sleep(0.05)
        assert pool.calls == calls + 1
    asyncio.run(go())


def test_db_failure_with_nothing_cached_still_raises(env):
    d, pool = env
    pool.fail = True

    async def go():
        with pytest.raises(ConnectionError):
            await d.get_automod_config(1)
        assert not d._automod_flights                             # failed flight cleaned up
    asyncio.run(go())


def test_cache_is_bounded_and_flights_do_not_leak(env, monkeypatch):
    d, pool = env
    monkeypatch.setattr(Database, "_AUTOMOD_CACHE_MAX_ENTRIES", 10)

    async def go():
        await asyncio.gather(*[d.get_automod_config(g) for g in range(100)])
        await asyncio.sleep(0.01)
        assert len(d._automod_config_cache) == 10 and not d._automod_flights
    asyncio.run(go())


def test_one_cancelled_caller_does_not_cancel_the_shared_load(env):
    d, pool = env

    async def go():
        pool.gate.clear()
        a = asyncio.ensure_future(d.get_automod_config(1))
        b = asyncio.ensure_future(d.get_automod_config(1))
        await asyncio.sleep(0.01)
        a.cancel()
        pool.gate.set()
        assert (await b)["guild_id"] == 1
    asyncio.run(go())


def test_fresh_bypasses_cache(env):
    d, pool = env

    async def go():
        pool.rows[(1, None)] = _row(1)
        await d.get_automod_config(1)
        pool.rows[(1, None)] = _row(1, anti_invite_enabled=True)
        assert (await d.get_automod_config(1, fresh=True))["anti_invite_enabled"] is True
    asyncio.run(go())
