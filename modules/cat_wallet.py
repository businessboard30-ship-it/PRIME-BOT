# path: modules/cat_wallet.py

"""
Cat wallet: play coins and diamonds (virtual only, never bought with or sold for real money).

  * Every cat a person adopts comes with a random reward: usually coins, sometimes a diamond.
  * 1 diamond converts into 10,000 coins (one tap in "My cats").
  * Coins are spent on the cats' care (water, a bed, food), added in a later phase.
  * A wallet belongs to one person on one bot (main bot vs each clone), like the cats themselves.
  * Coins move between people only through the cat market (modules/cat_market.py): a buyer pays the price the seller
    asked and the seller receives exactly that. Wallets cannot be gifted, and diamonds are never traded.

Pure rules live at the top (no database), the wallet queries at the bottom.
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

DIAMOND_COINS = 10_000
DIAMOND_CHANCE_PERCENT = 3                  # of every adoption reward
COIN_MIN, COIN_MAX = 50, 500                # before the rarity multiplier
RARITY_MULTIPLIER = {"common": 1.0, "uncommon": 1.5, "rare": 2.5, "legendary": 5.0}
COINS = "🪙"
DIAMOND = "💎"


@dataclass(frozen=True)
class Reward:
    kind: str                               # "coins" | "diamond"
    amount: int


@dataclass(frozen=True)
class Wallet:
    coins: int = 0
    diamonds: int = 0


def roll_reward(rarity: str, rng=random) -> Reward:
    """What comes with an adopted cat: a diamond (DIAMOND_CHANCE_PERCENT %), otherwise 50-500 coins times the cat's
    rarity (common x1 ... legendary x5)."""
    if rng.random() * 100 < DIAMOND_CHANCE_PERCENT:
        return Reward("diamond", 1)
    base = rng.randint(COIN_MIN, COIN_MAX)
    return Reward("coins", max(1, int(round(base * RARITY_MULTIPLIER.get(rarity, 1.0)))))


def reward_text(reward: Optional[Reward]) -> str:
    if reward is None:
        return ""
    if reward.kind == "diamond":
        return (f"{DIAMOND} It also came with a **diamond**! It's worth {DIAMOND_COINS:,} coins, "
                "convert it in **My cats**.")
    return f"{COINS} It also came with **{reward.amount:,} coins**!"


def wallet_text(w: Wallet) -> str:
    return f"{COINS} {w.coins:,} coins · {DIAMOND} {w.diamonds:,} diamond{'s' if w.diamonds != 1 else ''}"


# ── database ──────────────────────────────────────────────────────────────

async def _pool():
    from database import get_pool
    return await get_pool()


SHARED_KEY = -1                  # coins and diamonds are one wallet per person across the main bot and every clone


def _key(clone_id) -> int:
    return -1 if clone_id is None else int(clone_id)


async def add_reward(conn, owner_id: int, clone_id, reward: Reward) -> None:
    """Add a reward inside the caller's transaction (so it commits or rolls back together with the adoption)."""
    coins = reward.amount if reward.kind == "coins" else 0
    diamonds = reward.amount if reward.kind == "diamond" else 0
    clone_id = None                         # one wallet for every bot (main bot and all clones)
    await conn.execute(
        "INSERT INTO cat_wallets (owner_id, clone_id, coins, diamonds) VALUES ($1, $2, $3, $4) "
        "ON CONFLICT (owner_id, (COALESCE(clone_id, -1))) DO UPDATE SET "
        "coins = cat_wallets.coins + EXCLUDED.coins, diamonds = cat_wallets.diamonds + EXCLUDED.diamonds, "
        "updated_at = NOW()", owner_id, clone_id, coins, diamonds)


async def get_wallet(owner_id: int, clone_id) -> Wallet:
    pool = await _pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT coins, diamonds FROM cat_wallets WHERE owner_id = $1 AND COALESCE(clone_id, -1) = $2",
                                  owner_id, SHARED_KEY)
    return Wallet(int(row["coins"]), int(row["diamonds"])) if row else Wallet()


async def safe_wallet(owner_id: int, clone_id) -> Wallet:
    """get_wallet that never raises: a wallet problem must not stop the collection from opening."""
    try:
        return await get_wallet(owner_id, clone_id)
    except Exception:
        logger.debug("[cat-wallet] couldn't read the wallet", exc_info=True)
        return Wallet()


async def convert(owner_id: int, clone_id, count: Optional[int] = 1) -> Optional[Wallet]:
    """Turn `count` diamonds into coins (count=None: all of them). One atomic statement, so two quick taps can never
    spend the same diamond twice. Returns the new wallet, or None when there aren't enough diamonds."""
    pool = await _pool()
    async with pool.acquire() as conn:
        if count is None:
            row = await conn.fetchrow(
                "UPDATE cat_wallets SET coins = coins + diamonds::bigint * $3, diamonds = 0, updated_at = NOW() "
                "WHERE owner_id = $1 AND COALESCE(clone_id, -1) = $2 AND diamonds >= 1 RETURNING coins, diamonds",
                owner_id, SHARED_KEY, DIAMOND_COINS)
        else:
            row = await conn.fetchrow(
                "UPDATE cat_wallets SET diamonds = diamonds - $3, coins = coins + $3::bigint * $4, updated_at = NOW() "
                "WHERE owner_id = $1 AND COALESCE(clone_id, -1) = $2 AND diamonds >= $3 RETURNING coins, diamonds",
                owner_id, SHARED_KEY, int(count), DIAMOND_COINS)
    return Wallet(int(row["coins"]), int(row["diamonds"])) if row else None
