# path: modules/cat_rank.py

"""
Cat ranks (play game only, coins only, never real money).

  * Every cat starts at rank 0, the old ones and the new ones alike. Spend coins to rank a cat up, one step at a time,
    up to rank 25. The rank stays with the cat when it is traded or sold, so a ranked cat is worth more on the market.
  * Rarer cats cost more to rank up (same multiplier as their adoption rewards): common x1, uncommon x1.5, rare x2.5,
    legendary x5.
  * Every rank adds 2 points to its owner's Collectors score on the /cat leaderboard.
  * One database transaction checks the owner, the rank the person was looking at, that the cat isn't for sale, and
    takes the coins, so two quick taps can never charge twice or skip a rank.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from modules import cat_wallet

logger = logging.getLogger(__name__)

MAX_RANK = 25
RANK_TITLES = (
    "Newcomer", "Rising", "Seasoned", "Champion", "Royal", "Legend",                    # ranks 0-5 (the original six)
    "Mythic", "Celestial", "Cosmic", "Stellar", "Radiant", "Eternal", "Immortal", "Divine", "Ascendant", "Sovereign",
    "Emperor", "Archon", "Titan", "Phoenix", "Dragon", "Astral", "Primordial", "Omniscient", "Transcendent", "Ultimate",
)                                                                                       # ranks 6-25
RANK_EMOJI = (
    "\u25AB\uFE0F", "\U0001F949", "\U0001F948", "\U0001F947", "\U0001F48E", "\U0001F31F",
    "\U0001F52E", "\u2728", "\U0001F30C", "\U0001F4AB", "\u2600\uFE0F", "\u267E\uFE0F", "\U0001F531", "\U0001F47C",
    "\U0001F680", "\U0001F3DB\uFE0F", "\U0001F985", "\u26A1", "\U0001F5FF", "\U0001F525", "\U0001F409",
    "\U0001F320", "\U0001F300", "\U0001F441\uFE0F", "\U0001FA90", "\U0001F3C6",
)
_ORIGINAL_COST = (0, 300, 900, 2_400, 6_000, 15_000)                                    # ranks 0-5, unchanged
_GROWTH = 1.18                                                                          # each new rank costs 18% more than the last
BASE_COST = _ORIGINAL_COST + tuple(
    int(round(15_000 * _GROWTH ** (n - 5), -2)) for n in range(6, MAX_RANK + 1))        # coins to REACH rank n, for a common cat
POINTS_PER_RANK = 2                                  # added to the Collectors leaderboard score


def clamp(rank) -> int:
    try:
        return max(0, min(int(rank), MAX_RANK))
    except (TypeError, ValueError):
        return 0


def badge(rank) -> str:
    r = clamp(rank)
    return f"{RANK_EMOJI[r]} Rank {r} \u00b7 {RANK_TITLES[r]}"


def cost_to_reach(rank: int, rarity: str) -> int:
    """Coins to reach `rank` (1..MAX_RANK) from the one below it."""
    r = clamp(rank)
    if r < 1:
        return 0
    return max(1, int(round(BASE_COST[r] * cat_wallet.RARITY_MULTIPLIER.get(rarity, 1.0))))


def next_cost(cat) -> Optional[int]:
    """What the next rank costs for this cat, or None at the top."""
    r = clamp(getattr(cat, "cat_rank", 0))
    return None if r >= MAX_RANK else cost_to_reach(r + 1, cat.rarity)


@dataclass(frozen=True)
class RankResult:
    status: str                 # ok | not_yours | stale | max | listed | poor
    rank: int = 0               # the cat's rank after this call
    cost: int = 0
    coins: int = 0              # coins left (ok) or coins the person has (poor)


def _key(clone_id) -> int:
    return -1 if clone_id is None else int(clone_id)


async def _pool():
    from database import get_pool
    return await get_pool()


async def rank_up(cat_id: int, owner_id: int, clone_id, expect_rank: int) -> RankResult:
    """Rank a cat up one step. `expect_rank` is the rank the person saw, so a stale screen can't cause a surprise."""
    pool = await _pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT owner_id, clone_id, rarity, cat_rank FROM cat_adoptions WHERE id = $1 FOR UPDATE", cat_id)
            if row is None or int(row["owner_id"]) != owner_id or _key(row["clone_id"]) != _key(clone_id):
                return RankResult("not_yours")
            rank = clamp(row["cat_rank"])
            if rank != clamp(expect_rank):
                return RankResult("stale", rank)
            if rank >= MAX_RANK:
                return RankResult("max", rank)
            if await conn.fetchval("SELECT 1 FROM cat_listings WHERE adoption_id = $1 AND status = 'open' UNION ALL SELECT 1 FROM market_listings WHERE item_type = 'cat' AND item_ref = $1 AND status = 'open'", cat_id):
                return RankResult("listed", rank)
            cost = cost_to_reach(rank + 1, row["rarity"])
            left = await conn.fetchval(
                "UPDATE cat_wallets SET coins = coins - $3, updated_at = NOW() "
                "WHERE owner_id = $1 AND COALESCE(clone_id, -1) = $2 AND coins >= $3 RETURNING coins",
                owner_id, -1, cost)                 # -1: the wallet is shared by every bot
            if left is None:
                have = await conn.fetchval(
                    "SELECT coins FROM cat_wallets WHERE owner_id = $1 AND COALESCE(clone_id, -1) = $2",
                    owner_id, -1)
                return RankResult("poor", rank, cost, int(have or 0))
            await conn.execute("UPDATE cat_adoptions SET cat_rank = $2 WHERE id = $1", cat_id, rank + 1)
    return RankResult("ok", rank + 1, cost, int(left))
