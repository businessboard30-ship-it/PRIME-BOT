"""Anime feed slice B: due/sleep rules, worker lifecycle (sleep when idle, wake on demand, backoff, dedup, cancellation). No network."""
import asyncio
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import discord
import pytest

from modules import anime_feed as af
from modules import anime_feed_alerts as alerts
from modules import anime_feed_worker as aw
from modules import anime_feed_spawn as spawn_mod

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


def make(monkeypatch, guilds, items, ok=True, chans=None, claimed=None, enabled_raises=False, pick_ok=True):
    calls = SimpleNamespace(provider=0, claims=[], released=[], disabled=[], enabled_reads=0, noted=[])
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

    async def claim_spawn(c):
        calls.claims.append(c.guild_id)
        if c.guild_id in claimed or not af.spawn_is_due(c, time.time()):
            return None
        claimed.add(c.guild_id)
        return datetime.fromtimestamp(0, timezone.utc)

    async def release_spawn(c, prev):
        calls.released.append(c.guild_id)
        claimed.discard(c.guild_id)

    async def recent(g, c=None):
        return []

    async def noted(g, c, a):
        calls.noted.append((g, a))

    async def pick(kinds, recent_ids, rng=None):
        return (P(len(calls.claims) + 100, 0, 0, "Spawned"), True) if pick_ok else (None, False)

    async def disable(g, c=None):
        calls.disabled.append(g)
    monkeypatch.setattr(af, "enabled_guilds", enabled)
    monkeypatch.setattr(af, "airing_soon", airing)
    monkeypatch.setattr(af, "claim_spawn", claim_spawn)
    monkeypatch.setattr(af, "release_spawn", release_spawn)
    monkeypatch.setattr(af, "recent_spawned", recent)
    monkeypatch.setattr(af, "note_spawned", noted)
    monkeypatch.setattr(spawn_mod, "pick", pick)
    monkeypatch.setattr(af, "disable", disable)

    async def no_dm_followers():
        return False
    monkeypatch.setattr(alerts, "has_dm_followers", no_dm_followers)         # D2: nobody has a DM alert unless a test says so
    bot = SimpleNamespace(get_guild=lambda gid: SimpleNamespace(get_channel=lambda cid: chans.get(gid), get_member=lambda u: None), wait_until_ready=None)
    return aw.Worker(bot, None, post_gap=0), calls, chans


def cfg(g, last=None, interval=60):
    """A server with spawns on. `last` = seconds since its last spawn (None = it has never spawned, so one is due now)."""
    when = datetime.fromtimestamp(time.time() - last, timezone.utc) if last is not None else None
    return af.FeedConfig(g, None, True, 100 + g, None, None, "both", interval, when)


def test_no_enabled_feeds_means_no_provider_request_and_no_timer(monkeypatch):
    w, calls, _ = make(monkeypatch, [], [P(1, at=int(time.time()) - 10)])
    assert run(w.cycle()) is None and calls.provider == 0


def test_a_due_server_gets_one_spawn_and_is_not_spawned_again_until_its_interval_passes(monkeypatch):
    w, calls, chans = make(monkeypatch, [cfg(1), cfg(2)], [])
    wait = run(w.cycle())
    assert [len(c.sent) for c in chans.values()] == [1, 1] and calls.provider == 0           # no airing lookup: spawns don't need one
    assert calls.noted and 30 <= wait <= aw.MAX_SLEEP and w.state == aw.SLEEPING
    run(w.cycle())
    assert [len(c.sent) for c in chans.values()] == [1, 1]                                   # claimed: nothing spawned twice


def test_a_server_whose_interval_has_not_passed_is_left_alone(monkeypatch):
    w, calls, chans = make(monkeypatch, [cfg(1, last=600, interval=60)], [])                 # spawned 10 min ago, interval 60
    run(w.cycle())
    assert chans[1].sent == [] and calls.claims == []


