# path: tests/integration/test_card_games_postgres.py
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from tests.integration.test_card_economy_postgres import (  # noqa: F401
    MIGRATION, _bal, _fund, pg_uri, pool)
from modules import card_economy as ce
from modules import card_games as g

DIR = MIGRATION.parent
T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


async def _up(pool):
    async with pool.acquire() as c:
        for f in ("090_card_trading.sql", "091_card_coin_games.sql"):
            await c.execute((DIR / f).read_text())


@pytest.mark.asyncio
async def test_coinflip_payouts_validation_and_cooldown(pool):
    await _up(pool)
    await _fund(pool, 1, 100)
    for w in (0, 5, 10_001):
        with pytest.raises(ce.StoreError) as e:
            await g.coin_flip(1, None, w)
        assert e.value.code == "bad_wager"
    assert (await g.coin_flip(1, None, 50, rng=lambda: 0.0, now=T0))["balance"] == 150
    with pytest.raises(ce.StoreError) as e:
        await g.coin_flip(1, None, 50, rng=lambda: 0.0, now=T0 + timedelta(seconds=3))
    assert e.value.code == "cooldown"
    r = await g.coin_flip(1, None, 50, rng=lambda: 0.99, now=T0 + timedelta(seconds=10))
    assert r["won"] is False and r["balance"] == 100


@pytest.mark.asyncio
async def test_cannot_wager_more_than_balance_and_loss_keeps_balance(pool):
    await _up(pool)
    await _fund(pool, 1, 40)
    with pytest.raises(ce.StoreError) as e:
        await g.coin_flip(1, None, 50, rng=lambda: 0.99, now=T0)
    assert e.value.code == "funds" and await _bal(pool, 1) == 40
    async with pool.acquire() as c:   # failed play must not burn the cooldown or write a log
        assert await c.fetchval("SELECT COUNT(*) FROM card_game_state") == 0
        assert await c.fetchval("SELECT COUNT(*) FROM card_game_log") == 0


@pytest.mark.asyncio
async def test_rapid_clicks_only_one_play(pool):
    await _up(pool)
    await _fund(pool, 1, 1000)
    res = await asyncio.gather(*[g.coin_flip(1, None, 100, rng=lambda: 0.0) for _ in range(8)],
                               return_exceptions=True)
    assert sum(isinstance(r, dict) for r in res) == 1
    assert await _bal(pool, 1) == 1100
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT COUNT(*) FROM card_game_log") == 1


@pytest.mark.asyncio
async def test_league_flip(pool):
    await _up(pool)
    await _fund(pool, 1, 100)
    with pytest.raises(ce.StoreError):
        await g.league_flip(1, None, "mythic", 10)
    assert (await g.league_flip(1, None, "gold", 20, rng=lambda: 0.0, now=T0))["delta"] == 60
    for name, (p, m) in g.LEAGUES.items():
        assert p * m < 1.0, name     # no league is positive-EV


@pytest.mark.asyncio
async def test_rob_rules_and_conservation(pool):
    await _up(pool)
    await _fund(pool, 1, 1000)
    await _fund(pool, 2, 1000)
    with pytest.raises(ce.StoreError):
        await g.rob(1, 1, None)
    r = await g.rob(1, 2, None, rng=lambda: 0.0, now=T0)       # success, min % steal
    assert r["success"] and r["amount"] == 50
    assert await _bal(pool, 1) + await _bal(pool, 2) == 2000
    with pytest.raises(ce.StoreError) as e:                     # thief cooldown
        await g.rob(1, 2, None, rng=lambda: 0.0, now=T0 + timedelta(minutes=5))
    assert e.value.code == "cooldown"
    await _fund(pool, 3, 1000)
    with pytest.raises(ce.StoreError) as e:                     # victim shield
        await g.rob(3, 2, None, rng=lambda: 0.0, now=T0 + timedelta(minutes=5))
    assert e.value.code == "cooldown"
    r = await g.rob(1, 2, None, rng=lambda: 0.99, now=T0 + timedelta(hours=7))   # fail -> fine to victim
    assert not r["success"] and r["amount"] > 0
    assert await _bal(pool, 1) + await _bal(pool, 2) == 2000
    await _fund(pool, 4, 150)
    with pytest.raises(ce.StoreError) as e:
        await g.rob(3, 4, None, rng=lambda: 0.0, now=T0 + timedelta(hours=8))
    assert e.value.code == "poor_target"


@pytest.mark.asyncio
async def test_concurrent_robs_of_one_victim_only_one_succeeds(pool):
    await _up(pool)
    await _fund(pool, 9, 10_000)
    for u in range(1, 6):
        await _fund(pool, u, 100)
    res = await asyncio.gather(*[g.rob(u, 9, None, rng=lambda: 0.0) for u in range(1, 6)], return_exceptions=True)
    assert sum(isinstance(r, dict) for r in res) == 1      # shield blocks the rest
    total = sum([await _bal(pool, u) for u in list(range(1, 6)) + [9]])
    assert total == 10_500


@pytest.mark.asyncio
async def test_a_broke_player_cannot_win_free_coins(pool):
    """Regression: funds used to be checked only on a LOSS, so a 0-coin player who rolled a win was paid for nothing."""
    await _up(pool)
    for play in (lambda: g.coin_flip(7, None, 100, rng=lambda: 0.0, now=T0),
                 lambda: g.league_flip(7, None, "diamond", 100, rng=lambda: 0.0, now=T0)):
        with pytest.raises(ce.StoreError) as e:
            await play()
        assert e.value.code == "funds"
    assert (await _bal(pool, 7) or 0) == 0
    await _fund(pool, 7, 99)                                   # one short of the stake: still refused, nothing paid
    with pytest.raises(ce.StoreError):
        await g.coin_flip(7, None, 100, rng=lambda: 0.0, now=T0)
    assert await _bal(pool, 7) == 99
    await _fund(pool, 7, 100)                                  # exactly the stake: allowed, win pays 1:1
    assert (await g.coin_flip(7, None, 100, rng=lambda: 0.0, now=T0))["balance"] == 200
