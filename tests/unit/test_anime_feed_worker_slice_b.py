"""Anime feed slice B: due/sleep rules, worker lifecycle (sleep when idle, wake on demand, backoff, dedup, cancellation). No network."""
import asyncio
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import discord
import pytest

from modules import anime_feed as af
from modules import anime_feed_worker as aw

ROOT = Path(__file__).resolve().parents[2]


def run(c):
    return asyncio.run(c)


def P(i, ep=1, at=0, title="Show"):
    return af.Preview(i, title, ep, at, "syn", None, f"https://anilist.co/anime/{i}")


def test_due_only_recent_aired_and_after_the_feed_was_switched_on():
    now = 10_000_000
    items = [P(1, at=now - 60), P(2, at=now + 60), P(3, at=now - aw.GRACE_SECONDS - 1), P(4, at=now - 3000)]
    assert [p.anime_id for p in aw.due(items, now)] == [1, 4]
    assert [p.anime_id for p in aw.due(items, now, since_ts=now - 100)] == [1]       # 4 aired before the feed was on: never a backlog


def test_sleep_until_the_next_episode_within_bounds():
    now = 1000
    assert aw.next_wait([P(1, at=now + 600)], now) == 605
    assert aw.next_wait([P(1, at=now + 5)], now) == aw.MIN_SLEEP
    assert aw.next_wait([P(1, at=now + 10 ** 6)], now) == aw.MAX_SLEEP
    assert aw.next_wait([], now) == aw.MAX_SLEEP


def test_backoff_grows_with_jitter_and_is_capped():
    class R:
        @staticmethod
        def uniform(a, b): return b
    assert [aw.backoff_delay(i, R) for i in range(7)] == [60, 120, 240, 480, 960, 1800, 1800]
    assert 30 <= aw.backoff_delay(0) <= 60


class Chan:
    def __init__(self, fail=None):
        self.sent, self.fail = [], fail

    async def send(self, **kw):
        if self.fail:
            raise self.fail
        self.sent.append(kw)


def make(monkeypatch, guilds, items, ok=True, chans=None, claimed=None, enabled_raises=False):
    calls = SimpleNamespace(provider=0, claims=[], released=[], disabled=[], enabled_reads=0)
    chans = chans if chans is not None else {g.guild_id: Chan() for g in guilds}
    claimed = claimed if claimed is not None else set()

    async def enabled(clone_id=None):
        calls.enabled_reads += 1
        if enabled_raises:
            raise RuntimeError("db down")
        return guilds

    async def airing():
        calls.provider += 1
        return items, ok

    async def claim(g, c, a, e):
        calls.claims.append((g, a, e))
        if (g, a, e) in claimed:
            return False
        claimed.add((g, a, e))
        return True

    async def release(g, c, a, e):
        calls.released.append((g, a, e))
        claimed.discard((g, a, e))

    async def disable(g, c=None):
        calls.disabled.append(g)
    monkeypatch.setattr(af, "enabled_guilds", enabled)
    monkeypatch.setattr(af, "airing_soon", airing)
    monkeypatch.setattr(af, "claim", claim)
    monkeypatch.setattr(af, "release", release)
    monkeypatch.setattr(af, "disable", disable)
    bot = SimpleNamespace(get_guild=lambda gid: SimpleNamespace(get_channel=lambda cid: chans.get(gid)), wait_until_ready=None)
    return aw.Worker(bot, None, post_gap=0), calls, chans


def cfg(g, since=None):
    return af.FeedConfig(g, None, True, 100 + g, datetime.fromtimestamp(since, timezone.utc) if since else None)


def test_no_enabled_feeds_means_no_provider_request_and_no_timer(monkeypatch):
    w, calls, _ = make(monkeypatch, [], [P(1, at=int(time.time()) - 10)])
    assert run(w.cycle()) is None and calls.provider == 0


def test_due_episode_is_posted_once_per_server_and_a_second_pass_posts_nothing(monkeypatch):
    now = int(time.time())
    w, calls, chans = make(monkeypatch, [cfg(1, now - 500), cfg(2, now - 500)], [P(7, 2, now - 30), P(8, 1, now + 900)])
    wait = run(w.cycle())
    assert [len(c.sent) for c in chans.values()] == [1, 1] and calls.provider == 1          # one shared provider call for both servers
    assert 30 <= wait <= 905 and w.state == aw.SLEEPING
    run(w.cycle())
    assert [len(c.sent) for c in chans.values()] == [1, 1]                                  # dedup: nothing posted twice


