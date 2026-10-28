# path: tests/integration/test_card_economy_postgres.py
"""Real-Postgres tests for modules/card_economy.py (uses the pip `pgserver` bundled Postgres; skipped if absent)."""
import asyncio
import pathlib
import tempfile

import pytest
import pytest_asyncio

pgserver = pytest.importorskip("pgserver")
asyncpg = pytest.importorskip("asyncpg")

from modules import card_economy as ce  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[2]
MIGRATION = ROOT / "database" / "migrations" / "088_card_economy_core.sql"

BASE_DDL = """
CREATE TABLE IF NOT EXISTS card_catalog (
    card_id SERIAL PRIMARY KEY, clone_id INTEGER, name TEXT NOT NULL, rarity TEXT NOT NULL DEFAULT 'common',
    emoji TEXT, image_url TEXT, base_value INTEGER NOT NULL DEFAULT 10, active BOOLEAN NOT NULL DEFAULT TRUE);
CREATE TABLE IF NOT EXISTS card_coins (
    user_id BIGINT NOT NULL, clone_id INTEGER, balance BIGINT NOT NULL DEFAULT 0,
    last_daily_at TIMESTAMPTZ, started_at TIMESTAMPTZ NOT NULL DEFAULT NOW());
CREATE UNIQUE INDEX IF NOT EXISTS card_coins_user_clone_key ON card_coins (user_id, COALESCE(clone_id, -1));
"""


@pytest.fixture(scope="module")
def pg_uri():
    d = tempfile.mkdtemp()
    return pgserver.get_server(d).get_uri()


@pytest_asyncio.fixture
async def pool(pg_uri, monkeypatch):
    p = await asyncpg.create_pool(pg_uri, min_size=2, max_size=20)
    async with p.acquire() as c:
        await c.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        await c.execute(BASE_DDL)
        await c.execute(MIGRATION.read_text())
    async def _p():
        return p
    monkeypatch.setattr(ce, "_pool", _p)
    yield p
    await p.close()


async def _card(pool, name="Dragon", stock=None, **kw):
    async with pool.acquire() as c:
        cid = await c.fetchval(
            "INSERT INTO card_catalog (name, rarity, store_enabled, min_level, max_level) "
            "VALUES ($1,'god',TRUE,$2,$3) RETURNING card_id", name, kw.get("min_level", 0), kw.get("max_level"))
    if stock is not None:
        await ce.set_limited_stock(cid, stock)
    return cid


async def _fund(pool, uid, amount):
    async with pool.acquire() as c:
        await c.execute(
            "INSERT INTO card_coins (user_id, clone_id, balance) VALUES ($1, NULL, $2) "
            "ON CONFLICT (user_id, COALESCE(clone_id, -1)) DO UPDATE SET balance = $2", uid, amount)


async def _bal(pool, uid):
    async with pool.acquire() as c:
        return await c.fetchval("SELECT balance FROM card_coins WHERE user_id = $1", uid)


@pytest.mark.asyncio
async def test_migration_is_idempotent(pool):
    async with pool.acquire() as c:
        await c.execute(MIGRATION.read_text())
        await c.execute(MIGRATION.read_text())


@pytest.mark.asyncio
async def test_last_copy_race_only_one_winner(pool):
    cid = await _card(pool, stock=1)
    offer = await ce.create_offer(cid, "permanent", 100)
    users = list(range(1, 13))
    for u in users:
        await _fund(pool, u, 100)
    res = await asyncio.gather(*[ce.buy_offer(u, None, offer, 50) for u in users], return_exceptions=True)
    wins = [r for r in res if isinstance(r, dict)]
    losses = [r for r in res if isinstance(r, ce.StoreError)]
    assert len(wins) == 1 and len(losses) == 11
    assert all(e.code == "out_of_stock" for e in losses)
    assert wins[0]["serial_no"] == 1
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT stock_remaining FROM card_catalog WHERE card_id=$1", cid) == 0
        assert await c.fetchval("SELECT COUNT(*) FROM card_copies WHERE card_id=$1", cid) == 1
    # losers kept every coin; only the winner paid
    balances = [await _bal(pool, u) for u in users]
    assert sorted(balances) == [0] + [100] * 11


