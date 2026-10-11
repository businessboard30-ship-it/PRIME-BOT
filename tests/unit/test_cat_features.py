"""Daily care streaks, cosmetics purchase outcomes, rename lock (no real database)."""
import asyncio
from contextlib import asynccontextmanager
from datetime import date

from modules import cat_features, cat_adopt


class FakeConn:
    def __init__(self, previous=None, item=None, owned=False, wallet=None):
        self.previous, self.item, self.owned, self.wallet = previous, item, owned, wallet
        self.executed = []

    def transaction(self):
        @asynccontextmanager
        async def cm():
            yield
        return cm()

    async def fetchrow(self, sql, *a):
        if "cat_daily_care" in sql:
            return self.previous
        if "cat_cosmetics" in sql:
            return self.item
        if "cat_wallets" in sql:
            return self.wallet
        return None

    async def fetchval(self, sql, *a):
        return 1 if self.owned else None

    async def execute(self, sql, *a):
        self.executed.append((sql, a))


class FakePool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        @asynccontextmanager
        async def cm():
            yield self.conn
        return cm()


def _run(coro, conn, monkeypatch):
    async def pool():
        return FakePool(conn)
    monkeypatch.setattr(cat_features, "_pool", pool)
    return asyncio.run(coro)


def test_first_care_starts_streak_at_one(monkeypatch):
    r = _run(cat_features.care(1, None, date(2026, 1, 10)), FakeConn(), monkeypatch)
    assert (r.status, r.streak, r.coins, r.diamonds) == ("ok", 1, 30, 0)


def test_consecutive_day_extends_streak(monkeypatch):
    prev = {"streak": 6, "care_date": date(2026, 1, 9)}
    r = _run(cat_features.care(1, None, date(2026, 1, 10)), FakeConn(previous=prev), monkeypatch)
    assert r.streak == 7 and r.diamonds == 1


def test_missed_day_resets_streak(monkeypatch):
    prev = {"streak": 12, "care_date": date(2026, 1, 7)}
    r = _run(cat_features.care(1, None, date(2026, 1, 10)), FakeConn(previous=prev), monkeypatch)
    assert r.streak == 1


def test_same_day_pays_nothing_twice(monkeypatch):
    prev = {"streak": 3, "care_date": date(2026, 1, 10)}
    conn = FakeConn(previous=prev)
    r = _run(cat_features.care(1, None, date(2026, 1, 10)), conn, monkeypatch)
    assert r.status == "already" and r.coins == 0 and conn.executed == []


def test_streak_is_capped(monkeypatch):
    prev = {"streak": 30, "care_date": date(2026, 1, 9)}
    r = _run(cat_features.care(1, None, date(2026, 1, 10)), FakeConn(previous=prev), monkeypatch)
    assert r.streak == cat_features.CARE_STREAK_CAP


def test_buy_outcomes(monkeypatch):
    item = {"id": 1, "price_coins": 500, "price_diamonds": 0}
    assert _run(cat_features.buy_cosmetic(1, None, 1), FakeConn(item=None), monkeypatch) == "missing"
    assert _run(cat_features.buy_cosmetic(1, None, 1), FakeConn(item=item, owned=True), monkeypatch) == "owned"
    assert _run(cat_features.buy_cosmetic(1, None, 1), FakeConn(item=item, wallet={"coins": 10, "diamonds": 0}), monkeypatch) == "poor"
    conn = FakeConn(item=item, wallet={"coins": 900, "diamonds": 0})
    assert _run(cat_features.buy_cosmetic(1, None, 1), conn, monkeypatch) == "ok"
    assert len(conn.executed) == 2        # one debit, one ownership row


def test_rename_refuses_a_cat_that_is_for_sale():
    import inspect
    src = inspect.getsource(cat_adopt.rename)
    assert "cat_listings" in src and "status = 'open'" in src
