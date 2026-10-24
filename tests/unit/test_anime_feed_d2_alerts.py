"""Anime feed D2: Alert me (mention / DM / mute), quiet hours, DM queue + dedupe, worker lifecycle, persistence. No network, no database."""
import asyncio
import re
import time
from types import SimpleNamespace

import discord
import pytest

from discord_bot.cogs import _views_anime_actions as aa
from discord_bot.cogs import _views_anime_feed as vf
from discord_bot.cogs import _views_join_dm as jd
from modules import anime_feed as af
from modules import anime_feed_alerts as al
from modules import anime_plus as plus
from modules import anime_feed_limiter as lim
from modules import anime_feed_worker as aw


def run(c):
    return asyncio.run(c)


def P(anime_id=7, episode=2, at=None):
    return af.Preview(anime_id, "Show", episode, at or int(time.time()) - 30, "syn", None, f"https://anilist.co/anime/{anime_id}")


# -- pure rules ---------------------------------------------------------------------------

def test_quiet_window_wraps_midnight_and_empty_windows_never_match():
    assert al.in_window(23, 22, 7) and al.in_window(3, 22, 7) and not al.in_window(7, 22, 7) and not al.in_window(12, 22, 7)
    assert al.in_window(0, 0, 8) and not al.in_window(8, 0, 8)
    assert not al.in_window(5, None, None) and not al.in_window(5, 5, 5) and not al.in_window(5, 3, None)


def test_timezone_parsing_is_strict_and_local_hour_uses_it():
    assert al.parse_tz("UTC+1") == 1 and al.parse_tz("utc-5") == -5 and al.parse_tz("UTC+0") == 0
    assert al.parse_tz("Europe/Paris") is None and al.parse_tz("UTC+99") is None and al.parse_tz(None) is None
    noon_utc = 12 * 3600
    assert al.local_hour("UTC+0", noon_utc) == 12 and al.local_hour("UTC+3", noon_utc) == 15 and al.local_hour("UTC-13", noon_utc) == 12   # bad tz = UTC
    assert al.is_quiet(al.Prefs("UTC+0", 22, 7), 23 * 3600) and not al.is_quiet(al.Prefs("UTC+0", 22, 7), 12 * 3600)


def test_presets_round_trip_and_quiet_people_are_dropped_from_the_mention_line():
    assert al.preset_key(al.Prefs()) == "off" and al.preset_key(al.Prefs(quiet_start=22, quiet_end=7)) == "22-7"
    night = 2 * 3600
    prefs = {1: al.Prefs("UTC+0", 22, 7)}
    assert al.split_mentions([1, 2], prefs, night) == [2] and al.split_mentions([1, 2], prefs, 12 * 3600) == [1, 2]
    assert len(al.TZ_CHOICES) <= 25 and len(al.QUIET_PRESETS) <= 25


def test_limiter_keeps_a_reserve_for_the_worker():
    l = lim.Limiter(per_minute=10, reserve=4)
    assert [l.try_acquire() for _ in range(7)] == [True] * 6 + [False]          # users stop at 10 - 4
    assert all(l.try_acquire(priority=True) for _ in range(4)) and not l.try_acquire(priority=True)


# -- panel + buttons ------------------------------------------------------------------------

def test_panel_has_three_selects_that_all_persist_and_the_current_choices_are_marked():
    view = aa.alert_panel(42, "Show", "dm", al.Prefs("UTC+1", 22, 7))
    sels = [k for k in view.walk_children() if isinstance(k, discord.ui.DynamicItem)]
    assert sorted(k.custom_id for k in sels) == ["anime_amode:42", "anime_aq:42", "anime_atz:42"]
    defaults = {k.custom_id: [o.value for o in k.item.options if o.default] for k in sels}
    assert defaults == {"anime_amode:42": ["dm"], "anime_atz:42": ["1"], "anime_aq:42": ["22-7"]}
    text = " ".join(getattr(k, "content", "") or "" for k in view.walk_children())
    assert "Alerts: Show" in text and "opt-in" in text


