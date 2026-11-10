"""Per-server inactivity sleep (modules.guild_activity SLEEP layer + its consumers). No network, no database."""
import asyncio
import pathlib
from types import SimpleNamespace

import pytest

from modules import guild_activity as ga
from modules import sleep_events as se
from modules.guild_activity import ActivityTracker

DAY = 86400
HOUR = 3600
ROOT = pathlib.Path(__file__).resolve().parents[2]


def _t():
    now = [1_000_000.0]
    return now, ActivityTracker(clock=lambda: now[0])


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for k in ("GUILD_DORMANT_DAYS", "GUILD_SLEEP_DAYS", "GUILD_SLEEP_RECHECK_HOURS", "GUILD_DORMANCY"):
        monkeypatch.delenv(k, raising=False)
    se._queue.clear()
    se._last_sent.clear()
    for k in se.counts:
        se.counts[k] = 0


# ── 1. detection ────────────────────────────────────────────────────────────────────────────────────────────────
def test_guild_falls_asleep_after_three_days_without_activity():
    now, t = _t()
    t.seed([1])
    now[0] += 3 * DAY - 60
    assert not t.is_sleeping(1, member_count=5000)
    now[0] += 120
    assert t.is_sleeping(1, member_count=5000)          # a BIG server too: sleep is 3 days for everyone
    assert t.is_sleeping(1)                             # also without a member count


def test_unseen_guild_never_sleeps_and_switch_off_works(monkeypatch):
    now, t = _t()
    assert not t.is_sleeping(99)
    t.seed([1])
    now[0] += 10 * DAY
    assert t.is_sleeping(1)
    monkeypatch.setenv("GUILD_DORMANCY", "0")
    assert not t.is_sleeping(1)


def test_sleep_threshold_is_configurable_and_clamped(monkeypatch):
    assert ga.sleep_days() == 3
    monkeypatch.setenv("GUILD_SLEEP_DAYS", "5")
    assert ga.sleep_days() == 5
    monkeypatch.setenv("GUILD_SLEEP_DAYS", "0")
    assert ga.sleep_days() == 1
    monkeypatch.setenv("GUILD_SLEEP_DAYS", "junk")
    assert ga.sleep_days() == 3


def test_existing_dormancy_tiers_are_untouched_and_dormant_implies_asleep():
    now, t = _t()
    t.seed([1])
    now[0] += 4 * DAY
    assert t.is_sleeping(1, member_count=5000) and not t.is_dormant(1, member_count=5000)   # asleep, cats/ship still on
    assert t.is_dormant(1, member_count=10) and t.is_sleeping(1, member_count=10)
    now[0] += 20 * DAY
    assert t.is_dormant(1, member_count=5000) and t.is_sleeping(1, member_count=5000)
    assert [ga.dormant_days_for(n) for n in (10, 100, 5000)] == [3, 7, 14]


def test_flat_dormant_env_below_sleep_days_still_sleeps_first(monkeypatch):
    monkeypatch.setenv("GUILD_DORMANT_DAYS", "1")
    now, t = _t()
    t.seed([1])
    now[0] += 2 * DAY
    assert t.is_dormant(1, member_count=50) and t.is_sleeping(1, member_count=50) and t.is_sleeping(1)


# ── 2/4. wake-up ─────────────────────────────────────────────────────────────────────────────────────────────────
def test_activity_wakes_a_sleeping_guild_immediately_and_fires_the_hook_once():
    now, t = _t()
    seen = []
    t.on_wake(lambda g, reason, quiet: seen.append((g, reason, round(quiet / DAY, 1))))
    t.seed([1])
    now[0] += 4 * DAY
    assert t.is_sleeping(1)
    t.touch(1, "message")
    assert not t.is_sleeping(1)
    assert seen == [(1, "message", 4.0)]
    t.touch(1, "message")
    now[0] += 400
    t.touch(1, "interaction")
    assert len(seen) == 1                               # awake already: no repeat wake


