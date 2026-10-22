import os

from modules.guild_activity import ActivityTracker, dormant_days_for, setting_key

DAY = 86400


def _t():
    now = [1_000_000.0]
    return now, ActivityTracker(clock=lambda: now[0])


def test_unseen_server_is_never_dormant_and_seed_starts_the_clock():
    now, t = _t()
    assert not t.is_dormant(1)
    t.seed([1])
    now[0] += 13 * DAY
    assert not t.is_dormant(1)
    now[0] += 2 * DAY
    assert t.is_dormant(1)


def test_activity_wakes_a_dormant_server():
    now, t = _t()
    t.seed([1])
    now[0] += 20 * DAY
    assert t.is_dormant(1)
    t.touch(1)
    assert not t.is_dormant(1)


def test_touch_is_rate_limited_but_marks_dirty_when_stamped():
    now, t = _t()
    t.touch(1)
    t.dirty = False
    now[0] += 10
    t.touch(1)
    assert not t.dirty
    now[0] += 400
    t.touch(1)
    assert t.dirty


def test_dump_load_round_trip_and_bad_data_fails_open():
    now, t = _t()
    t.seed([1, 2])
    now[0] += 30 * DAY
    t2 = ActivityTracker(clock=lambda: now[0])
    t2.load(t.dump())
    assert t2.is_dormant(1) and t2.is_dormant(2)
    t3 = ActivityTracker(clock=lambda: now[0])
    t3.load("not json {")
    assert not t3.is_dormant(1)


def test_forget_missing_and_switch_off(monkeypatch):
    now, t = _t()
    t.seed([1, 2])
    t.forget_missing([1])
    assert t.quiet_seconds(2) is None
    now[0] += 30 * DAY
    assert t.is_dormant(1)
    monkeypatch.setenv("GUILD_DORMANCY", "0")
    assert not t.is_dormant(1)


def test_setting_keys_are_per_bot():
    assert setting_key(None) != setting_key(5) and setting_key(5) == "guild_activity:5"


def test_quiet_time_that_counts_as_dormant_scales_with_server_size(monkeypatch):
    monkeypatch.delenv("GUILD_DORMANT_DAYS", raising=False)
    assert [dormant_days_for(n) for n in (2, 10, 20, 21, 200, 201, 5000)] == [3, 3, 3, 7, 7, 14, 14]
    assert dormant_days_for(None) == 14 and dormant_days_for(0) == 14 and dormant_days_for("x") == 14


def test_a_small_quiet_server_sleeps_sooner_than_a_big_one_and_activity_still_wakes_it(monkeypatch):
    monkeypatch.delenv("GUILD_DORMANT_DAYS", raising=False)
    now, t = _t()
    t.seed([1, 2])
    now[0] += 4 * DAY
    assert t.is_dormant(1, member_count=10) and not t.is_dormant(2, member_count=5000)
    assert not t.is_dormant(1)                       # no member count: the flat 14-day default, as before
    t.touch(1)
    assert not t.is_dormant(1, member_count=10)      # any activity wakes it at once


def test_flat_env_threshold_overrides_size_tiers(monkeypatch):
    monkeypatch.setenv("GUILD_DORMANT_DAYS", "30")
    assert dormant_days_for(2) == dormant_days_for(9999) == 30
    now, t = _t()
    t.seed([1])
    now[0] += 20 * DAY
    assert not t.is_dormant(1, member_count=2)