def test_old_backlog_and_other_feeds_start_are_not_posted(monkeypatch):
    now = int(time.time())
    w, _, chans = make(monkeypatch, [cfg(1, now - 10)], [P(1, at=now - 600)])               # aired before the feed was switched on
    run(w.cycle())
    assert chans[1].sent == []


def test_provider_or_database_failure_backs_off_with_growing_delay_and_recovers(monkeypatch):
    now = int(time.time())
    w, calls, chans = make(monkeypatch, [cfg(1)], [], ok=False)
    d1, d2 = run(w.cycle()), run(w.cycle())
    assert w.state == aw.BACKOFF and w.failures == 2 and 30 <= d1 <= 60 and 60 <= d2 <= 120
    w2, _, _ = make(monkeypatch, [cfg(1)], [], enabled_raises=True)
    assert run(w2.cycle()) > 0 and w2.state == aw.BACKOFF
    w3, _, chans3 = make(monkeypatch, [cfg(1, now - 100)], [P(1, at=now - 5)])
    w3.failures = 3
    run(w3.cycle())
    assert w3.failures == 0 and len(chans3[1].sent) == 1


def test_missing_permission_keeps_the_claim_but_a_server_error_gives_it_back_and_a_deleted_channel_switches_off(monkeypatch):
    now = int(time.time())
    forbidden = discord.Forbidden(SimpleNamespace(status=403, reason="x"), "no")
    w, calls, _ = make(monkeypatch, [cfg(1, now - 100)], [P(1, at=now - 5)], chans={1: Chan(fail=forbidden)})
    run(w.cycle())
    assert calls.released == []
    http = discord.HTTPException(SimpleNamespace(status=500, reason="x"), "boom")
    w, calls, _ = make(monkeypatch, [cfg(1, now - 100)], [P(1, at=now - 5)], chans={1: Chan(fail=http)})
    run(w.cycle())
    assert calls.released == [(1, 1, 1)]
    w, calls, _ = make(monkeypatch, [cfg(1, now - 100)], [P(1, at=now - 5)], chans={})
    run(w.cycle())
    assert calls.disabled == [1]


def test_a_cycle_posts_a_bounded_batch(monkeypatch):
    now = int(time.time())
    items = [P(i, at=now - 10) for i in range(1, 40)]
    w, _, chans = make(monkeypatch, [cfg(1, now - 100)], items)
    run(w.cycle())
    assert len(chans[1].sent) == aw.MAX_POSTS_PER_CYCLE


def test_worker_sleeps_when_idle_wakes_on_demand_and_stops_cleanly(monkeypatch):
    async def go():
        now = int(time.time())
        w, calls, chans = make(monkeypatch, [], [P(1, at=now - 5)], chans={1: Chan()})
        w.start()
        await asyncio.sleep(0.05)
        assert w.state == aw.STOPPED and calls.provider == 0 and calls.enabled_reads == 1       # idle: no polling at all
        await asyncio.sleep(0.2)
        assert calls.enabled_reads == 1
        guilds = [cfg(1, now - 100)]

        async def enabled(clone_id=None):
            calls.enabled_reads += 1
            return guilds
        monkeypatch.setattr(af, "enabled_guilds", enabled)
        aw.wake_all()                                                                           # a settings change wakes it at once
        await asyncio.sleep(0.1)
        assert len(chans[1].sent) == 1 and w.state == aw.SLEEPING
        await w.stop()
        assert w.task is None and w not in aw._workers
    run(go())


def test_wiring_and_migration():
    assert "anime_feed" in (ROOT / "discord_bot/bot.py").read_text()
    sql = (ROOT / "database/migrations/071_anime_feed_posting.sql").read_text()
    assert "anime_feed_posted" in sql and "PRIMARY KEY (guild_id, clone_key, anime_id, episode)" in sql and "DROP" not in sql.upper()
    assert "071_anime_feed_posting.sql" in (ROOT / "database.py").read_text()
    assert "wake_all" in (ROOT / "modules/anime_feed.py").read_text()


def test_a_crashing_cycle_backs_off_instead_of_killing_the_worker():
    async def go():
        w = aw.Worker(SimpleNamespace())
        calls = []

        async def cycle():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("db down while fetching followers")
            raise asyncio.CancelledError()

        w.cycle = cycle
        w._wake.set()                                        # skip the real sleep after the first (crashed) pass
        with pytest.raises(asyncio.CancelledError):
            await w.run()
        return calls, w
    calls, w = run(go())
    assert len(calls) == 2                                   # it survived the crash and ran another pass
    assert w.failures == 1 and w.state == aw.BACKOFF
