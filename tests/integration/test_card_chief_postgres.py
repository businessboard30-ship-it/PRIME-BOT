# path: tests/integration/test_card_chief_postgres.py
"""Buying a Chief Seat with coins (modules/card_chief.py) + the leaderboard recompute's lease rules, on a real Postgres."""
import asyncio

import pytest

import config as app_config
from tests.integration.test_card_economy_postgres import (  # noqa: F401
    MIGRATION, _bal, _fund, pg_uri, pool)
from modules import card_chief as cc
from modules import card_economy as ce

DIR = MIGRATION.parent
G = 555

XP_DDL = """
CREATE TABLE IF NOT EXISTS discord_xp (guild_id BIGINT NOT NULL, clone_id INTEGER, user_id BIGINT NOT NULL,
    total_xp BIGINT NOT NULL DEFAULT 0, level INTEGER NOT NULL DEFAULT 0, last_xp_at TIMESTAMPTZ);
"""


async def _up(pool):
    async with pool.acquire() as c:
        await c.execute(XP_DDL)
        for f in ("018_clan_chiefs.sql", "095_card_chief_seats.sql"):
            await c.execute((DIR / f).read_text())


async def _xp(pool, uid, level=5, xp=None, guild=G):
    async with pool.acquire() as c:
        await c.execute("INSERT INTO discord_xp (guild_id, clone_id, user_id, total_xp, level, last_xp_at) "
                        "VALUES ($1, NULL, $2, $3, $4, NOW())", guild, uid, xp if xp is not None else level * 100, level)


async def _seat(pool, rank, guild=G):
    async with pool.acquire() as c:
        return dict(await c.fetchrow("SELECT * FROM discord_clan_chiefs WHERE guild_id=$1 AND seat_rank=$2", guild, rank))


@pytest.mark.asyncio
async def test_migration_is_idempotent(pool):
    await _up(pool)
    await _up(pool)


@pytest.mark.asyncio
async def test_buy_empty_seat_charges_once_and_logs_everything(pool):
    await _up(pool)
    await _xp(pool, 1)
    await _fund(pool, 1, 100_000)
    r = await cc.buy_seat(1, None, G, 2)
    price = cc.seat_price(2)
    assert r["price"] == price and r["displaced_id"] is None and not r["renewed"]
    assert await _bal(pool, 1) == 100_000 - price
    s = await _seat(pool, 2)
    assert s["user_id"] == 1 and s["lease_until"] is not None
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT COUNT(*) FROM card_chief_purchases WHERE buyer_id=1") == 1
        assert await c.fetchval("SELECT delta FROM card_coin_ledger WHERE user_id=1 AND reason='chief_seat'") == -price


@pytest.mark.asyncio
async def test_failures_consume_nothing(pool):
    await _up(pool)
    await _xp(pool, 1, level=app_config.CHIEF_MIN_LEVEL - 1)          # too low a level
    await _xp(pool, 2)                                                  # can't afford it
    await _fund(pool, 1, 999_999)
    await _fund(pool, 2, 10)
    with pytest.raises(ce.StoreError) as e:
        await cc.buy_seat(1, None, G, 1)
    assert e.value.code == "min_level" and await _bal(pool, 1) == 999_999
    with pytest.raises(ce.StoreError) as e:
        await cc.buy_seat(2, None, G, 1)
    assert e.value.code == "funds" and await _bal(pool, 2) == 10
    assert all(s["user_id"] is None for s in await cc.list_seats(G, None))     # nobody got a seat
    with pytest.raises(ce.StoreError) as e:                             # a user that has no XP row at all
        await cc.buy_seat(99, None, G, 1)
    assert e.value.code == "min_level"
    with pytest.raises(ce.StoreError) as e:
        await cc.buy_seat(2, None, G, 9)
    assert e.value.code == "no_seat"
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT COUNT(*) FROM card_chief_purchases") == 0


