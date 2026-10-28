# path: modules/card_boost.py

"""
Card boosting: coins + dedicated boost items, capped strength curve, skill unlocks.

Rules (all in the tables below so new items/rarities need no logic change):
  * A boost raises card_copies.boost_level by exactly 1. The level cap is card_catalog.max_boost_level.
  * Levels below COIN_ONLY_LEVELS can be bought with coins alone (cost grows with level and rarity).
  * Higher levels REQUIRE the item tier named in ITEM_FOR_LEVEL (plus coins); rare items are granted, not sold,
    so the strongest upgrades can't be bought without limit.
  * Any level may instead use a matching-or-better item in place of its coin cost (item_cost_waived).
  * Strength = base * (1 + MAX_BONUS * (1 - DECAY ** level)): diminishing returns, hard ceiling base*(1+MAX_BONUS).
  * Rented copies can't be boosted. Only the owner can boost. Everything is one row-locked transaction.
"""

from __future__ import annotations

import math
from typing import Optional

from modules import card_economy as _ce
from modules.card_economy import StoreError, _clone_key, _debit

MAX_BONUS = 1.0          # at most +100% of base strength, ever
DECAY = 0.82             # closer to 1 = slower curve
COIN_ONLY_LEVELS = 5     # levels 0..4 -> 1..5 purchasable with coins alone
BASE_COST = 100
RARITY_COST_MULT = {"common": 1, "rare": 2, "epic": 4, "legendary": 8, "god": 20, "supreme": 40}

# item_key -> tier rank (higher = rarer). Granted by drops/quests/admin; only "boost_shard" is meant to be sold.
ITEM_TIERS = {"boost_shard": 1, "boost_core": 2, "boost_apex": 3}
# target level (the level being reached) -> minimum item tier required. Missing = no item required.
ITEM_FOR_LEVEL = {6: 2, 7: 2, 8: 2, 9: 3, 10: 3}


def strength(base: int, level: int) -> int:
    return int(math.floor(base * (1 + MAX_BONUS * (1 - DECAY ** max(0, level))) + 0.5))   # half-up, same as SQL


def coin_cost(rarity: str, current_level: int) -> int:
    return int(BASE_COST * RARITY_COST_MULT.get(rarity, 1) * (1 + current_level * 0.5))


def active_skills(skills: list, level: int) -> list:
    """skills: [{"key","name","unlock_level"}]; a skill is active once boost_level >= unlock_level."""
    return [s for s in (skills or []) if level >= int(s.get("unlock_level", 0))]


def next_milestone(skills: list, level: int) -> Optional[dict]:
    later = sorted((s for s in (skills or []) if int(s.get("unlock_level", 0)) > level),
                   key=lambda s: int(s["unlock_level"]))
    return later[0] if later else None


def required_tier(target_level: int) -> int:
    return ITEM_FOR_LEVEL.get(target_level, 0)


async def grant_item(user_id: int, clone_id: Optional[int], item_key: str, qty: int = 1) -> int:
    if item_key not in ITEM_TIERS:
        raise StoreError("bad_item", "Unknown boost item.")
    if qty <= 0:
        raise StoreError("bad_amount", "Quantity must be positive.")
    pool = await _ce._pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO user_boost_items (user_id, clone_id, item_key, quantity) VALUES ($1,$2,$3,$4) "
            "ON CONFLICT (user_id, COALESCE(clone_id, -1), item_key) DO UPDATE "
            "SET quantity = user_boost_items.quantity + EXCLUDED.quantity RETURNING quantity",
            user_id, clone_id, item_key, qty)
        return row["quantity"]


async def boost_card(user_id: int, clone_id: Optional[int], copy_id: int, use_item: Optional[str] = None) -> dict:
    """Raise one copy by one level. use_item=None pays coins; an item key consumes one of that item.
    Raises StoreError(code) on any failed rule; nothing is spent on failure."""
    if use_item is not None and use_item not in ITEM_TIERS:
        raise StoreError("bad_item", "Unknown boost item.")
    pool = await _ce._pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            copy = await conn.fetchrow("SELECT * FROM card_copies WHERE copy_id = $1 FOR UPDATE", copy_id)
            if copy is None or copy["owner_id"] != user_id:
                raise StoreError("not_owner", "You don't own that card.")
            if copy["rental_expires_at"] is not None:
                raise StoreError("rented", "Rented cards can't be boosted.")
            if copy["trade_locked"]:
                raise StoreError("locked", "This card is in a pending trade.")
            card = await conn.fetchrow("SELECT * FROM card_catalog WHERE card_id = $1", copy["card_id"])
            cur = copy["boost_level"]
            if cur >= card["max_boost_level"]:
                raise StoreError("max_boost", "This card is at its boost limit.")
            target = cur + 1
            need = required_tier(target)
            tier = ITEM_TIERS[use_item] if use_item else 0
            if need and tier < need:
                raise StoreError("item_required", "This upgrade needs a higher-tier boost item.")
            coins = 0
            if use_item:
                row = await conn.fetchrow(
                    "UPDATE user_boost_items SET quantity = quantity - 1 WHERE user_id = $1 "
                    "AND COALESCE(clone_id, -1) = $2 AND item_key = $3 AND quantity >= 1 RETURNING quantity",
                    user_id, _clone_key(clone_id), use_item)
                if row is None:
                    raise StoreError("no_item", "You don't have that boost item.")
            else:
                coins = coin_cost(card["rarity"], cur)
                await _debit(conn, user_id, clone_id, coins, "card_boost", f"copy:{copy_id}")
            await conn.execute("UPDATE card_copies SET boost_level = $2 WHERE copy_id = $1", copy_id, target)
            await conn.execute(
                "INSERT INTO card_boost_log (copy_id, user_id, from_level, to_level, coins_spent, item_key) "
                "VALUES ($1,$2,$3,$4,$5,$6)", copy_id, user_id, cur, target, coins, use_item)
            skills = card["skills"] if isinstance(card["skills"], list) else __import__("json").loads(card["skills"])
            return {"copy_id": copy_id, "level": target, "strength": strength(card["base_strength"], target),
                    "coins_spent": coins, "item": use_item, "skills": active_skills(skills, target),
                    "next": next_milestone(skills, target)}


async def list_items(user_id: int, clone_id: Optional[int]) -> dict:
    """{item_key: quantity} for items the person holds (quantity > 0)."""
    pool = await _ce._pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT item_key, quantity FROM user_boost_items WHERE user_id = $1 AND COALESCE(clone_id,-1) = $2 "
            "AND quantity > 0", user_id, _clone_key(clone_id))
    return {r["item_key"]: r["quantity"] for r in rows if r["item_key"] in ITEM_TIERS}


def next_boost_plan(rarity: str, level: int, max_level: int) -> Optional[dict]:
    """What the NEXT boost needs: None at the cap, else {target, coins, tier} (tier 0 = coins alone are enough)."""
    if level >= max_level:
        return None
    target = level + 1
    return {"target": target, "coins": coin_cost(rarity, level), "tier": required_tier(target)}
