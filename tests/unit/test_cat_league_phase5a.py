"""Cat League, slice A: pure rules and the join transaction on an in-memory fake. Play coins only."""
import asyncio
import copy
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from modules import cat_league as cl

ROOT = Path(__file__).resolve().parents[2]


def run(c):
    return asyncio.run(c)


def test_constants_and_divisions():
    assert cl.SEASON_DAYS == 28 and len(cl.DIVISIONS) == 5 and cl.ENTRY_FEE == 1_000
    assert cl.division_for(1000) == 0 and cl.division_for(1100) == 1 and cl.division_for(1399) == 2
    assert cl.division_for(1400) == 3 and cl.division_for(5000) == 4 and cl.division_for(50) == 0
    assert cl.division_name(0) == "Bronze" and cl.division_name(99) == "Diamond"
    assert cl.points_for(True) == 3 and cl.points_for(False) == 1
    assert sorted([(5, 1, 3), (5, 2, 9), (7, 0, 1)], key=lambda t: cl.rank_key(*t)) == [(7, 0, 1), (5, 2, 9), (5, 1, 3)]


class Fake:
    def __init__(self, coins=5_000, arena_on=True, rating=1000):
        self.coins, self.arena = coins, {"arena_on": arena_on, "rating": rating}
        self.seasons, self.entries, self.sink = {}, {}, 0

    def acquire(self): return self

    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False

    @asynccontextmanager
    async def transaction(self):
        snap = copy.deepcopy((self.coins, self.seasons, self.entries))
        try:
            yield
        except BaseException:
            self.coins, self.seasons, self.entries = snap
            raise

    async def fetchrow(self, sql, *a):
        if "FROM cat_arena" in sql:
            return self.arena
        if sql.startswith("UPDATE cat_wallets"):
            if self.coins >= a[2]:
                self.coins -= a[2]
                return {"coins": self.coins}
            return None

    async def fetchval(self, sql, *a):
        if "SELECT id FROM cat_league_seasons" in sql:
            return next((i for i, s in self.seasons.items() if s["status"] == "active"), None)
        if sql.startswith("INSERT INTO cat_league_seasons"):
            i = len(self.seasons) + 1
            self.seasons[i] = {"status": "active"}
            return i
        if "FROM cat_league_entries" in sql:
            return 1 if (a[0], a[1]) in self.entries else None

    async def execute(self, sql, *a):
        if sql.startswith("INSERT INTO cat_league_entries"):
            self.entries[(a[0], a[1])] = {"division": a[2], "fee": a[3]}


@pytest.fixture
def db(monkeypatch):
    def make(**kw):
        conn = Fake(**kw)

        async def pool():
            return conn
        monkeypatch.setattr(cl, "_pool", pool)
        return conn
    return make


def test_join_takes_the_fee_once_and_places_by_rating(db):
    conn = db(rating=1300)
    r = run(cl.join(1, None))
    assert r.status == "ok" and r.division == 2 and r.coins == 4_000 and conn.coins == 4_000 and len(conn.entries) == 1
    again = run(cl.join(1, None))
    assert again.status == "already" and conn.coins == 4_000 and len(conn.entries) == 1 and len(conn.seasons) == 1


def test_join_needs_arena_and_coins_and_changes_nothing_otherwise(db):
    conn = db(arena_on=False)
    assert run(cl.join(1, None)).status == "me_off" and conn.coins == 5_000 and not conn.entries
    conn = db(coins=999)
    assert run(cl.join(1, None)).status == "poor" and conn.coins == 999 and not conn.entries


def test_a_failure_while_entering_gives_the_fee_back(db):
    conn = db()

    async def boom(sql, *a):
        raise RuntimeError("boom")
    conn.execute = boom
    with pytest.raises(RuntimeError):
        run(cl.join(1, None))
    assert conn.coins == 5_000 and not conn.entries


def test_migration_is_additive_and_wired():
    sql = (ROOT / "database/migrations/062_cat_league.sql").read_text()
    assert "CREATE TABLE IF NOT EXISTS cat_league_seasons" in sql and "cat_league_one_active_idx" in sql
    assert "DROP" not in sql.upper() and "062_cat_league.sql" in (ROOT / "database.py").read_text()
