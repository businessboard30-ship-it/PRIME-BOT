# path: modules/card_rewards.py

"""
Level-up coin rewards: ties the leveling system to the card-coin economy.

Rules:
  * A level pays out at most ONCE per person per bot (unique key in card_levelup_rewards), no matter how many servers the
    person levels in or how often XP is lost and regained. The insert and the coin credit share one transaction, so a
    crash can't pay coins without recording it (or record it without paying).
  * reward(level) = min(BASE + PER_LEVEL * level, MAX), all in config.py. Leveling is progression, never a paid product.
  * Jumping several levels in one go pays each level, capped at MAX_CATCH_UP levels per call so a bad value can't loop.
"""

from __future__ import annotations

import logging
from typing import Optional

import config as app_config
from modules import card_economy as _ce
from modules.card_economy import _credit
from modules import card_boost as _cb

logger = logging.getLogger(__name__)

MAX_CATCH_UP = 20


def reward_for(level: int) -> int:
    base = int(app_config.CARD_LEVELUP_COIN_BASE) + int(app_config.CARD_LEVELUP_COIN_PER_LEVEL) * max(0, level)
    return max(0, min(base, int(app_config.CARD_LEVELUP_COIN_MAX)))


def item_for(level: int) -> Optional[tuple]:
    """Boost item dropped for reaching `level`, or None. Rarer items need bigger milestones: every 5th level a Shard,
    every 25th a Core, every 50th an Apex (Apex is what boosts 9-10 need, so it stays scarce). Once per person per level."""
    if not app_config.CARD_LEVELUP_ITEMS_ENABLED or level <= 0:
        return None
    if level % 50 == 0:
        return ("boost_apex", 1)
    if level % 25 == 0:
        return ("boost_core", 1)
    if level % 5 == 0:
        return ("boost_shard", 1)
    return None


async def award_levelup(user_id: int, clone_id: Optional[int], old_level: int, new_level: int) -> int:
    """Pay coins for every level in (old_level, new_level] not paid before. Returns the coins paid now (0 if none)."""
    if not app_config.CARD_LEVELUP_COINS_ENABLED or new_level <= old_level or new_level <= 0:
        return 0
    first = max(old_level + 1, new_level - MAX_CATCH_UP + 1, 1)
    pool = await _ce._pool()
    paid = 0
    async with pool.acquire() as conn:
        async with conn.transaction():
            for lvl in range(first, new_level + 1):
                coins = reward_for(lvl)
                got = await conn.fetchval(
                    "INSERT INTO card_levelup_rewards (user_id, clone_id, level, coins) VALUES ($1,$2,$3,$4) "
                    "ON CONFLICT DO NOTHING RETURNING level", user_id, clone_id, lvl, coins)
                if got is None:            # already paid for this level
                    continue
                if coins:
                    await _credit(conn, user_id, clone_id, coins, "levelup", f"level:{lvl}")
                drop = item_for(lvl)       # same transaction + same once-per-level marker => can't be farmed or half-paid
                if drop:
                    await _cb.grant_item(user_id, clone_id, drop[0], drop[1], conn=conn)
                paid += coins
    return paid
