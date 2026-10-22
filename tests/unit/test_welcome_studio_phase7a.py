"""Welcome Card Studio Phase 7a: scheduled designs, data layer + render-time resolver (no wizard UI yet)."""
import asyncio
import datetime as dt
import json
import re
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

import database as dbmod
from modules import welcome_schedule as sch
from modules import welcome_studio as ws

ROOT = Path(__file__).resolve().parents[2]
D = dt.date


def run(c):
    return asyncio.run(c)


# ---------- parsing what an admin types ----------

@pytest.mark.parametrize("text,expect", [
    ("25 Oct", (D(2000, 10, 25), True)), ("Oct 25", (D(2000, 10, 25), True)), ("25th October", (D(2000, 10, 25), True)),
    ("october 5", (D(2000, 10, 5), True)), ("10-25", (D(2000, 10, 25), True)), ("10/25", (D(2000, 10, 25), True)),
    ("2026-10-25", (D(2026, 10, 25), False)), ("2026/1/5", (D(2026, 1, 5), False)), ("  25   Oct ", (D(2000, 10, 25), True)),
    ("29 Feb", (D(2000, 2, 29), True)), ("1 Nov,", (D(2000, 11, 1), True)),
])
def test_parse_when_understands_the_common_ways_to_write_a_day(text, expect):
    assert sch.parse_when(text) == expect


@pytest.mark.parametrize("text", ["", None, "   ", "31 Feb", "13-40", "2026-02-30", "someday", "25 Octopus!", "0 Oct", 5, "25"])
def test_parse_when_rejects_junk_without_raising(text):
    assert sch.parse_when(text) is None


def test_build_range_statuses():
    assert sch.build_range("25 Oct", "1 Nov") == ("ok", D(2000, 10, 25), D(2000, 11, 1), True)
    assert sch.build_range("2026-10-25", "2026-11-01") == ("ok", D(2026, 10, 25), D(2026, 11, 1), False)
    assert sch.build_range("25 Oct", "") == ("ok", D(2000, 10, 25), D(2000, 10, 25), True)          # blank end = one day
    assert sch.build_range("nope", "1 Nov")[0] == "bad_from" and sch.build_range("25 Oct", "nope")[0] == "bad_to"
    assert sch.build_range("2026-10-25", "1 Nov")[0] == "mixed"
    assert sch.build_range("2026-11-02", "2026-10-25")[0] == "backwards"
    assert sch.build_range("20 Dec", "5 Jan")[0] == "ok"                                              # yearly may wrap New Year


# ---------- is the schedule on today? ----------

def row(a, b, yearly, i=1):
    return {"id": i, "sched_from": a, "sched_to": b, "sched_yearly": yearly}


def test_one_off_range_is_inclusive_on_both_ends():
    r = row(D(2026, 10, 25), D(2026, 11, 1), False)
    assert [sch.is_active(r, D(2026, 10, x)) for x in (24, 25, 31)] == [False, True, True]
    assert sch.is_active(r, D(2026, 11, 1)) and not sch.is_active(r, D(2026, 11, 2))
    assert not sch.is_active(r, D(2027, 10, 28))                                                      # one-off: next year is off


def test_yearly_repeats_every_year_and_ignores_the_stored_year():
    r = row(D(2000, 10, 25), D(2000, 11, 1), True)
    assert sch.is_active(r, D(2026, 10, 30)) and sch.is_active(r, D(2031, 11, 1)) and not sch.is_active(r, D(2026, 11, 2))


def test_yearly_range_can_wrap_over_new_year():
    r = row(D(2000, 12, 20), D(2000, 1, 5), True)
    for day, want in [(D(2026, 12, 19), False), (D(2026, 12, 20), True), (D(2027, 1, 5), True), (D(2027, 1, 6), False), (D(2027, 6, 1), False)]:
        assert sch.is_active(r, day) is want


def test_leap_day_is_a_valid_yearly_day():
    r = row(D(2000, 2, 29), D(2000, 2, 29), True)
    assert sch.is_active(r, D(2028, 2, 29)) and not sch.is_active(r, D(2027, 3, 1))


def test_unscheduled_or_broken_rows_are_never_active():
    for bad in ({}, {"id": 1}, {"sched_from": None, "sched_to": None}, {"sched_from": "x", "sched_to": "y"}, None):
        assert sch.is_active(bad, D(2026, 10, 25)) is False


def test_datetime_values_from_the_database_are_accepted():
    r = row(dt.datetime(2026, 10, 25, 9, 0), dt.datetime(2026, 10, 26, 9, 0), False)
    assert sch.is_active(r, D(2026, 10, 26))


# ---------- which design wins ----------

def test_pick_none_when_nothing_covers_today():
    assert sch.pick_scheduled([row(D(2026, 1, 1), D(2026, 1, 2), False)], D(2026, 10, 25)) is None
    assert sch.pick_scheduled([], D(2026, 10, 25)) is None and sch.pick_scheduled(None, D(2026, 10, 25)) is None


