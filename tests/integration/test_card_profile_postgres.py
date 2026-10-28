# path: tests/integration/test_card_profile_postgres.py
import pytest

from tests.integration.test_card_economy_postgres import MIGRATION, _fund, pg_uri, pool  # noqa: F401
from modules import card_boost as cb
from modules import card_economy as ce
from modules import card_profile as cp

DIR = MIGRATION.parent


async def _mig(pool):
    async with pool.acquire() as c:
        for f in ("089_card_boosts.sql", "090_card_trading.sql", "092_card_profiles.sql"):
            await c.execute((DIR / f).read_text())


async def _card(pool, name, rarity="common", base=100, stock=None, store=True, skills="[]"):
    await _mig(pool)
    async with pool.acquire() as c:
        return await c.fetchval(
            "INSERT INTO card_catalog (name, rarity, base_strength, store_enabled, stock_total, stock_remaining, skills) "
            "VALUES ($1,$2,$3,$4,$5,$5,$6::jsonb) RETURNING card_id", name, rarity, base, store, stock, skills)


async def _give(pool, card, owner, level=0, serial=None, rented=False):
    async with pool.acquire() as c:
        return await c.fetchval(
            "INSERT INTO card_copies (card_id, owner_id, boost_level, serial_no, rental_expires_at) VALUES "
            "($1,$2,$3,$4," + ("NOW()+interval '1 day'" if rented else "NULL") + ") RETURNING copy_id",
            card, owner, level, serial)


@pytest.mark.asyncio
async def test_stats_distinguish_copies_unique_and_exclude_rentals(pool):
    a = await _card(pool, "A", "common")
    b = await _card(pool, "B", "god", base=500, stock=10)
    c = await _card(pool, "C", "rare")
    await _give(pool, a, 1); await _give(pool, a, 1, level=3)
    await _give(pool, b, 1, serial=1, level=2)
    await _give(pool, c, 1, rented=True)                 # rental: must not count anywhere
    s = await cp.get_stats(1, None)
    assert (s["copies"], s["unique"], s["limited"], s["high_rarity"], s["boosts"]) == (3, 2, 1, 1, 5)
    assert s["highest_rarity"] == "god"
    assert s["strength"] == cb.strength(100, 0) + cb.strength(100, 3) + cb.strength(500, 2)
    assert s["catalogue_size"] == 3 and s["completion_pct"] == 66


@pytest.mark.asyncio
async def test_sql_strength_matches_python_for_many_levels(pool):
    k = await _card(pool, "K", base=137)
    for lvl in range(0, 11):
        await _give(pool, k, 7, level=lvl)
    s = await cp.get_stats(7, None)
    assert s["strength"] == sum(cb.strength(137, n) for n in range(11))


@pytest.mark.asyncio
async def test_privacy_and_featured(pool):
    k = await _card(pool, "K", "epic", base=200)
    mine, other = await _give(pool, k, 1, level=2), await _give(pool, k, 2)
    await _fund(pool, 1, 777)
    with pytest.raises(ce.StoreError) as e:
        await cp.set_featured(1, None, [other])
    assert e.value.code == "not_owner"
    await cp.set_featured(1, None, [], signature=mine)
    pub = await cp.get_profile(99, 1, None)
    assert pub["coins"] is None and pub["trades"] == 0 and pub["signature"]["copy_id"] == mine
    assert pub["signature"]["strength"] == cb.strength(200, 2)
    await cp.update_settings(1, None, show_collection=False, show_trades=False, show_coins=True)
    pub = await cp.get_profile(99, 1, None)
    assert pub["stats"] is None and pub["trades"] is None and pub["coins"] == 777 and pub["featured"] == []
    with pytest.raises(ce.StoreError) as e:
        await cp.browse_collection(99, 1, None)
    assert e.value.code == "private"
    assert (await cp.get_profile(1, 1, None))["stats"]["copies"] == 1      # owner always sees own
    with pytest.raises(ce.StoreError):
        await cp.update_settings(1, None, evil=True)


