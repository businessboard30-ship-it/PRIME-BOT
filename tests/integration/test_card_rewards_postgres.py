# path: tests/integration/test_card_rewards_postgres.py
"""Level-up coin rewards (modules/card_rewards.py) and the expiry sweep (cogs/card_hub.py) on a real Postgres."""
import asyncio

import pytest

import config as app_config
from tests.integration.test_card_economy_postgres import (  # noqa: F401
    MIGRATION, _bal, _fund, pg_uri, pool)
from modules import card_economy as ce
from modules import card_rewards as cr

DIR = MIGRATION.parent


async def _up(pool):
    async with pool.acquire() as c:
        for f in ("089_card_boosts.sql", "090_card_trading.sql", "093_card_levelup_rewards.sql"):
            await c.execute((DIR / f).read_text())


def test_reward_formula_is_capped_and_never_negative():
    assert cr.reward_for(1) == app_config.CARD_LEVELUP_COIN_BASE + app_config.CARD_LEVELUP_COIN_PER_LEVEL
    assert cr.reward_for(10_000) == app_config.CARD_LEVELUP_COIN_MAX
    assert cr.reward_for(0) >= 0 and cr.reward_for(-5) >= 0


@pytest.mark.asyncio
async def test_migration_is_idempotent(pool):
    await _up(pool)
    await _up(pool)


@pytest.mark.asyncio
async def test_each_level_pays_once_even_when_replayed_or_in_another_server(pool):
    await _up(pool)
    first = await cr.award_levelup(1, None, 0, 3)               # levels 1, 2, 3
    assert first == sum(cr.reward_for(l) for l in (1, 2, 3))
    assert await _bal(pool, 1) == first
    # same levels reached again (second server, lost XP and regained, duplicate event): nothing more
    assert await cr.award_levelup(1, None, 0, 3) == 0
    assert await cr.award_levelup(1, None, 2, 3) == 0
    assert await _bal(pool, 1) == first
    # a genuinely new level pays only that level
    assert await cr.award_levelup(1, None, 3, 4) == cr.reward_for(4)
    assert await _bal(pool, 1) == first + cr.reward_for(4)
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT COUNT(*) FROM card_levelup_rewards WHERE user_id=1") == 4
        assert await c.fetchval("SELECT COALESCE(SUM(delta),0) FROM card_coin_ledger WHERE user_id=1 AND reason='levelup'") \
            == await _bal(pool, 1)


@pytest.mark.asyncio
async def test_concurrent_level_up_events_pay_once(pool):
    await _up(pool)
    res = await asyncio.gather(*[cr.award_levelup(7, None, 0, 5) for _ in range(10)])
    assert sum(res) == sum(cr.reward_for(l) for l in range(1, 6))
    assert await _bal(pool, 7) == sum(res)


@pytest.mark.asyncio
async def test_no_reward_when_not_leveling_or_when_switched_off(pool, monkeypatch):
    await _up(pool)
    assert await cr.award_levelup(2, None, 5, 5) == 0
    assert await cr.award_levelup(2, None, 6, 5) == 0
    monkeypatch.setattr(app_config, "CARD_LEVELUP_COINS_ENABLED", False)
    assert await cr.award_levelup(2, None, 0, 5) == 0
    assert await _bal(pool, 2) is None
    async with pool.acquire() as c:                              # switched off must not burn the levels either
        assert await c.fetchval("SELECT COUNT(*) FROM card_levelup_rewards") == 0


@pytest.mark.asyncio
async def test_big_jump_is_capped_and_clones_are_separate(pool):
    await _up(pool)
    await cr.award_levelup(3, None, 0, 100)
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT COUNT(*) FROM card_levelup_rewards WHERE user_id=3") == cr.MAX_CATCH_UP
    await cr.award_levelup(3, 5, 0, 2)                            # a clone bot has its own economy
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT COUNT(*) FROM card_levelup_rewards WHERE user_id=3 AND clone_id=5") == 2