def test_waking_clears_recheck_timers_so_paused_work_runs_on_the_next_tick():
    now, t = _t()
    t.seed([1])
    now[0] += 4 * DAY
    assert t.gate_due("k", {1: None}) is True           # first look at a sleeping guild runs once
    assert t.gate_due("k", {1: None}) is False          # then it sleeps
    t.touch(1)
    assert t.gate_due("k", {1: None}) is True           # woke: runs at once, no waiting for the recheck window
    assert t.gate_due("k", {1: None}) is True           # and keeps running at the normal pace


def test_insignificant_events_do_not_wake_or_flap():
    """The cog only touches for people; this pins the tracker side: no touch means still asleep, and after a wake the guild
    needs a full 3 quiet days again before it sleeps (no sleep/wake flapping)."""
    now, t = _t()
    t.seed([1])
    now[0] += 5 * DAY
    assert t.is_sleeping(1)
    now[0] += 10 * HOUR                                  # bots/typing/presence would be here: nothing touches
    assert t.is_sleeping(1)
    t.touch(1)
    for _ in range(20):
        now[0] += 2 * HOUR
        assert not t.is_sleeping(1)
    now[0] += 3 * DAY
    assert t.is_sleeping(1)


# ── 2. periodic recheck while asleep ──────────────────────────────────────────────────────────────────────────────
def test_sleeping_guild_is_rechecked_only_every_few_hours():
    now, t = _t()
    t.seed([1])
    now[0] += 4 * DAY
    runs = 0
    for _ in range(48):                                  # a 30-minute loop for 24 hours
        runs += t.gate_due("feed", {1: None})
        now[0] += 1800
    assert runs == 4                                     # once per 6 hours instead of 48 times


def test_recheck_interval_is_configurable(monkeypatch):
    monkeypatch.setenv("GUILD_SLEEP_RECHECK_HOURS", "12")
    assert ga.sleep_recheck_seconds() == 12 * HOUR
    monkeypatch.setenv("GUILD_SLEEP_RECHECK_HOURS", "999")
    assert ga.sleep_recheck_seconds() == 48 * HOUR


def test_shared_work_only_sleeps_when_every_involved_guild_is_asleep():
    now, t = _t()
    t.seed([1, 2])
    now[0] += 4 * DAY
    t.touch(2)                                           # guild 2 is awake
    assert all(t.gate_due("shared", {1: None, 2: None}) for _ in range(5))
    now[0] += 4 * DAY
    assert t.gate_due("shared", {1: None, 2: None}) and not t.gate_due("shared", {1: None, 2: None})


# ── 2. isolation ──────────────────────────────────────────────────────────────────────────────────────────────────
def test_many_guilds_sleep_and_wake_independently():
    now, t = _t()
    ids = list(range(1, 101))
    t.seed(ids)
    now[0] += 2 * DAY
    for g in (10, 20, 30):
        t.touch(g)
    now[0] += 2 * DAY                                    # 4 days for most, 2 for the three that talked
    asleep = {g for g in ids if t.is_sleeping(g)}
    assert asleep == set(ids) - {10, 20, 30}
    t.touch(5)                                           # waking one leaves every other guild asleep
    assert not t.is_sleeping(5) and {g for g in ids if t.is_sleeping(g)} == asleep - {5}
    assert t.gate_due("a", {7: None}) and not t.gate_due("a", {7: None})
    assert t.gate_due("b", {8: None})                    # different key, different guild: unaffected by guild 7's timer


# ── 6. restart safety, removal, bounded memory ──────────────────────────────────────────────────────────────────────
def test_sleep_survives_a_restart_and_is_not_reset_by_seeding():
    now, t = _t()
    t.seed([1, 2])
    now[0] += 4 * DAY
    saved = t.dump()
    t2 = ActivityTracker(clock=lambda: now[0])           # new process: nothing in memory
    assert not t2.is_sleeping(1)                         # before the saved stamps load it fails open (awake)
    t2.load(saved)
    t2.seed([1, 2, 3])                                   # the cog seeds right after loading
    assert t2.is_sleeping(1) and t2.is_sleeping(2)       # still asleep
    assert not t2.is_sleeping(3)                         # a guild first seen now starts its clock now
    t2.touch(2)                                          # activity right after the restart wakes it
    assert not t2.is_sleeping(2) and t2.is_sleeping(1)