@pytest.mark.asyncio
async def test_switched_off_and_dm_are_refused(pool, monkeypatch):
    await _up(pool)
    await _xp(pool, 1)
    await _fund(pool, 1, 999_999)
    with pytest.raises(ce.StoreError) as e:
        await cc.buy_seat(1, None, 0, 1)
    assert e.value.code == "no_guild"
    monkeypatch.setattr(app_config, "CHIEF_SEAT_PURCHASE_ENABLED", False)
    with pytest.raises(ce.StoreError) as e:
        await cc.buy_seat(1, None, G, 1)
    assert e.value.code == "disabled" and await _bal(pool, 1) == 999_999


@pytest.mark.asyncio
async def test_lease_blocks_others_but_an_earned_seat_can_be_taken(pool):
    await _up(pool)
    for u in (1, 2, 3):
        await _xp(pool, u)
        await _fund(pool, u, 500_000)
    await cc.list_seats(G, None)
    async with pool.acquire() as c:                                      # user 3 EARNED seat 4 (no lease)
        await c.execute("UPDATE discord_clan_chiefs SET user_id=3, since=NOW() WHERE guild_id=$1 AND seat_rank=4", G)
    r = await cc.buy_seat(1, None, G, 4)                                 # displaces the earned chief
    assert r["displaced_id"] == 3 and (await _seat(pool, 4))["user_id"] == 1
    with pytest.raises(ce.StoreError) as e:                              # but not a running lease
        await cc.buy_seat(2, None, G, 4)
    assert e.value.code == "leased" and await _bal(pool, 2) == 500_000
    assert (await _seat(pool, 4))["user_id"] == 1


@pytest.mark.asyncio
async def test_one_seat_per_person_and_renewal_is_capped(pool):
    await _up(pool)
    await _xp(pool, 1)
    await _fund(pool, 1, 10_000_000)
    await cc.buy_seat(1, None, G, 5)
    with pytest.raises(ce.StoreError) as e:
        await cc.buy_seat(1, None, G, 3)
    assert e.value.code == "holds_other"
    first = (await _seat(pool, 5))["lease_until"]
    r = await cc.buy_seat(1, None, G, 5)                                 # renew: extends from the END of the lease
    assert r["renewed"] and r["lease_until"] - first == __import__("datetime").timedelta(days=app_config.CHIEF_SEAT_LEASE_DAYS)
    renewals = 1
    with pytest.raises(ce.StoreError) as e:
        while True:
            await cc.buy_seat(1, None, G, 5)
            renewals += 1
            assert renewals < 20
    assert e.value.code == "max_lease"
    assert renewals * app_config.CHIEF_SEAT_LEASE_DAYS <= app_config.CHIEF_SEAT_MAX_LEASE_DAYS


@pytest.mark.asyncio
async def test_two_buyers_one_seat_only_one_wins(pool):
    await _up(pool)
    for u in (1, 2):
        await _xp(pool, u)
        await _fund(pool, u, 100_000)
    res = await asyncio.gather(cc.buy_seat(1, None, G, 1), cc.buy_seat(2, None, G, 1), return_exceptions=True)
    assert sum(1 for r in res if isinstance(r, dict)) == 1
    assert sum(1 for r in res if isinstance(r, ce.StoreError) and r.code == "leased") == 1
    winner = (await _seat(pool, 1))["user_id"]
    loser = 2 if winner == 1 else 1
    assert await _bal(pool, winner) == 100_000 - cc.seat_price(1) and await _bal(pool, loser) == 100_000


@pytest.mark.asyncio
async def test_one_buyer_cannot_grab_two_seats_by_racing(pool):
    await _up(pool)
    await _xp(pool, 1)
    await _fund(pool, 1, 1_000_000)
    res = await asyncio.gather(*[cc.buy_seat(1, None, G, k) for k in (1, 2, 3, 4, 5)], return_exceptions=True)
    assert sum(1 for r in res if isinstance(r, dict)) == 1
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT COUNT(*) FROM discord_clan_chiefs WHERE user_id=1") == 1
        assert await c.fetchval("SELECT COUNT(*) FROM card_chief_purchases") == 1
    won = next(r for r in res if isinstance(r, dict))
    assert await _bal(pool, 1) == 1_000_000 - won["price"]


