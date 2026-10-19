# path: modules/cat_primole.py

"""
Primole 🔮: the cat game's second play currency (virtual only, never bought with or sold for real money).

  * Lives on the shared cat wallet (cat_wallets.primole), one balance per person for the main bot and every clone.
  * Only way in for players: convert diamonds, 1 💎 = 100 🔮. ONE WAY: Primole never turns back into diamonds or coins.
    (grant() is for league season rewards and top-tier quests only, never fights, raids or the market.)
  * Account-bound: it cannot be traded, gifted, listed on the market or paid in fights. Nothing in this module moves
    Primole from one person to another.
  * Every change is one atomic statement (or runs inside the caller's transaction), so two quick taps can never spend
    the same balance twice.
"""

from __future__ import annotations

import logging
from typing import Optional

from modules import cat_wallet
from modules.cat_wallet import SHARED_KEY, Wallet

logger = logging.getLogger(__name__)

PRIMOLE = "🔮"
NAME = "Primole"
PRIMOLE_PER_DIAMOND = 100            # 1 diamond is worth 10,000 coins, or 100 Primole: the player picks, both are final
MAX_CONVERT_AT_ONCE = 1_000          # sanity bound on one conversion


def primole_text(amount: int) -> str:
    return f"{PRIMOLE} {int(amount):,} {NAME}"


def convert_note() -> str:
    return (f"{cat_wallet.DIAMOND} 1 diamond = {PRIMOLE} {PRIMOLE_PER_DIAMOND} {NAME}. "
            "**This cannot be undone**, Primole never turns back into diamonds or coins.")


async def _pool():
    from database import get_pool
    return await get_pool()


async def convert_diamonds(owner_id: int, count: Optional[int] = 1) -> Optional[Wallet]:
    """Turn `count` diamonds into Primole (count=None: all of them). One atomic UPDATE; None when there aren't enough
    diamonds (or count is not a positive number). There is deliberately no function that goes the other way."""
    if count is not None:
        count = int(count)
        if count < 1 or count > MAX_CONVERT_AT_ONCE:
            return None
    pool = await _pool()
    async with pool.acquire() as conn:
        if count is None:
            row = await conn.fetchrow(
                "UPDATE cat_wallets SET primole = primole + LEAST(diamonds, $3)::bigint * $4, "
                "diamonds = diamonds - LEAST(diamonds, $3), updated_at = NOW() "
                "WHERE owner_id = $1 AND COALESCE(clone_id, -1) = $2 AND diamonds >= 1 "
                "RETURNING coins, diamonds, primole",
                owner_id, SHARED_KEY, MAX_CONVERT_AT_ONCE, PRIMOLE_PER_DIAMOND)
        else:
            row = await conn.fetchrow(
                "UPDATE cat_wallets SET diamonds = diamonds - $3, primole = primole + $3::bigint * $4, updated_at = NOW() "
                "WHERE owner_id = $1 AND COALESCE(clone_id, -1) = $2 AND diamonds >= $3 "
                "RETURNING coins, diamonds, primole",
                owner_id, SHARED_KEY, count, PRIMOLE_PER_DIAMOND)
    return Wallet(int(row["coins"]), int(row["diamonds"]), int(row["primole"])) if row else None


async def spend(conn, owner_id: int, amount: int) -> Optional[int]:
    """Spend Primole inside the caller's transaction (so it commits or rolls back with whatever it buys).
    Returns the new balance, or None when the balance is too small or the amount is not positive."""
    amount = int(amount)
    if amount < 1:
        return None
    row = await conn.fetchrow(
        "UPDATE cat_wallets SET primole = primole - $3, updated_at = NOW() "
        "WHERE owner_id = $1 AND COALESCE(clone_id, -1) = $2 AND primole >= $3 RETURNING primole",
        owner_id, SHARED_KEY, amount)
    return int(row["primole"]) if row else None


async def grant(conn, owner_id: int, amount: int) -> None:
    """Add Primole inside the caller's transaction. Only for league season rewards and top-tier quests/badges."""
    amount = int(amount)
    if amount < 1:
        return
    await conn.execute(
        "INSERT INTO cat_wallets (owner_id, clone_id, coins, diamonds, primole) VALUES ($1, NULL, 0, 0, $2) "
        "ON CONFLICT (owner_id, (COALESCE(clone_id, -1))) DO UPDATE SET "
        "primole = cat_wallets.primole + EXCLUDED.primole, updated_at = NOW()", owner_id, amount)


async def balance(owner_id: int) -> int:
    pool = await _pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT primole FROM cat_wallets WHERE owner_id = $1 AND COALESCE(clone_id, -1) = $2", owner_id, SHARED_KEY)
    return int(row["primole"]) if row else 0
