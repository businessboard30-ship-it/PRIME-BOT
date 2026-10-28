# path: tests/integration/test_card_trade_postgres.py
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from tests.integration.test_card_economy_postgres import (  # noqa: F401
    MIGRATION, _bal, _fund, pg_uri, pool)
from modules import card_boost as cb
from modules import card_economy as ce
from modules import card_trade as ct

DIR = MIGRATION.parent


async def _setup(pool):
    async with pool.acquire() as c:
        for f in ("089_card_boosts.sql", "090_card_trading.sql"):
            await c.execute((DIR / f).read_text())


async def _copy(pool, owner, tradeable=True, rented=False, level=0):
    await _setup(pool)
    async with pool.acquire() as c:
        cid = await c.fetchval("INSERT INTO card_catalog (name, tradeable) VALUES ('X',$1) RETURNING card_id", tradeable)
        return await c.fetchval(
            "INSERT INTO card_copies (card_id, owner_id, boost_level, rental_expires_at) VALUES ($1,$2,$3,"
            + ("NOW() + interval '1 day'" if rented else "NULL") + ") RETURNING copy_id", cid, owner, level)


async def _owner(pool, copy):
    async with pool.acquire() as c:
        return await c.fetchval("SELECT owner_id FROM card_copies WHERE copy_id=$1", copy)


@pytest.mark.asyncio
async def test_global_swap_preserves_boosts_and_history(pool):
    a, b = await _copy(pool, 1, level=4), await _copy(pool, 2, level=2)
    t = await ct.propose_trade(1, 2, None, "global", [a], [b])
    await ct.accept_trade(t, 2)
    assert (await _owner(pool, a), await _owner(pool, b)) == (2, 1)
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT boost_level FROM card_copies WHERE copy_id=$1", a) == 4
        assert await c.fetchval("SELECT status FROM card_trades WHERE trade_id=$1", t) == "completed"
        assert not await c.fetchval("SELECT trade_locked FROM card_copies WHERE copy_id=$1", a)


@pytest.mark.asyncio
async def test_card_for_coins_and_failed_accept_changes_nothing(pool):
    a = await _copy(pool, 1)
    await _fund(pool, 2, 50)
    t = await ct.propose_trade(1, 2, None, "global", [a], [], get_coins=100)
    with pytest.raises(ce.StoreError) as e:
        await ct.accept_trade(t, 2)
    assert e.value.code == "funds"
    assert await _owner(pool, a) == 1 and await _bal(pool, 2) == 50
    await _fund(pool, 2, 100)
    await ct.accept_trade(t, 2)
    assert await _owner(pool, a) == 2 and await _bal(pool, 1) == 100


@pytest.mark.asyncio
async def test_coins_move_on_successful_sale(pool):
    a = await _copy(pool, 1)
    await _fund(pool, 2, 100)
    t = await ct.propose_trade(2, 1, None, "global", [], [a], give_coins=100)
    await ct.accept_trade(t, 1)
    assert await _owner(pool, a) == 2 and await _bal(pool, 1) == 100 and await _bal(pool, 2) == 0


@pytest.mark.asyncio
async def test_double_accept_and_double_offer(pool):
    a, b = await _copy(pool, 1), await _copy(pool, 2)
    t = await ct.propose_trade(1, 2, None, "global", [a], [b])
    with pytest.raises(ce.StoreError) as e:
        await ct.propose_trade(1, 3, None, "global", [a], [await _copy(pool, 3)])
    assert e.value.code == "locked"
    res = await asyncio.gather(*[ct.accept_trade(t, 2) for _ in range(6)], return_exceptions=True)
    assert sum(isinstance(r, dict) for r in res) == 1
    assert (await _owner(pool, a), await _owner(pool, b)) == (2, 1)