@pytest.mark.asyncio
async def test_concurrent_double_click_charges_once(pool):
    await _up(pool)
    await _xp(pool, 1)
    await _fund(pool, 1, 1_000_000)
    res = await asyncio.gather(*[cc.buy_seat(1, None, G, 2) for _ in range(6)], return_exceptions=True)
    ok = [r for r in res if isinstance(r, dict)]
    assert len(ok) >= 1                                                  # the first buys, the rest can only be renewals
    assert sum(1 for r in ok if not r["renewed"]) == 1
    assert await _bal(pool, 1) == 1_000_000 - sum(r["price"] for r in ok)
    assert len(ok) * app_config.CHIEF_SEAT_LEASE_DAYS <= app_config.CHIEF_SEAT_MAX_LEASE_DAYS


# ---------------------------------------------------------------- the XP recompute (database.py) respects leases
@pytest.fixture
def db(pool, monkeypatch):
    import database
    async def _p():
        return pool
    monkeypatch.setattr(database, "get_pool", _p)
    obj = database.Database.__new__(database.Database)
    return obj


@pytest.mark.asyncio
async def test_recompute_leaves_a_leased_seat_alone_then_releases_it(pool, db):
    await _up(pool)
    for u in range(1, 7):                                                # six eligible XP members, 1 is the strongest
        await _xp(pool, u, level=10, xp=10_000 - u * 100)
    await _xp(pool, 9, level=app_config.CHIEF_MIN_LEVEL, xp=300)         # the buyer is nowhere near the top 5
    await _fund(pool, 9, 500_000)
    await cc.buy_seat(9, None, G, 3)
    await db.recompute_clan_chiefs(G, clone_id=None)
    seats = {s["seat_rank"]: s for s in await db.get_clan_seats(G)}
    assert seats[3]["user_id"] == 9                                      # lease survives the recompute
    holders = {s["user_id"] for s in seats.values()}
    assert 9 in holders and len(holders - {None}) == 5

    async with pool.acquire() as c:                                      # lease ends
        await c.execute("UPDATE discord_clan_chiefs SET lease_until = NOW() - INTERVAL '1 hour' "
                        "WHERE guild_id=$1 AND seat_rank=3", G)
    changes = await db.recompute_clan_chiefs(G, clone_id=None)
    assert any(c["old_user_id"] == 9 and c["new_user_id"] is None and c["seat_rank"] == 3 for c in changes)
    seats = {s["seat_rank"]: s for s in await db.get_clan_seats(G)}
    assert 9 not in {s["user_id"] for s in seats.values()}               # seat is back with the XP top 5
    assert seats[3]["lease_until"] is None and seats[3]["user_id"] in {1, 2, 3, 4, 5}


@pytest.mark.asyncio
async def test_expired_lease_keeps_a_holder_who_is_also_in_the_top_5(pool, db):
    await _up(pool)
    for u in range(1, 4):
        await _xp(pool, u, level=10, xp=10_000 - u * 100)
    await _fund(pool, 1, 500_000)
    await cc.buy_seat(1, None, G, 5)
    async with pool.acquire() as c:
        await c.execute("UPDATE discord_clan_chiefs SET lease_until = NOW() - INTERVAL '1 hour' "
                        "WHERE guild_id=$1 AND seat_rank=5", G)
    await db.recompute_clan_chiefs(G, clone_id=None)
    s = await _seat(pool, 5)
    assert s["user_id"] == 1 and s["lease_until"] is None                # earned it too: keeps it, now as an earned seat


@pytest.mark.asyncio
async def test_other_servers_are_unaffected(pool, db):
    await _up(pool)
    await _xp(pool, 1)
    await _xp(pool, 1, guild=G + 1)
    await _fund(pool, 1, 500_000)
    await cc.buy_seat(1, None, G, 1)
    other = await cc.list_seats(G + 1, None)
    assert all(s["user_id"] is None and not s["leased"] for s in other)
