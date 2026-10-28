# path: tests/integration/test_card_boost_postgres.py
import asyncio
import json

import pytest

from tests.integration.test_card_economy_postgres import (  # noqa: F401  (fixtures)
    MIGRATION, _bal, _fund, pg_uri, pool)
from modules import card_boost as cb
from modules import card_economy as ce

M2 = MIGRATION.parent / "089_card_boosts.sql"


async def _copy(pool, owner=1, rarity="epic", base=100, maxb=10, rented=False, skills=None):
    async with pool.acquire() as c:
        await c.execute(M2.read_text())
        cid = await c.fetchval(
            "INSERT INTO card_catalog (name, rarity, base_strength, max_boost_level, skills) "
            "VALUES ('X',$1,$2,$3,$4::jsonb) RETURNING card_id", rarity, base, maxb, json.dumps(skills or []))
        return await c.fetchval(
            "INSERT INTO card_copies (card_id, owner_id, rental_expires_at) VALUES ($1,$2,"
            + ("NOW() + interval '1 day'" if rented else "NULL") + ") RETURNING copy_id", cid, owner)


def test_strength_curve_is_capped_and_diminishing():
    vals = [cb.strength(100, n) for n in range(0, 40)]
    assert vals[0] == 100 and vals == sorted(vals)
    assert vals[-1] <= 200
    assert vals[2] - vals[1] > vals[8] - vals[7]


@pytest.mark.asyncio
async def test_coin_boost_and_ledger(pool):
    cp = await _copy(pool, skills=[{"key": "a", "name": "A", "unlock_level": 2}])
    await _fund(pool, 1, 10_000)
    r = await cb.boost_card(1, None, cp)
    assert r["level"] == 1 and r["coins_spent"] == cb.coin_cost("epic", 0) and r["skills"] == []
    r = await cb.boost_card(1, None, cp)
    assert [s["key"] for s in r["skills"]] == ["a"] and r["next"] is None


@pytest.mark.asyncio
async def test_high_levels_need_items_and_cap(pool):
    cp = await _copy(pool, maxb=7)
    await _fund(pool, 1, 10**9)
    for _ in range(5):
        await cb.boost_card(1, None, cp)
    with pytest.raises(ce.StoreError) as e:
        await cb.boost_card(1, None, cp)           # level 6 needs tier 2
    assert e.value.code == "item_required"
    await cb.grant_item(1, None, "boost_shard")
    with pytest.raises(ce.StoreError) as e:
        await cb.boost_card(1, None, cp, use_item="boost_shard")
    assert e.value.code == "item_required"
    await cb.grant_item(1, None, "boost_core", 2)
    await cb.boost_card(1, None, cp, use_item="boost_core")
    await cb.boost_card(1, None, cp, use_item="boost_core")
    with pytest.raises(ce.StoreError) as e:
        await cb.boost_card(1, None, cp, use_item="boost_core")
    assert e.value.code in ("max_boost", "no_item")


@pytest.mark.asyncio
async def test_concurrent_item_use_consumes_once(pool):
    cp = await _copy(pool)
    await cb.grant_item(1, None, "boost_shard", 1)
    res = await asyncio.gather(*[cb.boost_card(1, None, cp, use_item="boost_shard") for _ in range(5)],
                               return_exceptions=True)
    assert sum(isinstance(r, dict) for r in res) == 1
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT boost_level FROM card_copies WHERE copy_id=$1", cp) == 1
        assert await c.fetchval("SELECT quantity FROM user_boost_items WHERE user_id=1") == 0


@pytest.mark.asyncio
async def test_concurrent_coin_boosts_never_overspend(pool):
    cp = await _copy(pool, rarity="common")
    await _fund(pool, 1, 150)   # levels cost 100, 150, ... only the first fits
    res = await asyncio.gather(*[cb.boost_card(1, None, cp) for _ in range(5)], return_exceptions=True)
    assert sum(isinstance(r, dict) for r in res) == 1
    assert await _bal(pool, 1) == 50


@pytest.mark.asyncio
async def test_not_owner_rented_and_failure_spends_nothing(pool):
    cp = await _copy(pool, owner=2)
    await _fund(pool, 1, 1000)
    with pytest.raises(ce.StoreError) as e:
        await cb.boost_card(1, None, cp)
    assert e.value.code == "not_owner"
    rented = await _copy(pool, owner=1, rented=True)
    with pytest.raises(ce.StoreError) as e:
        await cb.boost_card(1, None, rented)
    assert e.value.code == "rented"
    with pytest.raises(ce.StoreError) as e:
        await cb.boost_card(1, None, await _copy(pool, owner=1), use_item="boost_shard")
    assert e.value.code == "no_item"
    assert await _bal(pool, 1) == 1000