def test_one_off_beats_yearly_then_later_start_then_lowest_id():
    yearly = row(D(2000, 10, 1), D(2000, 10, 31), True, 1)
    once = row(D(2026, 10, 20), D(2026, 10, 30), False, 2)
    assert sch.pick_scheduled([yearly, once], D(2026, 10, 25))["id"] == 2
    a = row(D(2000, 10, 1), D(2000, 10, 31), True, 3)
    b = row(D(2000, 10, 20), D(2000, 10, 31), True, 4)
    assert sch.pick_scheduled([a, b], D(2026, 10, 25))["id"] == 4                                      # narrower, more recent window
    c = row(D(2000, 10, 20), D(2000, 10, 31), True, 6)
    assert sch.pick_scheduled([c, b], D(2026, 10, 25))["id"] == 4                                      # tie -> lowest id


def test_describe_in_plain_words():
    assert sch.describe(row(D(2000, 10, 25), D(2000, 11, 1), True)) == "25 Oct to 1 Nov every year"
    assert sch.describe(row(D(2026, 10, 25), D(2026, 11, 1), False)) == "25 Oct 2026 to 1 Nov 2026"
    assert sch.describe(row(D(2000, 12, 25), D(2000, 12, 25), True)) == "25 Dec every year"
    assert sch.describe({}) is None


# ---------- the resolver and the render helpers ----------

TODAY = D(2026, 10, 28)
HALLOWEEN = {"id": 7, "name": "Halloween", "options_json": json.dumps({"font": "horror", "frame": "thick", "text_color": "orange"}),
             "avatar_shape": "rounded_square" if "rounded_square" in __import__("modules.welcome_card", fromlist=["x"]).AVATAR_SHAPES else "circle",
             "sched_from": D(2000, 10, 25), "sched_to": D(2000, 11, 1), "sched_yearly": True}
CFG = {"guild_id": 5, "clone_id": None, "design_schedule_on": True, "avatar_shape": "circle",
       "ultra_card_json": json.dumps({"font": "tall"})}


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    ws.forget_schedule_cache()
    monkeypatch.setattr(ws, "studio_unlocked", AsyncMock(return_value=True))
    monkeypatch.setattr(dbmod.db, "design_list", AsyncMock(return_value=[HALLOWEEN]), raising=False)
    yield
    ws.forget_schedule_cache()


def test_flag_off_costs_nothing_and_changes_nothing():
    cfg = dict(CFG, design_schedule_on=False)
    assert run(ws.scheduled_design(cfg, TODAY)) is None
    ws.studio_unlocked.assert_not_awaited()
    dbmod.db.design_list.assert_not_awaited()
    assert run(ws.options_for_render(cfg)) == cfg["ultra_card_json"]                                  # byte-identical to before
    assert run(ws.avatar_shape_for_render(cfg)) == "circle"


def test_premium_server_inside_the_window_gets_the_design(monkeypatch):
    got = run(ws.scheduled_design(CFG, TODAY))
    assert got["id"] == 7
    monkeypatch.setattr(ws, "_today_utc", lambda: TODAY)
    opts = json.loads(run(ws.options_for_render(CFG)))
    assert opts["font"] == "horror" and opts["frame"] == "thick"
    assert run(ws.avatar_shape_for_render(CFG)) == HALLOWEEN["avatar_shape"]


def test_outside_the_window_the_live_look_is_used():
    assert run(ws.scheduled_design(CFG, D(2026, 12, 1))) is None


def test_without_premium_the_schedule_is_ignored_and_nothing_is_deleted():
    ws.studio_unlocked.return_value = False
    assert run(ws.scheduled_design(CFG, TODAY)) is None
    dbmod.db.design_list.assert_not_awaited()


def test_lookup_errors_fall_back_to_the_live_look():
    dbmod.db.design_list.side_effect = RuntimeError("down")
    assert run(ws.scheduled_design(CFG, TODAY)) is None
    ws.forget_schedule_cache()
    ws.studio_unlocked.side_effect = RuntimeError("down")
    assert run(ws.scheduled_design(CFG, TODAY)) is None
    assert run(ws.options_for_render(CFG)) is not None


def test_one_lookup_serves_a_burst_of_joins_and_forget_clears_it():
    for _ in range(25):
        assert run(ws.scheduled_design(CFG, TODAY))["id"] == 7
    assert dbmod.db.design_list.await_count == 1
    ws.forget_schedule_cache(5, None)
    run(ws.scheduled_design(CFG, TODAY))
    assert dbmod.db.design_list.await_count == 2
    run(ws.scheduled_design(CFG, D(2026, 10, 29)))                                                    # a new day is a new answer
    assert dbmod.db.design_list.await_count == 3


def test_clones_do_not_share_an_answer():
    run(ws.scheduled_design(CFG, TODAY))
    run(ws.scheduled_design(dict(CFG, clone_id=9), TODAY))
    assert dbmod.db.design_list.await_count == 2


def test_a_corrupt_stored_design_still_renders_with_defaults():
    dbmod.db.design_list.return_value = [dict(HALLOWEEN, options_json="not json{", avatar_shape="nope")]
    got = run(ws.options_for_render(CFG))
    assert isinstance(got, str)
    assert json.loads(got)                                                                            # validated defaults, never a crash
    assert run(ws.avatar_shape_for_render(CFG)) in __import__("modules.welcome_card", fromlist=["x"]).AVATAR_SHAPES