def test_activity_before_the_stamps_load_is_not_overwritten_by_older_ones():
    now, t = _t()
    t.seed([1])
    now[0] += 4 * DAY
    saved = t.dump()
    t2 = ActivityTracker(clock=lambda: now[0])
    t2.touch(1)                                          # a message arrives before the first load
    t2.load(saved)
    assert not t2.is_sleeping(1)


def test_removed_guilds_are_forgotten_completely():
    now, t = _t()
    t.seed([1, 2])
    now[0] += 4 * DAY
    assert t.is_sleeping(1) and t.gate_due("k", {1: None})
    t.forget(1)
    assert t.quiet_seconds(1) is None and 1 not in t.sleeping_ids() and not t._gate_next and 1 not in t._gate_guilds
    t.forget_missing([])                                 # the periodic cleanup also drops anything left
    assert t.sleeping_ids() == set() and t.quiet_seconds(2) is None


def test_sleeping_state_is_bounded_by_guild_count():
    now, t = _t()
    ids = range(1, 3001)
    t.seed(ids)
    now[0] += 4 * DAY
    for g in ids:
        t.gate_due(f"k{g % 10}", {g: None})
    assert len(t.sleeping_ids()) == 3000 and len(t._gate_next) <= 10


def test_a_broken_wake_hook_cannot_stop_the_wake():
    now, t = _t()
    t.on_wake(lambda *a: 1 / 0)
    got = []
    t.on_wake(lambda g, r, q: got.append(g))
    t.seed([1])
    now[0] += 4 * DAY
    t.is_sleeping(1)
    t.touch(1)
    assert got == [1] and not t.is_sleeping(1)


# ── cog: listeners, logging, restart summary ────────────────────────────────────────────────────────────────────
def _cog(monkeypatch, guilds):
    from discord_bot.cogs import guild_activity as cog_mod
    now, t = _t()
    monkeypatch.setattr(ga, "tracker", t)
    bot = SimpleNamespace(clone_id=None, user=SimpleNamespace(id=999), guilds=guilds,
                          get_guild=lambda gid: next((g for g in guilds if g.id == gid), None))
    c = cog_mod.GuildActivityCog.__new__(cog_mod.GuildActivityCog)
    c.bot, c._sleep_prev, c._dormant_prev, c._stage_prev = bot, None, None, {}
    t.on_wake(c._on_wake)
    return c, t, now


def _g(i, n=50):
    return SimpleNamespace(id=i, name=f"G{i}", member_count=n)


def test_cog_logs_sleep_and_wake_and_stays_quiet_on_the_first_pass_after_start(monkeypatch):
    guilds = [_g(1), _g(2)]
    c, t, now = _cog(monkeypatch, guilds)
    t.seed([1, 2])
    now[0] += 4 * DAY
    c._report_sleep()                                    # first pass after a (re)start: restored silently
    assert se.pending() == 0 and c._sleep_prev == {1, 2}
    t.touch(2, "message")
    assert [k for k, _ in se.drain(10)] == ["guild_wake"]
    c._report_sleep()                                    # the next 10-minute pass: only guild 1 is still asleep
    assert se.pending() == 0 and c._sleep_prev == {1}
    now[0] += 4 * DAY
    c._report_sleep()                                    # guild 2 has gone back to sleep after 4 more quiet days
    assert [k for k, _ in se.drain(10)] == ["guild_sleep"] and c._sleep_prev == {1, 2}


