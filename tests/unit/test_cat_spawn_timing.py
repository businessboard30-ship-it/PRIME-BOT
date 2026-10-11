"""Cat spawn timing: a server picks the UTC hours cats may appear; the daily amount is unchanged."""
import asyncio
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from discord_bot.cogs import _views_cat_setup as wiz
from discord_bot.cogs import cat
from modules import cat_setup as cs


def at(h, m=0, d=11):
    return datetime(2026, 10, d, h, m, tzinfo=timezone.utc)


def test_window_length_and_defaults():
    assert cs.window_hours(0, 24) == 24 and cs.window_hours(5, 5) == 24
    assert cs.window_hours(8, 20) == 12 and cs.window_hours(22, 6) == 8
    assert cs.window_hours("x", None) == 24            # junk = all day


def test_in_window_normal_and_overnight():
    assert cs.in_window(8, 20, at(8)) and cs.in_window(8, 20, at(19, 59)) and not cs.in_window(8, 20, at(20))
    assert not cs.in_window(8, 20, at(7, 59))
    assert cs.in_window(22, 6, at(23)) and cs.in_window(22, 6, at(2)) and not cs.in_window(22, 6, at(12))
    assert cs.in_window(0, 24, at(13))


def test_next_window_start():
    assert cs.next_window_start(8, 20, at(3)) == at(8)
    assert cs.next_window_start(8, 20, at(21)) == at(8, d=12)
    assert cs.next_window_start(8, 20, at(10)) == at(10)          # already open
    assert cs.next_window_start(22, 6, at(12)) == at(22)


def test_daily_amount_unchanged_but_gaps_shrink_inside_a_shorter_window():
    d = date(2026, 10, 11)
    full = cs.CatConfig(guild_id=1, enabled=True, channel_ids=(5,))
    half = cs.replace(full, active_start=8, active_end=20)
    assert full.effective(False, d)[1] == 86400 / cs.daily_target(1, None, cs.FREE, d)
    assert abs(half.effective(False, d)[1] * 2 - full.effective(False, d)[1]) < 1e-6
    n = 12 * 3600 / half.effective(False, d)[1]
    assert cs.FREE.daily_min - 1 <= n <= cs.FREE.daily_max + 1


def test_from_row_defaults_and_clamps():
    base = dict(guild_id=1, clone_id=None, enabled=True, channel_ids=[5], interval_minutes=720,
                next_at=None, last_post_at=None, pace_percent=100)

    class Row(dict):
        pass
    old = cs.CatConfig.from_row(Row(base))                       # row without the new columns
    assert (old.active_start, old.active_end) == (0, 24)
    new = cs.CatConfig.from_row(Row(dict(base, active_start=9, active_end=17)))
    assert (new.active_start, new.active_end) == (9, 17)
    junk = cs.CatConfig.from_row(Row(dict(base, active_start=99, active_end=-3)))
    assert (junk.active_start, junk.active_end) == (0, 24)


def test_changing_hours_reschedules():
    old = cs.CatConfig(guild_id=1, enabled=True, channel_ids=(5,), next_at=at(12))
    new = cs.after_change(old, cs.replace(old, active_start=8))
    assert new.next_at is None
    assert cs.after_change(old, cs.replace(old, pace_percent=50)).next_at == at(12)


def test_wizard_shows_and_sets_hours():
    cfg = cs.CatConfig(guild_id=1, enabled=True, channel_ids=(5,), active_start=8, active_end=20)
    assert "Spawn hours" in wiz.render_embed(cfg, False).description
    assert "all day" in wiz.render_embed(cs.CatConfig(guild_id=1), False).description
    view = wiz.CatSetupView(MagicMock(), SimpleNamespace(id=1), None, 42, cfg, False)
    assert [o.value for o in view.start_hour.options if o.default] == ["8"]
    assert [o.value for o in view.end_hour.options if o.default] == ["20"]
    assert len(view.start_hour.options) == 24 and len(view.end_hour.options) == 24
    assert all(len({c.row for c in [i]}) for i in view.children)
    rows = {}
    for c in view.children:
        rows.setdefault(c.row, []).append(c)
    assert max(rows) <= 4


def test_spawner_posts_nothing_outside_hours_and_defers(monkeypatch):
    c = cat.CatCog(SimpleNamespace(clone_id=None, get_guild=lambda g: SimpleNamespace(id=g)))
    closed = cs.CatConfig(guild_id=1, enabled=True, channel_ids=(5,), active_start=0, active_end=1)
    closed = cs.replace(closed, active_start=(datetime.now(timezone.utc).hour + 5) % 24,
                        active_end=(datetime.now(timezone.utc).hour + 6) % 24 or 24)
    deferred, claimed, posted = [], [], []
    monkeypatch.setattr(cs, "due", AsyncMock(return_value=[closed]))
    monkeypatch.setattr(cs, "defer", AsyncMock(side_effect=lambda g, c_, until: deferred.append(until)))
    monkeypatch.setattr(cs, "claim", AsyncMock(side_effect=lambda *a: claimed.append(a) or True))
    monkeypatch.setattr(cat, "post_cat", AsyncMock(side_effect=lambda *a: posted.append(a) or True))
    assert asyncio.run(c.spawn_due()) == 0
    assert deferred and not claimed and not posted
    assert cs.in_window(closed.active_start, closed.active_end, deferred[0])