@pytest.mark.asyncio
async def test_sweep_expires_rentals_and_trades_but_spares_locked_cards(pool):
    from datetime import datetime, timedelta, timezone
    from discord_bot.cogs import card_hub
    from modules import card_trade as ct
    await _up(pool)
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    future = datetime.now(timezone.utc) + timedelta(days=1)
    async with pool.acquire() as c:
        cid = await c.fetchval("INSERT INTO card_catalog (name) VALUES ('X') RETURNING card_id")
        ids = {}
        for key, (owner, exp, locked) in {"expired": (1, past, False), "live": (1, future, False),
                                          "locked": (1, past, True), "perm": (1, None, False)}.items():
            ids[key] = await c.fetchval(
                "INSERT INTO card_copies (card_id, owner_id, rental_expires_at, trade_locked) VALUES ($1,$2,$3,$4) "
                "RETURNING copy_id", cid, owner, exp, locked)
        # a pending trade whose offer has run out, holding a permanent copy of each side
        a = await c.fetchval("INSERT INTO card_copies (card_id, owner_id) VALUES ($1,10) RETURNING copy_id", cid)
        b = await c.fetchval("INSERT INTO card_copies (card_id, owner_id) VALUES ($1,11) RETURNING copy_id", cid)
    t = await ct.propose_trade(10, 11, None, "global", [a], [b])
    async with pool.acquire() as c:
        await c.execute("UPDATE card_trades SET expires_at = NOW() - interval '1 minute' WHERE trade_id=$1", t)
    rentals, trades = await card_hub.sweep_once()
    assert (rentals, trades) == (1, 1)
    async with pool.acquire() as c:
        left = {r["copy_id"] for r in await c.fetch("SELECT copy_id FROM card_copies WHERE owner_id=1")}
        assert left == {ids["live"], ids["locked"], ids["perm"]}
        assert await c.fetchval("SELECT status FROM card_trades WHERE trade_id=$1", t) == "expired"
        assert not await c.fetchval("SELECT bool_or(trade_locked) FROM card_copies WHERE copy_id IN ($1,$2)", a, b)
    assert await card_hub.sweep_once() == (0, 0)                  # safe to run again


async def _items(pool, uid):
    async with pool.acquire() as c:
        rows = await c.fetch("SELECT item_key, quantity FROM user_boost_items WHERE user_id=$1", uid)
    return {r["item_key"]: r["quantity"] for r in rows}


def test_item_drop_schedule_is_rare_at_the_top():
    assert cr.item_for(4) is None and cr.item_for(0) is None
    assert cr.item_for(5) == ("boost_shard", 1) and cr.item_for(10) == ("boost_shard", 1)
    assert cr.item_for(25) == ("boost_core", 1) and cr.item_for(75) == ("boost_core", 1)
    assert cr.item_for(50) == ("boost_apex", 1) and cr.item_for(100) == ("boost_apex", 1)


@pytest.mark.asyncio
async def test_milestone_levels_drop_items_once_and_catch_up_pays_each(pool):
    await _up(pool)
    await cr.award_levelup(1, None, 0, 4)
    assert await _items(pool, 1) == {}
    await cr.award_levelup(1, None, 4, 12)                      # crosses 5 and 10
    assert await _items(pool, 1) == {"boost_shard": 2}
    await cr.award_levelup(1, None, 4, 12)                      # replay / second server: nothing more
    await cr.award_levelup(1, None, 0, 12)
    assert await _items(pool, 1) == {"boost_shard": 2}
    await cr.award_levelup(1, None, 12, 30)                     # 15, 20, 30 shards; 25 core (inside the 20-level catch-up cap)
    assert await _items(pool, 1) == {"boost_shard": 5, "boost_core": 1}
    await cr.award_levelup(1, None, 30, 50)                     # 35, 40, 45 shards; 50 apex
    assert await _items(pool, 1) == {"boost_shard": 8, "boost_core": 1, "boost_apex": 1}


@pytest.mark.asyncio
async def test_drops_can_be_switched_off_and_concurrent_awards_do_not_double_grant(pool, monkeypatch):
    await _up(pool)
    monkeypatch.setattr(app_config, "CARD_LEVELUP_ITEMS_ENABLED", False)
    await cr.award_levelup(2, None, 0, 10)
    assert await _items(pool, 2) == {}
    monkeypatch.setattr(app_config, "CARD_LEVELUP_ITEMS_ENABLED", True)
    await asyncio.gather(*[cr.award_levelup(3, None, 0, 5) for _ in range(6)])
    assert await _items(pool, 3) == {"boost_shard": 1}