def test_cog_listeners_count_people_only(monkeypatch):
    guilds = [_g(1)]
    c, t, now = _cog(monkeypatch, guilds)
    t.seed([1])
    now[0] += 4 * DAY
    assert t.is_sleeping(1)
    bot_msg = SimpleNamespace(guild=guilds[0], author=SimpleNamespace(bot=True))
    asyncio.run(c.on_message(bot_msg))
    asyncio.run(c.on_message(SimpleNamespace(guild=None, author=SimpleNamespace(bot=False))))                       # DM
    asyncio.run(c.on_raw_reaction_add(SimpleNamespace(guild_id=1, member=None, user_id=999)))                       # our own reaction
    asyncio.run(c.on_member_join(SimpleNamespace(bot=True, guild=guilds[0])))
    assert t.is_sleeping(1)                                                                                          # nothing woke it
    asyncio.run(c.on_message(SimpleNamespace(guild=guilds[0], author=SimpleNamespace(bot=False))))
    assert not t.is_sleeping(1)


@pytest.mark.parametrize("event", ["member_join", "reaction", "interaction", "voice"])
def test_other_human_events_also_wake(monkeypatch, event):
    guilds = [_g(1)]
    c, t, now = _cog(monkeypatch, guilds)
    t.seed([1])
    now[0] += 4 * DAY
    assert t.is_sleeping(1)
    if event == "member_join":
        asyncio.run(c.on_member_join(SimpleNamespace(bot=False, guild=guilds[0])))
    elif event == "reaction":
        asyncio.run(c.on_raw_reaction_add(SimpleNamespace(guild_id=1, member=SimpleNamespace(bot=False), user_id=5)))
    elif event == "interaction":
        asyncio.run(c.on_interaction(SimpleNamespace(guild_id=1)))
    else:
        asyncio.run(c.on_voice_state_update(SimpleNamespace(bot=False, guild=guilds[0]), None, SimpleNamespace(channel=object())))
    assert not t.is_sleeping(1)


def test_guild_removal_clears_state(monkeypatch):
    guilds = [_g(1)]
    c, t, now = _cog(monkeypatch, guilds)
    t.seed([1])
    now[0] += 4 * DAY
    t.is_sleeping(1)
    asyncio.run(c.on_guild_remove(guilds[0]))
    assert t.quiet_seconds(1) is None and not t.sleeping_ids()


# ── consumers: feeds ───────────────────────────────────────────────────────────────────────────────────────────────
def _connect(monkeypatch, guilds):
    from discord_bot.cogs import connect
    now, t = _t()
    monkeypatch.setattr(ga, "tracker", t)
    c = connect.ConnectCog.__new__(connect.ConnectCog)
    c.bot = SimpleNamespace(get_guild=lambda gid: next((g for g in guilds if g.id == gid), None))
    return c, t, now


def test_feed_polls_sleep_for_hours_and_resume_at_once_on_wake(monkeypatch):
    guilds = [_g(1), _g(2)]
    c, t, now = _connect(monkeypatch, guilds)
    t.seed([1, 2])
    now[0] += 4 * DAY
    rows = [{"guild_id": 1}, {"guild_id": 2}]
    runs = 0
    for _ in range(48):
        runs += c._poll_due("yt:abc", rows)
        now[0] += 1800
    assert runs == 4
    t.touch(2)
    assert c._poll_due("yt:abc", rows) and c._poll_due("yt:abc", rows)       # one awake follower: normal pace again


def test_feed_with_an_unknown_guild_or_no_state_keeps_the_normal_pace(monkeypatch):
    c, t, now = _connect(monkeypatch, [_g(1)])
    assert all(c._poll_due("yt:z", [{"guild_id": 1}, {"guild_id": 77}]) for _ in range(6))
    assert all(c._poll_due("yt:y", [{"guild_id": 1}]) for _ in range(6))      # never seen by the tracker: fails open


# ── audit guard: essential features must never ask whether a server is asleep ─────────────────────────────────────
ESSENTIAL = ["antiraid", "verification", "scam_shield", "schedule", "giveaways", "server_logs", "moderation", "bump",
             "log_channel", "data_retention", "catch", "leveling", "voice_xp", "report_notifications", "ai_jobs"]


