"""Anime feed D2 (Alert me): quiet hours, persistent button, worker lifecycle with DM alerts, DM dedupe/failure rules, panel rules. No network."""
import asyncio
import re
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import discord
import pytest

from discord_bot.cogs import _views_anime_feed as vf
from modules import anime_alerts as aa
from modules import anime_feed as af
from modules import anime_feed_worker as aw


def run(c):
    return asyncio.run(c)

@pytest.fixture(autouse=True)
def _no_donghua_network(monkeypatch):
    """The donghua schedule is a second provider call; these tests stay offline."""
    async def none():
        return [], True
    monkeypatch.setattr(af, "donghua_soon", none)



def P(i, ep=1, at=0, title="Show"):
    return af.Preview(i, title, ep, at, "syn", None, f"https://anilist.co/anime/{i}")


# ── pure rules ──────────────────────────────────────────────────────────

def test_quiet_hours_window_wraps_past_midnight_and_uses_the_local_timezone():
    utc_23 = 23 * 3600
    assert aa.in_quiet(utc_23, 0, 22, 7) is True
    assert aa.in_quiet(utc_23 + 9 * 3600, 0, 22, 7) is False             # 08:00 UTC
    assert aa.in_quiet(utc_23, 120, 22, 7) is True                       # 01:00 local
    assert aa.in_quiet(14 * 3600, 0, 13, 15) is True                     # a plain same-day window
    assert aa.in_quiet(15 * 3600, 0, 13, 15) is False                    # end is exclusive
    assert aa.in_quiet(utc_23, 0, None, None) is False and aa.in_quiet(utc_23, 0, 5, 5) is False


def test_modes_and_choice_lists_are_valid():
    assert aa.valid_mode("dm") and not aa.valid_mode("everyone")
    assert len(aa.TZ_CHOICES) <= 25 and len(aa.QUIET_CHOICES) + 1 <= 25      # Discord selects hold at most 25 options
    assert len({m for _, m in aa.TZ_CHOICES}) == len(aa.TZ_CHOICES)


# ── the button survives a restart ───────────────────────────────────────

def test_alert_button_is_on_every_card_with_a_persistent_id_that_rebuilds():
    kids = list(vf.post_view(P(42, at=2_000_000_000)).walk_children())
    ids = [getattr(c, "custom_id", None) for c in kids if getattr(c, "custom_id", None)]
    assert "anime_alert:42" in ids and all(len(i) <= 100 for i in ids)
    assert vf._AlertButton in vf.DYNAMIC_ITEMS
    m = re.match(vf._AlertButton.__discord_ui_compiled_template__.pattern, "anime_alert:42")
    item = run(vf._AlertButton.from_custom_id(None, None, m))
    assert item.anime_id == 42 and item.item.label == "Alert me" and item.custom_id == "anime_alert:42"


# ── worker lifecycle ────────────────────────────────────────────────────

def worker(monkeypatch, guilds, dm_wanted, items=(), dm_ok=True):
    calls = SimpleNamespace(provider=0)

    async def enabled(clone_id=None):
        return guilds

    async def any_dm():
        return dm_wanted

    async def airing():
        calls.provider += 1
        return list(items), True
    monkeypatch.setattr(af, "enabled_guilds", enabled)
    monkeypatch.setattr(aa, "any_dm_alerts", any_dm)
    monkeypatch.setattr(af, "airing_soon", airing)
    return aw.Worker(SimpleNamespace(get_guild=lambda g: object()), None, post_gap=0, dm_gap=0), calls


def test_no_feeds_and_no_dm_alerts_means_idle_with_no_requests(monkeypatch):
    w, calls = worker(monkeypatch, [], False)
    assert run(w.cycle()) is None and calls.provider == 0


def test_a_dm_only_follower_keeps_the_worker_awake_without_any_feed(monkeypatch):
    now = int(time.time())
    w, calls = worker(monkeypatch, [], True, [P(1, at=now + 600)])
    wait = run(w.cycle())
    assert wait is not None and calls.provider == 1


