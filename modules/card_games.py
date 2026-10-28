# path: modules/card_games.py

"""
Coin games on the global card_coins balance: Coin Flip, League Flip, Rob.

All outcomes are decided server-side inside one transaction that holds the player's balance row lock, so rapid-fire
clicks serialise: the cooldown check, balance check, payout and log row can't interleave (no overdraw, no double
payout). Every game has negative expected value for the player (tunable via the tables below) so coins can't be farmed.
`rng` / `now` are injectable for tests.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from typing import Optional

from modules import card_economy as _ce
from modules.card_economy import StoreError, _clone_key
from modules.card_trade import _lock_balances

MIN_WAGER, MAX_WAGER = 10, 10_000
COINFLIP_WIN_CHANCE = 0.49           # pays 1:1 -> EV -2%
COINFLIP_COOLDOWN = timedelta(seconds=8)
# league: (win chance, payout multiple of wager on a win). EV = chance*mult - (1-chance)... all slightly negative.
LEAGUES = {"bronze": (0.60, 1.5), "silver": (0.45, 2.0), "gold": (0.30, 3.0), "diamond": (0.15, 6.0)}
LEAGUE_COOLDOWN = timedelta(seconds=15)
ROB_COOLDOWN = timedelta(hours=1)
ROB_SHIELD = timedelta(hours=6)       # a victim can't be hit again for this long after being robbed
ROB_SUCCESS = 0.35
ROB_MIN_VICTIM = 200
ROB_STEAL_PCT = (0.05, 0.15)
ROB_MAX_STEAL = 5_000
ROB_FINE_PCT = 0.10                   # failed attempt: fine paid to the victim (coins are conserved)


def _wager_ok(wager: int):
    if not (MIN_WAGER <= wager <= MAX_WAGER):
        raise StoreError("bad_wager", f"Wager must be between {MIN_WAGER} and {MAX_WAGER:,}.")


async def _cooldown(conn, user_id, clone_id, game, span, now, stamp=True):
    row = await conn.fetchrow("SELECT last_at FROM card_game_state WHERE user_id=$1 AND COALESCE(clone_id,-1)=$2 "
                              "AND game=$3 FOR UPDATE", user_id, _clone_key(clone_id), game)
    if row and now < row["last_at"] + span:
        wait = int((row["last_at"] + span - now).total_seconds()) + 1
        raise StoreError("cooldown", f"Try again in {wait}s.")
    if stamp:
        await _stamp(conn, user_id, clone_id, game, now)


async def _stamp(conn, user_id, clone_id, game, now):
    await conn.execute(
        "INSERT INTO card_game_state (user_id, clone_id, game, last_at) VALUES ($1,$2,$3,$4) "
        "ON CONFLICT (user_id, COALESCE(clone_id,-1), game) DO UPDATE SET last_at = EXCLUDED.last_at",
        user_id, clone_id, game, now)


async def _log(conn, user_id, clone_id, game, wager, won, delta, target=None):
    await conn.execute("INSERT INTO card_game_log (user_id, clone_id, game, target_id, wager, won, delta) "
                       "VALUES ($1,$2,$3,$4,$5,$6,$7)", user_id, clone_id, game, target, wager, won, delta)


async def _wager_game(user_id, clone_id, game, wager, chance, mult, cooldown, rng, now):
    _wager_ok(wager)
    rng = rng or random.random
    now = now or datetime.now(timezone.utc)
    pool = await _ce._pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await _lock_balances(conn, clone_id, user_id)          # serialises this player's plays
            await _cooldown(conn, user_id, clone_id, game, cooldown, now)
            won = rng() < chance
            if won:
                gain = int(wager * mult)
                bal = await _ce._credit(conn, user_id, clone_id, gain, game, "win")
                delta = gain
            else:
                bal = await _ce._debit(conn, user_id, clone_id, wager, game, "loss")   # raises 'funds' if short
                delta = -wager
            await _log(conn, user_id, clone_id, game, wager, won, delta)
            return {"won": won, "delta": delta, "balance": bal}


async def coin_flip(user_id, clone_id, wager, rng=None, now=None) -> dict:
    return await _wager_game(user_id, clone_id, "coinflip", wager, COINFLIP_WIN_CHANCE, 1.0, COINFLIP_COOLDOWN, rng, now)


async def league_flip(user_id, clone_id, league, wager, rng=None, now=None) -> dict:
    if league not in LEAGUES:
        raise StoreError("bad_league", "Unknown league.")
    chance, mult = LEAGUES[league]
    return await _wager_game(user_id, clone_id, "leagueflip", wager, chance, mult, LEAGUE_COOLDOWN, rng, now)


async def rob(thief_id, victim_id, clone_id, rng=None, now=None) -> dict:
    if thief_id == victim_id:
        raise StoreError("self", "You can't rob yourself.")
    rng = rng or random.random
    now = now or datetime.now(timezone.utc)
    pool = await _ce._pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await _lock_balances(conn, clone_id, thief_id, victim_id)
            await _cooldown(conn, thief_id, clone_id, "rob", ROB_COOLDOWN, now)
            await _cooldown(conn, victim_id, clone_id, "robbed", ROB_SHIELD, now, stamp=False)
            vbal = await conn.fetchval("SELECT balance FROM card_coins WHERE user_id=$1 AND COALESCE(clone_id,-1)=$2",
                                       victim_id, _clone_key(clone_id))
            if vbal < ROB_MIN_VICTIM:
                raise StoreError("poor_target", "That user doesn't have enough coins to be worth robbing.")
            if rng() < ROB_SUCCESS:
                pct = ROB_STEAL_PCT[0] + (ROB_STEAL_PCT[1] - ROB_STEAL_PCT[0]) * rng()
                amount = max(1, min(int(vbal * pct), ROB_MAX_STEAL))
                await _ce._debit(conn, victim_id, clone_id, amount, "robbed", f"by:{thief_id}")
                await _ce._credit(conn, thief_id, clone_id, amount, "rob", f"from:{victim_id}")
                await _stamp(conn, victim_id, clone_id, "robbed", now)
                await _log(conn, thief_id, clone_id, "rob", 0, True, amount, victim_id)
                return {"success": True, "amount": amount}
            tbal = await conn.fetchval("SELECT balance FROM card_coins WHERE user_id=$1 AND COALESCE(clone_id,-1)=$2",
                                       thief_id, _clone_key(clone_id))
            fine = min(tbal, max(1, int(vbal * ROB_FINE_PCT)), ROB_MAX_STEAL)
            if fine:
                await _ce._debit(conn, thief_id, clone_id, fine, "rob_fine", f"to:{victim_id}")
                await _ce._credit(conn, victim_id, clone_id, fine, "rob_fine", f"from:{thief_id}")
            await _log(conn, thief_id, clone_id, "rob", 0, False, -fine, victim_id)
            return {"success": False, "amount": fine}