@pytest.mark.parametrize("name", ESSENTIAL)
def test_essential_cogs_never_consult_sleep_state(name):
    src = (ROOT / "discord_bot" / "cogs" / f"{name}.py").read_text()
    assert "is_sleeping" not in src and "gate_due" not in src


def test_welcome_events_and_automod_event_handlers_are_not_gated():
    """Only the daily promo loops of welcome.py and the reminder loop of automod.py skip sleeping servers."""
    for name, allowed in (("welcome", 3), ("automod", 1)):
        src = (ROOT / "discord_bot" / "cogs" / f"{name}.py").read_text()
        assert src.count("is_sleeping(") == allowed
    w = (ROOT / "discord_bot" / "cogs" / "welcome.py").read_text()
    for handler in ("async def on_member_join", "async def on_member_remove"):
        if handler in w:
            body = w.split(handler, 1)[1].split("\n    async def ", 1)[0].split("\n    @", 1)[0]
            assert "is_sleeping" not in body


# ── deeper stages: days -> weeks -> months ────────────────────────────────────────────────────────────────────────
def test_sleep_deepens_from_days_to_weeks_to_months_and_wakes_from_any_depth():
    now, t = _t()
    t.seed([1])
    assert t.sleep_stage(1) == 0
    now[0] += 4 * DAY
    assert t.sleep_stage(1) == 1
    now[0] += 4 * DAY                                    # 8 days
    assert t.sleep_stage(1) == 2
    now[0] += 25 * DAY                                   # 33 days
    assert t.sleep_stage(1) == 3
    t.touch(1)
    assert t.sleep_stage(1) == 0 and not t.is_sleeping(1)


def test_deeper_sleep_rechecks_less_often():
    def runs_in_two_days(quiet_days):
        now, t = _t()
        t.seed([1])
        now[0] += quiet_days * DAY
        n = 0
        for _ in range(2 * 48):                          # a 30-minute loop for 2 days
            n += t.gate_due("k", {1: None})
            now[0] += 1800
        return n
    assert (runs_in_two_days(3.5), runs_in_two_days(8), runs_in_two_days(40)) == (8, 2, 1)   # 6h / 24h / 72h
    assert ga.ActivityTracker().recheck_seconds(1) == 6 * HOUR
    assert ga.ActivityTracker().recheck_seconds(2) == 24 * HOUR and ga.ActivityTracker().recheck_seconds(3) == 72 * HOUR


def test_stage_thresholds_are_configurable_and_kept_in_order(monkeypatch):
    monkeypatch.setenv("GUILD_SLEEP_WEEK_DAYS", "10")
    monkeypatch.setenv("GUILD_SLEEP_MONTH_DAYS", "5")    # nonsense order: month is never earlier than week
    now, t = _t()
    t.seed([1])
    now[0] += 11 * DAY
    assert t.sleep_stage(1) == 3
    monkeypatch.setenv("GUILD_SLEEP_WEEK_DAYS", "junk")
    assert ga.sleep_week_days() == 7 and ga.sleep_month_days() == 5


def test_shared_work_follows_the_lightest_sleeper_and_recheck_is_capped():
    now, t = _t()
    t.seed([1, 2])
    now[0] += 40 * DAY
    t.touch(2)
    now[0] += 4 * DAY                                    # guild 1 months deep, guild 2 only stage 1
    assert t.sleep_stage(1) == 3 and t.sleep_stage(2) == 1
    assert t.gate_due("s", {1: None, 2: None}) and t._gate_next["s"] - now[0] == 6 * HOUR
    assert t.recheck_seconds(3) <= ga.MAX_RECHECK_SECONDS


def test_cog_logs_when_a_guild_sleeps_deeper(monkeypatch, caplog):
    import logging
    guilds = [_g(1)]
    c, t, now = _cog(monkeypatch, guilds)
    t.seed([1])
    now[0] += 4 * DAY
    c._report_sleep()
    c._report_sleep()
    now[0] += 5 * DAY
    with caplog.at_level(logging.INFO):
        c._report_sleep()
    assert any("sleeps deeper: stage 2 (weeks)" in r.message for r in caplog.records)