def test_panel_for_someone_not_following_says_so_and_marks_nothing():
    view = aa.alert_panel(42, "Show", None, al.Prefs())
    mode = next(k for k in view.walk_children() if getattr(k, "custom_id", "") == "anime_amode:42")
    assert not any(o.default for o in mode.item.options)
    assert "don't follow" in " ".join(getattr(k, "content", "") or "" for k in view.walk_children())


def test_every_new_item_is_registered_and_rebuilds_from_its_custom_id_alone():
    items = vf.DYNAMIC_ITEMS
    assert all(c in jd.DYNAMIC_ITEMS for c in items)                              # the bot registers them
    ids = [k.custom_id for v in (vf.post_view(P(42), dm=False), vf.post_view(P(42), dm=True), aa.alert_panel(42, "S", "mention", al.Prefs()))
           for k in v.walk_children() if isinstance(k, discord.ui.DynamicItem)]
    assert {"anime_alert:42", "anime_mute:42", "anime_amode:42", "anime_atz:42", "anime_aq:42"} <= set(ids)
    for cid in ids:                                                                 # a restart: only the custom_id string survives
        owners = [c for c in items if re.match(c.__discord_ui_compiled_template__, cid)]
        assert len(owners) == 1 and all(len(i) <= 100 for i in ids)
        item = run(owners[0].from_custom_id(None, None, owners[0].__discord_ui_compiled_template__.match(cid)))
        assert getattr(item, "anime_id", 42) == 42 and item.item.custom_id == cid       # Search / Categories carry no id


def test_dm_card_swaps_follow_for_mute_and_both_cards_fit_the_limits():
    server = [getattr(k, "item", k).label for k in vf.post_view(P()).walk_children() if isinstance(getattr(k, "item", k), discord.ui.Button)]
    dm = [getattr(k, "item", k).label for k in vf.post_view(P(), dm=True).walk_children() if isinstance(getattr(k, "item", k), discord.ui.Button)]
    assert server == ["Read more", "Follow", "Alert me", "Upgrade", "Search", "Categories", "Details on AniList"]
    assert dm == ["Read more", "Mute", "Alert me", "Details on AniList"]
    assert len(list(vf.post_view(P(), ping="<@1>").walk_children())) <= 40


class Resp:
    def __init__(self):
        self.sent, self.deferred = [], 0

    async def defer(self, **kw):
        self.deferred += 1

    async def send_message(self, *a, **kw):
        self.sent.append((a, kw))

    def is_done(self):
        return self.deferred > 0 or bool(self.sent)


def test_mode_select_defers_first_follows_when_needed_and_respects_the_free_limit(monkeypatch):
    calls = []

    async def set_mode(uid, aid, title, mode, limit):
        calls.append((uid, aid, title, mode, limit))
        return ("limit", 5) if mode == "dm" else ("ok", 1)

    async def get_mode(uid, aid):
        return "mention"

    async def get_prefs(uid):
        return al.Prefs()
    monkeypatch.setattr(al, "set_mode", set_mode)
    monkeypatch.setattr(al, "get_mode", get_mode)
    monkeypatch.setattr(al, "get_prefs", get_prefs)
    edits = []

    state = {"plus": False}

    async def has_plus(uid, gid=None, clone=None):
        return state["plus"]
    monkeypatch.setattr(plus, "has_anime_plus", has_plus)

    class I:
        user = SimpleNamespace(id=9)
        guild = None
        client = SimpleNamespace(clone_id=0)
        response = Resp()
        message = SimpleNamespace(components=[SimpleNamespace(content="## 🔔 Alerts: My Show", children=None)])

        def __init__(self, value):
            self.data = {"values": [value]}

        async def edit_original_response(self, **kw):
            edits.append(kw)
    item = aa._ModeSelect(42)
    i = I("off")
    run(item.callback(i))
    assert i.response.deferred == 1 and calls == [(9, 42, "My Show", "off", af.FREE_FOLLOW_LIMIT)] and len(edits) == 1
    i2 = I("dm")                                                      # DM alerts are exclusive: without the upgrade nothing changes
    run(item.callback(i2))
    text = " ".join(getattr(k, "content", "") or "" for k in edits[-1]["view"].walk_children())
    assert "Upgrade" in text and len(calls) == 1
    state["plus"] = True                                              # with the upgrade the limit is the bigger one
    run(item.callback(I("dm")))
    text = " ".join(getattr(k, "content", "") or "" for k in edits[-1]["view"].walk_children())
    assert "your limit" in text and calls[-1] == (9, 42, "My Show", "dm", plus.PLUS_FOLLOW_LIMIT)
    i3 = I("hack")
    run(item.callback(i3))
    assert i3.response.sent and calls[-1][3] == "dm" and len(calls) == 2           # an unknown value never reaches the database


