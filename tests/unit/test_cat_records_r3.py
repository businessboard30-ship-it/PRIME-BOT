"""Cat fight records and the Veteran bonus (R3): formula and cap, record written with the settlement, rollback,
replay equality with a bonus, old snaps still replay. Play game only. Reuses the Phase 3 in-memory fake database."""
import asyncio
import copy
import json
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from modules import cat_arena as ar
from modules import cat_battle as cb
from modules import cat_records as rec
from tests.unit.test_cat_arena_phase3 import FakeConn

ROOT = Path(__file__).resolve().parents[2]


def run(c):
    return asyncio.run(c)


class RecConn(FakeConn):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.records = {}

    @asynccontextmanager
    async def transaction(self):
        snap = copy.deepcopy((self.wallets, self.arena, self.daily, self.effects, self.fights, self.cats, self.records))
        try:
            yield
        except BaseException:
            (self.wallets, self.arena, self.daily, self.effects, self.fights, self.cats, self.records) = snap
            raise

    async def fetch(self, sql, *a):
        if "FROM cat_records" in sql:
            return [dict(self.records[i]) for i in sorted(set(a[0])) if i in self.records]
        return await super().fetch(sql, *a)

    async def execute(self, sql, *a):
        if sql.startswith("INSERT INTO cat_records"):
            r = self.records.setdefault(a[0], dict(cat_id=a[0], fights=0, wins=0, losses=0, kos=0, streak=0, best_streak=0))
            r["fights"] += 1
            if "wins = cat_records.wins + 1" in sql:
                r["wins"] += 1
                r["kos"] += a[1]
                r["streak"] += 1
                r["best_streak"] = max(r["best_streak"], r["streak"])
            else:
                r["losses"] += 1
                r["streak"] = 0
            return
        return await super().execute(sql, *a)


@pytest.fixture
def db(monkeypatch):
    def make(**kw):
        conn = RecConn(**kw)

        class Pool:
            @asynccontextmanager
            async def acquire(self):
                yield conn

        async def get_pool():
            return Pool()
        monkeypatch.setattr(ar, "_pool", get_pool)
        monkeypatch.setattr(rec, "_pool", get_pool)
        return conn
    return make


# ---------- formula ----------
def test_bonus_formula_and_cap():
    assert [rec.bonus_pct(n) for n in (0, 9, 10, 19, 20, 49, 50, 500)] == [0, 0, 1, 1, 2, 4, 5, 5]
    assert rec.VET_CAP_PCT == 5 and rec.bonus_pct(-3) == 0


def test_next_step_and_line():
    assert rec.Record(1, fights=13).to_next() == 7 and rec.Record(1, fights=50).to_next() is None
    assert "Veteran +2%" in rec.line(rec.Record(1, fights=20, wins=12, losses=8, kos=3, best_streak=4))
    assert "Veteran" not in rec.line(rec.Record(1, fights=3))


def test_a_bonus_helps_and_changes_power_but_stays_small():
    base = cb.Fighter("A", 6, 5, 5, 4, skills=("pounce",))
    vet = cb.Fighter("A", 6, 5, 5, 4, skills=("pounce",), vet_pct=5)
    assert vet.power > base.power and vet.power <= int(round(base.power * 1.05)) + 1
    foe = cb.Fighter("B", 6, 5, 5, 4, skills=("pounce",))
    assert cb.win_share(vet, foe, runs=400) > cb.win_share(base, foe, runs=400) - 0.02


# ---------- settlement writes the record ----------
def test_record_is_written_with_the_settlement(db):
    conn = db()
    res = run(ar.accept(1, 1, None, 11, seed=7))
    assert res.status == "ok"
    win_cat = 11 if res.winner_id == 1 else 10
    lose_cat = 10 if win_cat == 11 else 11
    w, l = conn.records[win_cat], conn.records[lose_cat]
    assert (w["fights"], w["wins"], w["losses"], w["streak"]) == (1, 1, 0, 1)
    assert (l["fights"], l["wins"], l["losses"], l["streak"]) == (1, 0, 1, 0)
    assert w["kos"] == (1 if res.result.ended_by == "ko" else 0)


def test_a_failed_settlement_leaves_no_record(db):
    conn = db()
    conn.fail_at = "take"
    try:
        run(ar.accept(1, 1, None, 11, seed=7))
    except Exception:
        pass
    assert conn.records == {} and conn.fights[1]["status"] == "pending"