@pytest.mark.asyncio
async def test_insufficient_funds_does_not_consume_stock(pool):
    cid = await _card(pool, stock=5)
    offer = await ce.create_offer(cid, "permanent", 500)
    await _fund(pool, 1, 499)
    with pytest.raises(ce.StoreError) as e:
        await ce.buy_offer(1, None, offer, 50)
    assert e.value.code == "funds"
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT stock_remaining FROM card_catalog WHERE card_id=$1", cid) == 5
        assert await c.fetchval("SELECT COUNT(*) FROM card_copies") == 0
        assert await c.fetchval("SELECT COUNT(*) FROM card_purchases") == 0
    assert await _bal(pool, 1) == 499


@pytest.mark.asyncio
async def test_same_user_concurrent_buys_never_go_negative(pool):
    cid = await _card(pool)
    offer = await ce.create_offer(cid, "permanent", 60)
    await _fund(pool, 1, 100)
    res = await asyncio.gather(*[ce.buy_offer(1, None, offer, 50) for _ in range(6)], return_exceptions=True)
    assert sum(isinstance(r, dict) for r in res) == 1
    assert await _bal(pool, 1) == 40


@pytest.mark.asyncio
async def test_serials_count_up_and_replenish_continues(pool):
    cid = await _card(pool, stock=2)
    offer = await ce.create_offer(cid, "permanent", 10)
    for u in (1, 2, 3):
        await _fund(pool, u, 10)
    a = await ce.buy_offer(1, None, offer, 1)
    b = await ce.buy_offer(2, None, offer, 1)
    assert (a["serial_no"], b["serial_no"]) == (1, 2)
    with pytest.raises(ce.StoreError) as e:
        await ce.buy_offer(3, None, offer, 1)
    assert e.value.code == "out_of_stock"
    assert await ce.replenish_stock(cid, 1, actor_id=9) == 1
    c = await ce.buy_offer(3, None, offer, 1)
    assert c["serial_no"] == 3 and c["stock_total"] == 3


@pytest.mark.asyncio
async def test_level_gates(pool):
    cid = await _card(pool, min_level=20, max_level=40)
    offer = await ce.create_offer(cid, "permanent", 1)
    await _fund(pool, 1, 10)
    for lvl, code in ((19, "min_level"), (41, "max_level")):
        with pytest.raises(ce.StoreError) as e:
            await ce.buy_offer(1, None, offer, lvl)
        assert e.value.code == code
    assert (await ce.buy_offer(1, None, offer, 20))["kind"] == "permanent"


@pytest.mark.asyncio
async def test_rentals_expire_and_limited_cannot_be_rented(pool):
    limited = await _card(pool, name="Ltd", stock=3)
    with pytest.raises(ce.StoreError) as e:
        await ce.create_offer(limited, "rental", 5, rental_days=7)
    assert e.value.code == "limited_rental"
    cid = await _card(pool, name="Rent")
    offer = await ce.create_offer(cid, "rental", 5, rental_days=7)
    await _fund(pool, 1, 10)
    got = await ce.buy_offer(1, None, offer, 1)
    assert got["rental_expires_at"] is not None
    from datetime import datetime, timedelta, timezone
    assert await ce.expire_rentals() == 0
    assert await ce.expire_rentals(datetime.now(timezone.utc) + timedelta(days=8)) == 1


@pytest.mark.asyncio
async def test_not_in_store_and_ledger(pool):
    async with pool.acquire() as c:
        cid = await c.fetchval("INSERT INTO card_catalog (name) VALUES ('Hidden') RETURNING card_id")
    offer = await ce.create_offer(cid, "permanent", 1)
    await _fund(pool, 1, 10)
    with pytest.raises(ce.StoreError) as e:
        await ce.buy_offer(1, None, offer, 1)
    assert e.value.code == "not_for_sale"
    ok = await _card(pool, name="Shown")
    o2 = await ce.create_offer(ok, "permanent", 4)
    await ce.buy_offer(1, None, o2, 1)
    async with pool.acquire() as c:
        row = await c.fetchrow("SELECT delta, balance, reason FROM card_coin_ledger WHERE user_id=1")
    assert (row["delta"], row["balance"], row["reason"]) == (-4, 6, "store_purchase")