@pytest.mark.asyncio
async def test_featured_card_traded_away_drops_out(pool):
    k = await _card(pool, "K")
    mine = await _give(pool, k, 1)
    await cp.set_featured(1, None, [mine])
    async with pool.acquire() as c:
        await c.execute("UPDATE card_copies SET owner_id=2 WHERE copy_id=$1", mine)
    assert (await cp.get_profile(1, 1, None))["featured"] == []


@pytest.mark.asyncio
async def test_browse_filters_sorts_duplicates_and_pagination(pool):
    a = await _card(pool, "A", "common", skills='[{"key":"s","unlock_level":1}]')
    b = await _card(pool, "B", "legendary", base=300)
    await _give(pool, a, 1, level=1); await _give(pool, a, 1, level=5); await _give(pool, b, 1, level=0)
    r = await cp.browse_collection(1, 1, None, sort="strongest")
    assert [c["name"] for c in r["cards"]] == ["B", "A", "A"] and r["total"] == 3
    assert [c["duplicate"] for c in r["cards"]] == [False, True, True]
    assert [c["name"] for c in (await cp.browse_collection(1, 1, None, sort="rarest"))["cards"]][0] == "B"
    assert (await cp.browse_collection(1, 1, None, sort="boosted"))["cards"][0]["boost_level"] == 5
    assert (await cp.browse_collection(1, 1, None, rarity="common"))["total"] == 2
    assert (await cp.browse_collection(1, 1, None, min_boost=5))["total"] == 1
    assert (await cp.browse_collection(1, 1, None, has_skills=True))["total"] == 2
    assert len((await cp.browse_collection(1, 1, None, limit=2, offset=2))["cards"]) == 1
    with pytest.raises(ce.StoreError):
        await cp.browse_collection(1, 1, None, sort="nope")


@pytest.mark.asyncio
async def test_achievements_idempotent_cosmetic_and_rentals_excluded(pool):
    k = await _card(pool, "K", "supreme", stock=5)
    await _give(pool, k, 1, serial=1)
    assert sorted(await cp.check_achievements(1, None)) == ["complete_set", "first_limited", "high_roller"]
    assert await cp.check_achievements(1, None) == []
    assert {a["key"] for a in await cp.list_achievements(1, None)} >= {"first_limited"}
    await _give(pool, k, 2, rented=True)
    assert await cp.check_achievements(2, None) == []          # rental earns nothing
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT COUNT(*) FROM card_coins") == 0      # no coin rewards


@pytest.mark.asyncio
async def test_leaderboards_global_local_and_no_duplicate_counting(pool):
    a, b = await _card(pool, "A"), await _card(pool, "B", "god", base=500, stock=9)
    for _ in range(3): await _give(pool, a, 1)              # user1: 3 copies, 1 unique
    await _give(pool, a, 2); await _give(pool, b, 2, serial=1)   # user2: 2 copies, 2 unique
    await _give(pool, b, 3, rented=True)                    # rentals excluded
    uniq = await cp.leaderboard("unique", None)
    assert [(r["user_id"], r["score"]) for r in uniq] == [(2, 2), (1, 1)]
    assert [r["user_id"] for r in await cp.leaderboard("copies", None)] == [1, 2]
    assert (await cp.leaderboard("limited", None))[0]["user_id"] == 2
    assert (await cp.leaderboard("rarity", None))[0]["user_id"] == 2
    assert [r["user_id"] for r in await cp.leaderboard("copies", None, member_ids=[2, 3])] == [2]   # local
    assert [r["rank"] for r in await cp.leaderboard("copies", None, offset=1)] == [2]
    async with pool.acquire() as c:
        await c.execute("INSERT INTO card_trades (scope,initiator_id,recipient_id,status,expires_at) VALUES "
                        "('global',1,2,'completed',NOW()),('global',2,1,'completed',NOW()),('global',2,3,'cancelled',NOW())")
    tr = await cp.leaderboard("trades", None)
    assert [(r["user_id"], r["score"]) for r in tr] == [(1, 2), (2, 2)]
    with pytest.raises(ce.StoreError):
        await cp.leaderboard("hax", None)