def test_dm_delivery_claims_first_skips_quiet_hours_and_never_double_sends(monkeypatch):
    now = int(time.time())
    p = P(7, ep=3, at=now - 60)
    claimed, sent, quiet_hour = set(), [], aa.local_hour(now, 0)
    targets = [aa.Target(1), aa.Target(2), aa.Target(3, 0, quiet_hour, (quiet_hour + 2) % 24)]     # 3 is in quiet hours

    async def dm_targets(anime_id, airing_at):
        return targets

    async def claim_dm(u, a, e):
        if (u, a, e) in claimed:
            return False
        claimed.add((u, a, e))
        return True

    async def note(u, ok):
        return False

    class User:
        def __init__(self, uid): self.uid = uid
        async def send(self, **kw): sent.append(self.uid)

    monkeypatch.setattr(aa, "dm_targets", dm_targets)
    monkeypatch.setattr(aa, "claim_dm", claim_dm)
    monkeypatch.setattr(aa, "note_dm_result", note)
    w = aw.Worker(SimpleNamespace(get_user=lambda u: User(u)), None, post_gap=0, dm_gap=0)
    assert run(w._deliver_dms([p], now)) == 2 and sorted(sent) == [1, 2]
    assert run(w._deliver_dms([p], now)) == 0 and sorted(sent) == [1, 2]            # a second pass (or a restart) sends nothing again


def test_dm_pass_is_bounded_per_cycle(monkeypatch):
    now = int(time.time())
    items = [P(i, at=now - 60) for i in range(1, 6)]

    async def dm_targets(anime_id, airing_at):
        return [aa.Target(u) for u in range(100, 110)]

    async def claim_dm(*a): return True
    async def note(u, ok): return False

    class User:
        async def send(self, **kw): pass

    monkeypatch.setattr(aa, "dm_targets", dm_targets)
    monkeypatch.setattr(aa, "claim_dm", claim_dm)
    monkeypatch.setattr(aa, "note_dm_result", note)
    monkeypatch.setattr(aw, "MAX_DMS_PER_CYCLE", 12)
    w = aw.Worker(SimpleNamespace(get_user=lambda u: User()), None, post_gap=0, dm_gap=0)
    assert run(w._deliver_dms(items, now)) == 12


def test_closed_dm_is_recorded_and_a_temporary_error_releases_the_claim(monkeypatch):
    results, released = [], []

    async def note(u, ok):
        results.append((u, ok))
        return False

    async def release(u, a, e):
        released.append((u, a, e))
    monkeypatch.setattr(aa, "note_dm_result", note)
    monkeypatch.setattr(aa, "release_dm", release)
    resp = SimpleNamespace(status=403, reason="x")

    class Closed:
        async def send(self, **kw): raise discord.Forbidden(resp, "closed")

    class Flaky:
        async def send(self, **kw): raise discord.HTTPException(SimpleNamespace(status=500, reason="x"), "boom")

    w = aw.Worker(SimpleNamespace(get_user=lambda u: Closed()), None, dm_gap=0)
    assert run(w._dm(5, P(1, ep=2))) is False and results == [(5, False)] and released == []
    w = aw.Worker(SimpleNamespace(get_user=lambda u: Flaky()), None, dm_gap=0)
    assert run(w._dm(6, P(1, ep=2))) is False and released == [(6, 1, 2)]


# ── the panel ───────────────────────────────────────────────────────────

def test_panel_is_private_to_its_owner_and_shows_extras_only_with_plus():
    panel = vf.AlertPanel(10, 1, "Show", None, aa.MENTION, aa.Prefs(), True)
    selects = [c for c in panel.walk_children() if isinstance(c, discord.ui.Select)]
    assert len(selects) == 3
    free = vf.AlertPanel(10, 1, "Show", None, None, aa.Prefs(), False)
    assert len([c for c in free.walk_children() if isinstance(c, discord.ui.Select)]) == 1
    sent = []

    class R:
        async def send_message(self, text, **kw): sent.append((text, kw))
    inter = SimpleNamespace(user=SimpleNamespace(id=99), response=R())
    assert run(panel.interaction_check(inter)) is False and sent[0][1]["ephemeral"] is True
    assert run(panel.interaction_check(SimpleNamespace(user=SimpleNamespace(id=10), response=R()))) is True


def test_alert_button_defers_before_database_work(monkeypatch):
    order = []

    async def opener(*a, **k):
        order.append("db")
        return vf.AlertPanel(1, 5, "Show", None, None, aa.Prefs(), False)
    monkeypatch.setattr(vf.AlertPanel, "open", opener)
    monkeypatch.setattr(af, "cooldown_left", lambda *a: 0)

    class R:
        def is_done(self): return True
        async def defer(self, **kw): order.append("defer")

    class F:
        async def send(self, **kw): order.append("send")
    inter = SimpleNamespace(user=SimpleNamespace(id=1), guild=None, message=None, response=R(), followup=F())
    run(vf._AlertButton(5).callback(inter))
    assert order == ["defer", "db", "send"]