# -- worker: DM queue, dedupe, quiet hours, lifecycle -----------------------------------------

class User:
    def __init__(self, uid, fail=None):
        self.id, self.fail, self.sent = uid, fail, []

    async def send(self, **kw):
        if self.fail:
            raise self.fail
        self.sent.append(kw)


def dm_worker(monkeypatch, recipients, prefs=None, users=None, claimed=None):
    claimed = claimed if claimed is not None else set()
    log = SimpleNamespace(results=[], released=[])

    async def dm_followers(aid, limit=0):
        return list(recipients)

    async def get_many(ids):
        return dict(prefs or {})

    async def claim(u, a, e):
        if (u, a, e) in claimed:
            return False
        claimed.add((u, a, e))
        return True

    async def release(u, a, e):
        log.released.append(u)
        claimed.discard((u, a, e))

    async def note(u, ok):
        log.results.append((u, ok))
        return False
    monkeypatch.setattr(al, "dm_followers", dm_followers)
    monkeypatch.setattr(al, "get_prefs_many", get_many)
    monkeypatch.setattr(al, "claim_alert", claim)
    monkeypatch.setattr(al, "release_alert", release)
    monkeypatch.setattr(al, "note_dm_result", note)
    users = users or {u: User(u) for u in recipients}

    async def fetch_user(uid):
        return users[uid]
    bot = SimpleNamespace(get_user=lambda uid: users.get(uid), fetch_user=fetch_user, get_guild=lambda g: None, wait_until_ready=None)
    return aw.Worker(bot, None, post_gap=0), users, log, claimed


def test_dm_goes_to_opted_in_people_once_and_a_second_pass_sends_nothing(monkeypatch):
    now = int(time.time())
    w, users, log, _ = dm_worker(monkeypatch, [1, 2])
    assert run(w._dm_alerts([P(at=now - 20)], now)) == (2, False)
    assert [len(u.sent) for u in users.values()] == [1, 1] and log.results == [(1, True), (2, True)]
    assert run(w._dm_alerts([P(at=now - 20)], now)) == (0, False)                   # deduped across passes/restarts
    assert [len(u.sent) for u in users.values()] == [1, 1]
    am = users[1].sent[0]["allowed_mentions"]
    assert am.everyone is False and not am.users and not am.roles


def test_quiet_hours_hold_a_dm_without_claiming_it(monkeypatch):
    night = 2 * 3600 + 5 * 86400                                                    # 02:00 UTC
    w, users, _, claimed = dm_worker(monkeypatch, [1, 2], prefs={1: al.Prefs("UTC+0", 22, 7)})
    assert run(w._dm_alerts([P(at=night - 20)], night))[0] == 1
    assert users[1].sent == [] and len(users[2].sent) == 1 and (1, 7, 2) not in claimed   # still free to send once the quiet hours end