def test_record_follows_an_existing_streak(db):
    conn = db()
    conn.records[11] = dict(cat_id=11, fights=9, wins=5, losses=4, kos=1, streak=4, best_streak=4)
    conn.records[10] = dict(cat_id=10, fights=9, wins=4, losses=5, kos=0, streak=0, best_streak=2)
    res = run(ar.accept(1, 1, None, 11, seed=7))
    r11, r10 = conn.records[11], conn.records[10]
    assert r11["fights"] == 10 and r10["fights"] == 10
    if res.winner_id == 1:
        assert r11["streak"] == 5 and r11["best_streak"] == 5 and r10["streak"] == 0
    else:
        assert r11["streak"] == 0 and r11["best_streak"] == 4 and r10["streak"] == 1


# ---------- the bonus applies in the real fight, and replays match ----------
def test_a_veteran_fights_with_the_bonus_and_the_replay_is_identical(db):
    conn = db()
    conn.records[11] = dict(cat_id=11, fights=60, wins=30, losses=30, kos=5, streak=0, best_streak=3)
    res = run(ar.accept(1, 1, None, 11, seed=7))
    assert res.b.vet_pct == 5 and res.a.vet_pct == 0                    # b is the accepter's cat (11)
    snap = json.loads(conn.fights[1]["snap"])
    assert snap["b"]["vet_pct"] == 5 and snap["a"]["vet_pct"] == 0
    f = ar.Fight.from_row(conn.fights[1]) if hasattr(ar.Fight, "from_row") else None
    a2, b2, r2 = ar.replay_from(f)
    assert r2.winner == res.result.winner and r2.log() == res.result.log() and b2.vet_pct == 5


def test_old_snapshots_without_a_bonus_still_replay(db):
    conn = db()
    run(ar.accept(1, 1, None, 11, seed=7))
    snap = json.loads(conn.fights[1]["snap"])
    for side in ("a", "b"):
        snap[side].pop("vet_pct")
    conn.fights[1]["snap"] = json.dumps(snap)
    a2, b2, r2 = ar.replay_from(ar.Fight.from_row(conn.fights[1]))
    assert a2.vet_pct == 0 and b2.vet_pct == 0 and r2.winner in ("a", "b")


def test_no_bonus_leaves_the_engine_identical():
    a, b = cb.Fighter("A", 5, 3, 3, 2), cb.Fighter("B", 5, 3, 3, 2)
    a0 = cb.Fighter("A", 5, 3, 3, 2, vet_pct=0)
    assert cb.fight(a, b, 11).log() == cb.fight(a0, b, 11).log()


# ---------- wiring ----------
def test_migration_is_loaded_and_additive():
    sql = (ROOT / "database/migrations/064_cat_records.sql").read_text()
    assert "CREATE TABLE IF NOT EXISTS cat_records" in sql and "DROP" not in sql.upper()
    assert "064_cat_records.sql" in (ROOT / "database.py").read_text()


# ---------- screens ----------
def test_stats_screen_shows_the_record_and_stays_in_budget():
    from discord_bot.cogs import _views_cat_stats as vs
    from modules import cat_adopt as ca, cat_stats as cs
    cat = ca.Cat.from_row(FakeConn._cat(11, 1, "Mochi"))
    r = rec.Record(11, fights=23, wins=14, losses=9, kos=4, streak=2, best_streak=5)
    t = vs.screen_text(cat, cs.Stats(), {s: 0 for s in cs.STATS}, record=r)
    assert "Fight record" in t and "14W 9L" in t and "Veteran +2%" in t and "7 more fights" in t
    assert len(t) < 3900
    assert "Fight record" not in vs.screen_text(cat, cs.Stats(), {s: 0 for s in cs.STATS})
    assert "maxed" in vs.screen_text(cat, cs.Stats(), {s: 0 for s in cs.STATS}, record=rec.Record(11, fights=80))


def test_result_text_mentions_the_veteran_bonus(db):
    from discord_bot.cogs import _views_cat_arena as va
    conn = db()
    conn.records[11] = dict(cat_id=11, fights=60, wins=30, losses=30, kos=5, streak=0, best_streak=3)
    res = run(ar.accept(1, 1, None, 11, seed=7))
    assert "Veteran +5%" in va.result_text(res, 1)
    assert "Veteran" not in va.result_text(res, 2)
