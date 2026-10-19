"""Cat game Phase 7: Primole. One-way diamond conversion, account-bound, atomic spend, no revert path."""
import asyncio
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from modules import cat_primole as cp
from modules import cat_wallet as cw

ROOT = Path(__file__).resolve().parents[2]
SRC = (ROOT / "modules" / "cat_primole.py").read_text()


def run(c):
    return asyncio.run(c)


class Rec(dict):
    pass


class FakeConn:
    """Tiny in-memory stand-in: one wallet row, and it applies the same guards the SQL has."""
    def __init__(self, coins=0, diamonds=0, primole=0):
        self.w = {"coins": coins, "diamonds": diamonds, "primole": primole}
        self.sql = []

    async def fetchrow(self, sql, *a):
        self.sql.append(sql)
        if "SET diamonds = diamonds - $3, primole" in sql:
            n = a[2]
            if self.w["diamonds"] < n:
                return None
            self.w["diamonds"] -= n
            self.w["primole"] += n * a[3]
        elif "LEAST(diamonds" in sql:
            if self.w["diamonds"] < 1:
                return None
            n = min(self.w["diamonds"], a[2])
            self.w["diamonds"] -= n
            self.w["primole"] += n * a[3]
        elif "SET primole = primole - $3" in sql:
            if self.w["primole"] < a[2]:
                return None
            self.w["primole"] -= a[2]
        return Rec(self.w)


class FakePool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        conn = self.conn

        class Ctx:
            async def __aenter__(self_inner): return conn
            async def __aexit__(self_inner, *e): return False
        return Ctx()


@pytest.fixture
def conn(monkeypatch):
    c = FakeConn(coins=500, diamonds=3)

    async def pool():
        return FakePool(c)
    monkeypatch.setattr(cp, "_pool", pool)
    return c


def test_constants():
    assert cp.PRIMOLE_PER_DIAMOND == 100 and cp.PRIMOLE == "🔮" and cp.NAME == "Primole"
    assert cp.PRIMOLE_PER_DIAMOND * 100 == cw.DIAMOND_COINS      # equal value: 1 💎 = 10,000 🪙 = 100 🔮 at 100 coins each


def test_convert_one_diamond(conn):
    w = run(cp.convert_diamonds(1, 1))
    assert (w.diamonds, w.primole, w.coins) == (2, 100, 500)


def test_convert_all(conn):
    w = run(cp.convert_diamonds(1, None))
    assert (w.diamonds, w.primole) == (0, 300)


def test_not_enough_diamonds_changes_nothing(conn):
    assert run(cp.convert_diamonds(1, 4)) is None
    assert conn.w == {"coins": 500, "diamonds": 3, "primole": 0}
    conn.w["diamonds"] = 0
    assert run(cp.convert_diamonds(1, None)) is None


@pytest.mark.parametrize("bad", [0, -1, cp.MAX_CONVERT_AT_ONCE + 1])
def test_bad_counts_rejected_before_touching_the_database(conn, bad):
    assert run(cp.convert_diamonds(1, bad)) is None
    assert conn.sql == []


def test_double_tap_cannot_spend_one_diamond_twice(conn):
    conn.w["diamonds"] = 1
    first, second = run(cp.convert_diamonds(1, 1)), run(cp.convert_diamonds(1, 1))
    assert first is not None and second is None and conn.w["primole"] == 100


def test_spend(conn):
    conn.w["primole"] = 250
    assert run(cp.spend(conn, 1, 100)) == 150
    assert run(cp.spend(conn, 1, 151)) is None and conn.w["primole"] == 150
    assert run(cp.spend(conn, 1, 0)) is None and run(cp.spend(conn, 1, -5)) is None


def test_grant_ignores_non_positive_amounts():
    class C:
        called = False
        async def execute(self, *a): self.called = True
    c = C()
    run(cp.grant(c, 1, 0)); run(cp.grant(c, 1, -3))
    assert not c.called
    run(cp.grant(c, 1, 5)); assert c.called


def test_no_revert_path_in_the_module():
    """One way only: nothing in the module writes coins or diamonds up, and no function converts Primole back."""
    assert not re.search(r"coins\s*=\s*coins\s*\+", SRC)
    assert not re.search(r"diamonds\s*=\s*diamonds\s*\+", SRC)
    names = set(re.findall(r"^async def (\w+)|^def (\w+)", SRC, re.M))
    flat = {n for pair in names for n in pair if n}
    assert not any("revert" in n or "refund" in n or "to_coins" in n or "to_diamond" in n for n in flat)


def test_account_bound_primole_is_not_in_any_trade_market_or_fight_money_path():
    for name in ("cat_market.py", "market.py", "cat_adopt.py", "cat_arena.py", "cat_requests.py", "cat_life.py"):
        text = (ROOT / "modules" / name).read_text().lower()
        assert "primole" not in text, f"{name} must not touch Primole (account-bound)"


def test_wallet_text_shows_primole_only_when_owned():
    assert "Primole" not in cw.wallet_text(cw.Wallet(5, 1))
    assert "🔮 100 Primole" in cw.wallet_text(cw.Wallet(5, 1, 100))


def test_migration_is_additive_and_idempotent():
    sql = (ROOT / "database" / "migrations" / "068_cat_primole.sql").read_text()
    assert "ADD COLUMN IF NOT EXISTS primole BIGINT NOT NULL DEFAULT 0" in sql
    assert not re.search(r"\bDROP\b|\bDELETE\b|\bTRUNCATE\b", sql, re.I)
    assert "068_cat_primole.sql" in (ROOT / "database.py").read_text()


def test_get_wallet_reads_primole_and_tolerates_old_rows(monkeypatch):
    class C:
        def __init__(self, row): self.row = row
        async def fetchrow(self, *a): return self.row
    for row, want in ((Rec(coins=1, diamonds=2, primole=7), 7), (Rec(coins=1, diamonds=2), 0)):
        c = C(row)

        async def pool(c=c): return FakePool(c)
        monkeypatch.setattr(cw, "_pool", pool)
        assert run(cw.get_wallet(1, None)).primole == want