def test_closed_dms_are_counted_and_keep_the_claim_but_other_errors_release_it(monkeypatch):
    now = int(time.time())
    resp = SimpleNamespace(status=403, reason="x")
    users = {1: User(1, discord.Forbidden(resp, "closed")), 2: User(2, discord.HTTPException(SimpleNamespace(status=500, reason="x"), "boom"))}
    w, _, log, claimed = dm_worker(monkeypatch, [1, 2], users=users)
    assert run(w._dm_alerts([P(at=now - 20)], now)) == (0, False)
    assert log.results == [(1, False)] and (1, 7, 2) in claimed                      # closed DM: counted, never retried for this episode
    assert log.released == [2] and (2, 7, 2) not in claimed                           # server error: released for the next pass


def test_dm_queue_is_bounded_per_pass_and_reports_a_backlog(monkeypatch):
    monkeypatch.setattr(al, "MAX_DMS_PER_CYCLE", 3)
    now = int(time.time())
    w, users, _, _ = dm_worker(monkeypatch, list(range(1, 8)))
    assert run(w._dm_alerts([P(at=now - 20)], now)) == (3, True)
    assert run(w._dm_alerts([P(at=now - 20)], now)) == (3, True)                      # the next short pass continues where it stopped
    assert sum(len(u.sent) for u in users.values()) == 6


def test_old_episodes_are_never_dmed(monkeypatch):
    now = int(time.time())
    w, users, _, _ = dm_worker(monkeypatch, [1])
    assert run(w._dm_alerts([P(at=now - aw.GRACE_SECONDS - 60), P(8, at=now + 600)], now)) == (0, False)
    assert users[1].sent == []


def lifecycle(monkeypatch, dm_work, guilds):
    calls = SimpleNamespace(provider=0)

    async def enabled(clone_id=None):
        return guilds

    async def airing():
        calls.provider += 1
        return [], True

    async def has_dm():
        return dm_work
    monkeypatch.setattr(af, "enabled_guilds", enabled)
    monkeypatch.setattr(af, "airing_soon", airing)
    monkeypatch.setattr(al, "has_dm_followers", has_dm)
    return aw.Worker(SimpleNamespace(get_guild=lambda g: None, wait_until_ready=None), None, post_gap=0), calls


def test_a_dm_only_follower_is_work_and_no_followers_still_means_no_requests(monkeypatch):
    w, calls = lifecycle(monkeypatch, True, [])
    wait = run(w.cycle())
    assert wait is not None and calls.provider == 1 and w.state == aw.SLEEPING          # someone has a DM alert: the worker runs even with no feeds on
    w2, calls2 = lifecycle(monkeypatch, False, [])
    assert run(w2.cycle()) is None and calls2.provider == 0                              # nothing to do: no timer, no request


def test_a_database_error_reading_dm_alerts_backs_off_when_idle_but_never_blocks_server_posts(monkeypatch):
    async def boom():
        raise RuntimeError("db")
    w, calls = lifecycle(monkeypatch, False, [])
    monkeypatch.setattr(al, "has_dm_followers", boom)
    assert run(w.cycle()) > 0 and w.state == aw.BACKOFF
    w2, calls2 = lifecycle(monkeypatch, False, [af.FeedConfig(1, None, True, 100, None, None, "both", 60, __import__("datetime").datetime.now(__import__("datetime").timezone.utc))])
    monkeypatch.setattr(al, "has_dm_followers", boom)
    monkeypatch.setattr(w2.bot, "get_guild", lambda g: object(), raising=False)
    assert run(w2.cycle()) is not None and calls2.provider == 0      # spawns need no airing lookup


def test_migration_and_schema_are_wired_and_additive():
    root = __import__("pathlib").Path(__file__).resolve().parents[2]
    sql = (root / "database/migrations/074_anime_alerts.sql").read_text()
    assert "alert_mode TEXT NOT NULL DEFAULT 'mention'" in sql and "anime_user_prefs" in sql and "anime_alerts_sent" in sql
    assert "DROP " not in sql.upper() and "DELETE " not in sql.upper()
    db = (root / "database.py").read_text()
    assert "074_anime_alerts.sql" in db and 'SCHEMA_VERSION = "' in db