def test_spawn_wait_sleeps_until_the_soonest_server_within_bounds():
    now = time.time()
    assert aw.spawn_wait([cfg(1, last=0, interval=60)], [], now) == aw.MAX_SLEEP              # next one is an hour away: capped at 30 min
    assert aw.spawn_wait([cfg(1, last=3590, interval=60)], [], now) == aw.MIN_SLEEP           # about to be due: never under 30 s
    assert aw.spawn_wait([], [], now) == aw.MAX_SLEEP


def test_the_interval_is_one_of_the_allowed_choices():
    assert af.clean_interval(30) == 30 and af.clean_interval("120") == 120
    assert af.clean_interval(7) == af.DEFAULT_SPAWN_INTERVAL and af.clean_interval(None) == af.DEFAULT_SPAWN_INTERVAL
    assert af.interval_label(15) == "every 15 minutes" and af.interval_label(60) == "every hour" and af.interval_label(360) == "every 6 hours"


def test_provider_or_database_failure_backs_off_with_growing_delay_and_recovers(monkeypatch):
    w2, _, _ = make(monkeypatch, [cfg(1)], [], enabled_raises=True)
    assert run(w2.cycle()) > 0 and w2.state == aw.BACKOFF
    w3, _, chans3 = make(monkeypatch, [cfg(1)], [])
    w3.failures = 3
    run(w3.cycle())
    assert w3.failures == 0 and len(chans3[1].sent) == 1


def test_no_title_to_show_gives_the_interval_back_and_posts_nothing(monkeypatch):
    w, calls, chans = make(monkeypatch, [cfg(1)], [], pick_ok=False)                          # AniList down or every title already seen
    run(w.cycle())
    assert chans[1].sent == [] and calls.released == [1]


def test_missing_permission_keeps_the_interval_but_a_server_error_gives_it_back_and_a_deleted_channel_switches_off(monkeypatch):
    forbidden = discord.Forbidden(SimpleNamespace(status=403, reason="x"), "no")
    w, calls, _ = make(monkeypatch, [cfg(1)], [], chans={1: Chan(fail=forbidden)})
    run(w.cycle())
    assert calls.released == []
    http = discord.HTTPException(SimpleNamespace(status=500, reason="x"), "boom")
    w, calls, _ = make(monkeypatch, [cfg(1)], [], chans={1: Chan(fail=http)})
    run(w.cycle())
    assert calls.released == [1]
    w, calls, _ = make(monkeypatch, [cfg(1)], [], chans={})
    run(w.cycle())
    assert calls.disabled == [1]


def test_a_cycle_spawns_a_bounded_batch(monkeypatch):
    guilds = [cfg(i) for i in range(1, 40)]
    w, _, chans = make(monkeypatch, guilds, [])
    run(w.cycle())
    assert sum(len(c.sent) for c in chans.values()) == aw.MAX_POSTS_PER_CYCLE


def test_worker_sleeps_when_no_server_is_set_up_wakes_on_demand_and_stops_cleanly(monkeypatch):
    async def go():
        w, calls, chans = make(monkeypatch, [], [], chans={1: Chan()})
        w.start()
        await asyncio.sleep(0.05)
        assert w.state == aw.STOPPED and calls.provider == 0 and calls.enabled_reads == 1       # asleep: no polling at all
        await asyncio.sleep(0.2)
        assert calls.enabled_reads == 1
        guilds = [cfg(1)]

        async def enabled(clone_id=None):
            calls.enabled_reads += 1
            return guilds
        monkeypatch.setattr(af, "enabled_guilds", enabled)
        aw.wake_all()                                                                           # setting anime up wakes it at once
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
    spawn_sql = (ROOT / "database/migrations/084_anime_spawn.sql").read_text()
    assert "spawn_interval_minutes" in spawn_sql and "last_spawn_at" in spawn_sql and "DROP" not in spawn_sql.upper()
    assert "084_anime_spawn.sql" in (ROOT / "database.py").read_text()


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