@pytest.mark.asyncio
async def test_restrictions(pool):
    a = await _copy(pool, 1)
    for kw, code in (({"tradeable": False}, "not_tradeable"), ({"rented": True}, "rented")):
        bad = await _copy(pool, 1, **kw)
        with pytest.raises(ce.StoreError) as e:
            await ct.propose_trade(1, 2, None, "global", [bad], [await _copy(pool, 2)])
        assert e.value.code == code
    with pytest.raises(ce.StoreError) as e:
        await ct.propose_trade(1, 2, None, "local", [a], [await _copy(pool, 2)], guild_id=5, recipient_in_guild=False)
    assert e.value.code == "not_in_server"
    with pytest.raises(ce.StoreError) as e:
        await ct.propose_trade(1, 2, None, "global", [a], [await _copy(pool, 3)])   # other side's card not theirs
    assert e.value.code == "not_owner"
    t = await ct.propose_trade(1, 2, None, "local", [a], [await _copy(pool, 2)], guild_id=5, recipient_in_guild=True)
    with pytest.raises(ce.StoreError) as e:
        await ct.accept_trade(t, 2, recipient_in_guild=False)
    assert e.value.code == "not_in_server"
    with pytest.raises(ce.StoreError) as e:
        await ct.accept_trade(t, 1)
    assert e.value.code == "not_recipient"


@pytest.mark.asyncio
async def test_cancel_expire_and_locked_cards_cannot_boost(pool):
    a = await _copy(pool, 1)
    b = await _copy(pool, 2)
    t = await ct.propose_trade(1, 2, None, "global", [a], [b])
    await _fund(pool, 1, 10_000)
    with pytest.raises(ce.StoreError) as e:
        await cb.boost_card(1, None, a)
    assert e.value.code == "locked"
    await ct.cancel_trade(t, 2)
    with pytest.raises(ce.StoreError):
        await ct.accept_trade(t, 2)
    t2 = await ct.propose_trade(1, 2, None, "global", [a], [b])
    assert await ct.expire_trades() == 0
    assert await ct.expire_trades(datetime.now(timezone.utc) + timedelta(hours=1)) == 1
    with pytest.raises(ce.StoreError) as e:
        await ct.accept_trade(t2, 2)
    assert e.value.code == "not_pending"
    await ct.propose_trade(1, 2, None, "global", [a], [b])   # unlocked again after expiry


@pytest.mark.asyncio
async def test_opposite_coin_trades_do_not_deadlock(pool):
    await _setup(pool)
    await _fund(pool, 1, 1000)
    await _fund(pool, 2, 1000)
    cards = [(await _copy(pool, 1), await _copy(pool, 2)) for _ in range(6)]
    ts = []
    for i, (x, y) in enumerate(cards):
        ts.append(await (ct.propose_trade(1, 2, None, "global", [x], [], get_coins=10) if i % 2 == 0
                         else ct.propose_trade(2, 1, None, "global", [y], [], get_coins=10)))
    who = [2 if i % 2 == 0 else 1 for i in range(6)]
    res = await asyncio.wait_for(asyncio.gather(*[ct.accept_trade(t, w) for t, w in zip(ts, who)]), 20)
    assert len(res) == 6 and await _bal(pool, 1) + await _bal(pool, 2) == 2000


@pytest.mark.asyncio
async def test_coin_transfers(pool):
    await _setup(pool)
    await _fund(pool, 1, 100)
    for amt in (0, -5):
        with pytest.raises(ce.StoreError):
            await ct.transfer_coins(1, 2, None, amt)
    with pytest.raises(ce.StoreError):
        await ct.transfer_coins(1, 1, None, 5)
    with pytest.raises(ce.StoreError) as e:
        await ct.transfer_coins(1, 2, None, 101)
    assert e.value.code == "funds"
    res = await asyncio.gather(*[ct.transfer_coins(1, 2, None, 60) for _ in range(5)], return_exceptions=True)
    assert sum(isinstance(r, int) for r in res) == 1
    assert (await _bal(pool, 1), await _bal(pool, 2)) == (40, 60)
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT COUNT(*) FROM card_coin_transfers") == 1