# ---------- migration, wiring, database methods ----------

def test_migration_is_additive_registered_and_version_bumped():
    sql = (ROOT / "database/migrations/083_welcome_design_schedule.sql").read_text()
    for col in ("sched_from", "sched_to", "sched_yearly", "design_schedule_on"):
        assert f"ADD COLUMN IF NOT EXISTS {col}" in sql
    assert not re.search(r"\bDROP\b|\bTRUNCATE\b|\bDELETE\b", sql, re.I)
    src = (ROOT / "database.py").read_text()
    assert "083_welcome_design_schedule.sql" in src
    assert int(re.search(r'^SCHEMA_VERSION = "(\d+)"', src, re.M).group(1)) >= 132


class Conn:
    def __init__(self, s):
        self.s = s

    def transaction(self):
        class T:
            async def __aenter__(self_):
                return None

            async def __aexit__(self_, *a):
                return False
        return T()

    async def execute(self, sql, *a):
        self.s["sql"].append(sql)
        if sql.startswith("UPDATE discord_welcome_designs SET sched_from = $1"):
            f, t, y, did, g, k = a
            hit = [x for x in self.s["rows"] if x["id"] == did and x["guild_id"] == g and x["clone_key"] == k]
            for x in hit:
                x.update(sched_from=f, sched_to=t, sched_yearly=y)
            return "UPDATE %d" % len(hit)
        if sql.startswith("UPDATE discord_welcome_designs SET sched_from = NULL"):
            did, g, k = a
            hit = [x for x in self.s["rows"] if x["id"] == did and x["guild_id"] == g and x["clone_key"] == k]
            for x in hit:
                x.update(sched_from=None, sched_to=None, sched_yearly=False)
            return "UPDATE %d" % len(hit)
        if sql.startswith("UPDATE discord_welcome_config SET design_schedule_on"):
            g, cid, k = a
            self.s["flag"][(g, k)] = any(x["guild_id"] == g and x["clone_key"] == k and x.get("sched_from") for x in self.s["rows"])
            return "UPDATE 1"
        if sql.startswith("UPDATE discord_welcome_config SET active_design_id = $3"):
            self.s["active"] = a
            return "UPDATE 1"
        raise AssertionError(sql)


@pytest.fixture
def d(monkeypatch):
    store = {"rows": [{"id": 1, "guild_id": 1, "clone_key": -1, "name": "A"}, {"id": 2, "guild_id": 2, "clone_key": -1, "name": "B"}],
             "flag": {}, "sql": [], "active": None}

    class Pool:
        def acquire(self_):
            c = Conn(store)

            class A:
                async def __aenter__(s):
                    return c

                async def __aexit__(s, *a):
                    return False
            return A()

    async def gp():
        return Pool()
    monkeypatch.setattr(dbmod, "get_pool", gp)
    return dbmod.Database.__new__(dbmod.Database), store


def test_set_and_clear_schedule_keep_the_flag_in_step(d):
    db, store = d
    assert run(db.design_set_schedule(1, None, 1, D(2000, 10, 25), D(2000, 11, 1), True)) is True
    assert store["flag"][(1, -1)] is True and store["rows"][0]["sched_yearly"] is True
    assert run(db.design_clear_schedule(1, None, 1)) is True
    assert store["flag"][(1, -1)] is False and store["rows"][0]["sched_from"] is None


def test_schedule_is_scoped_to_the_server_and_missing_designs_return_false(d):
    db, store = d
    assert run(db.design_set_schedule(2, None, 1, D(2000, 10, 25), D(2000, 11, 1), True)) is False   # design 1 is another server's
    assert run(db.design_set_schedule(1, None, 99, D(2000, 10, 25), D(2000, 11, 1), True)) is False
    assert run(db.design_clear_schedule(1, None, 99)) is False
    assert "sched_from" not in store["rows"][1]
    assert not store["flag"]


def test_set_active_design(d):
    db, store = d
    assert run(db.set_active_design(1, None, 4)) is True and store["active"] == (1, None, 4)


def test_methods_never_raise_on_database_errors(monkeypatch):
    async def boom():
        raise RuntimeError("down")
    monkeypatch.setattr(dbmod, "get_pool", boom)
    db = dbmod.Database.__new__(dbmod.Database)
    assert run(db.design_set_schedule(1, None, 1, D(2000, 1, 1), D(2000, 1, 2), True)) is False
    assert run(db.design_clear_schedule(1, None, 1)) is False and run(db.set_active_design(1, None, 1)) is False


def test_schedule_never_touches_pictures_or_the_live_card():
    src = (ROOT / "database.py").read_text()
    body = src[src.index("async def design_set_schedule"):src.index("async def gallery_count_recent")]
    assert "discord_welcome_backgrounds" not in body and "ultra_card_json" not in body


def test_default_config_has_the_flag_off():
    src = (ROOT / "database.py").read_text()
    assert '"design_schedule_on": False' in src
