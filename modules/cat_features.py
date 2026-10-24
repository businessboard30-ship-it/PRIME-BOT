"""Persistent cat-game features: daily care, cosmetics, and public showcases.

All writes are transactional and scoped to the owner's bot/clone wallet. Cosmetics
belong to the owner account, not to a cat, so selling or releasing a cat never
removes access to purchased items.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta, timezone, datetime
from typing import Optional

from modules import cat_adopt, cat_wallet
import logging

logger = logging.getLogger(__name__)

CARE_BASE_COINS = 25
CARE_STREAK_CAP = 30


@dataclass(frozen=True)
class CareResult:
    status: str  # ok | already
    streak: int
    coins: int
    diamonds: int


@dataclass(frozen=True)
class Cosmetic:
    id: int
    slug: str
    kind: str
    name: str
    emoji: str
    price_coins: int
    price_diamonds: int


async def _pool():
    from database import get_pool
    return await get_pool()


def _key(clone_id) -> int:
    return -1 if clone_id is None else int(clone_id)


async def care(owner_id: int, clone_id, today: Optional[date] = None) -> CareResult:
    """Claim one UTC-day reward exactly once, with a bounded consecutive-day streak."""
    day = today or datetime.now(timezone.utc).date()
    pool = await _pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            previous = await conn.fetchrow(
                "SELECT streak, care_date FROM cat_daily_care WHERE owner_id=$1 AND COALESCE(clone_id,-1)=$2 "
                "ORDER BY care_date DESC LIMIT 1 FOR UPDATE", owner_id, _key(clone_id))
            if previous and previous["care_date"] == day:
                return CareResult("already", int(previous["streak"]), 0, 0)
            streak = min(CARE_STREAK_CAP, int(previous["streak"]) + 1) if previous and previous["care_date"] == day - timedelta(days=1) else 1
            coins = CARE_BASE_COINS + (streak * 5)
            diamonds = 1 if streak % 7 == 0 else 0
            await conn.execute(
                "INSERT INTO cat_daily_care(owner_id,clone_id,care_date,streak,coins_reward,diamonds_reward) VALUES($1,$2,$3,$4,$5,$6)",
                owner_id, clone_id, day, streak, coins, diamonds)
            await conn.execute(
                "INSERT INTO cat_wallets(owner_id,clone_id,coins,diamonds) VALUES($1,$2,$3,$4) "
                "ON CONFLICT (owner_id, (COALESCE(clone_id,-1))) DO UPDATE SET coins=cat_wallets.coins+EXCLUDED.coins, diamonds=cat_wallets.diamonds+EXCLUDED.diamonds, updated_at=NOW()",
                owner_id, None, coins, diamonds)      # one wallet for every bot
    return CareResult("ok", streak, coins, diamonds)


async def cosmetics() -> list[Cosmetic]:
    pool = await _pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM cat_cosmetics ORDER BY kind, price_diamonds, price_coins, id")
    return [Cosmetic(int(r["id"]), r["slug"], r["kind"], r["name"], r["emoji"], int(r["price_coins"]), int(r["price_diamonds"])) for r in rows]


async def buy_cosmetic(owner_id: int, clone_id, cosmetic_id: int) -> str:
    """Purchase with an atomic wallet debit; returns ok, owned, missing, or poor."""
    pool = await _pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            item = await conn.fetchrow("SELECT * FROM cat_cosmetics WHERE id=$1 FOR UPDATE", cosmetic_id)
            if not item:
                return "missing"
            if await conn.fetchval("SELECT 1 FROM cat_cosmetic_ownership WHERE owner_id=$1 AND COALESCE(clone_id,-1)=$2 AND cosmetic_id=$3", owner_id, _key(clone_id), cosmetic_id):
                return "owned"
            wallet = await conn.fetchrow("SELECT coins, diamonds FROM cat_wallets WHERE owner_id=$1 AND COALESCE(clone_id,-1)=$2 FOR UPDATE", owner_id, -1)
            if not wallet or int(wallet["coins"]) < int(item["price_coins"]) or int(wallet["diamonds"]) < int(item["price_diamonds"]):
                return "poor"
            await conn.execute("UPDATE cat_wallets SET coins=coins-$3, diamonds=diamonds-$4, updated_at=NOW() WHERE owner_id=$1 AND COALESCE(clone_id,-1)=$2", owner_id, -1, int(item["price_coins"]), int(item["price_diamonds"]))
            await conn.execute("INSERT INTO cat_cosmetic_ownership(owner_id,clone_id,cosmetic_id) VALUES($1,$2,$3)", owner_id, clone_id, cosmetic_id)
    return "ok"


async def wardrobe(owner_id: int, clone_id) -> tuple:
    """(owned cosmetics, ids of the ones being worn). Never raises: the shop still works without it."""
    try:
        pool = await _pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT c.*, (e.cosmetic_id IS NOT NULL) AS worn FROM cat_cosmetic_ownership o "
                "JOIN cat_cosmetics c ON c.id = o.cosmetic_id "
                "LEFT JOIN cat_equipped e ON e.owner_id = o.owner_id AND e.clone_key = o.clone_key AND e.cosmetic_id = c.id "
                "WHERE o.owner_id = $1 AND o.clone_key = $2 ORDER BY c.kind, c.id", owner_id, _key(clone_id))
    except Exception:
        logger.warning("swallowed exception in wardrobe", exc_info=True)
        return [], set()
    owned = [Cosmetic(int(r["id"]), r["slug"], r["kind"], r["name"], r["emoji"], int(r["price_coins"]), int(r["price_diamonds"])) for r in rows]
    return owned, {int(r["id"]) for r in rows if r["worn"]}


async def toggle_equip(owner_id: int, clone_id, cosmetic_id: int) -> str:
    """Wear an owned cosmetic (it replaces what is worn of the same kind), or take it off if it is already worn.
    Returns equipped | removed | not_owned | missing."""
    pool = await _pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            item = await conn.fetchrow("SELECT id, kind FROM cat_cosmetics WHERE id = $1", cosmetic_id)
            if item is None:
                return "missing"
            if not await conn.fetchval("SELECT 1 FROM cat_cosmetic_ownership WHERE owner_id = $1 AND clone_key = $2 AND cosmetic_id = $3",
                                       owner_id, _key(clone_id), cosmetic_id):
                return "not_owned"
            worn = await conn.fetchval("SELECT cosmetic_id FROM cat_equipped WHERE owner_id = $1 AND clone_key = $2 AND kind = $3 FOR UPDATE",
                                       owner_id, _key(clone_id), item["kind"])
            if worn is not None and int(worn) == int(cosmetic_id):
                await conn.execute("DELETE FROM cat_equipped WHERE owner_id = $1 AND clone_key = $2 AND kind = $3",
                                   owner_id, _key(clone_id), item["kind"])
                return "removed"
            await conn.execute(
                "INSERT INTO cat_equipped(owner_id, clone_id, kind, cosmetic_id) VALUES($1,$2,$3,$4) "
                "ON CONFLICT (owner_id, clone_key, kind) DO UPDATE SET cosmetic_id = EXCLUDED.cosmetic_id, equipped_at = NOW()",
                owner_id, clone_id, item["kind"], cosmetic_id)
            return "equipped"


async def equipped(owner_id: int, clone_id) -> dict:
    """{kind: {"slug", "name", "emoji"}} of what is worn AND still owned. Never raises."""
    try:
        pool = await _pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT c.kind, c.slug, c.name, c.emoji FROM cat_equipped e JOIN cat_cosmetics c ON c.id = e.cosmetic_id "
                "JOIN cat_cosmetic_ownership o ON o.cosmetic_id = e.cosmetic_id AND o.owner_id = e.owner_id AND o.clone_key = e.clone_key "
                "WHERE e.owner_id = $1 AND e.clone_key = $2", owner_id, _key(clone_id))
        return {r["kind"]: {"slug": r["slug"], "name": r["name"], "emoji": r["emoji"]} for r in rows}
    except Exception:
        logger.warning("swallowed exception in equipped", exc_info=True)
        return {}


async def public_profile(owner_id: int, clone_id) -> dict:
    pool = await _pool()
    async with pool.acquire() as conn:
        profile = await conn.fetchrow("SELECT * FROM cat_profiles WHERE owner_id=$1 AND COALESCE(clone_id,-1)=$2", owner_id, _key(clone_id))
        cats = await conn.fetch("SELECT * FROM cat_adoptions WHERE owner_id=$1 AND COALESCE(clone_id,-1)=$2 ORDER BY id", owner_id, _key(clone_id))
    cats = [cat_adopt.Cat.from_row(row) for row in cats]
    rarest = max(cats, key=lambda cat: cat_adopt.RARITY_RANK.get(cat.rarity, 0), default=None)
    pinned_ids = set((profile["pinned_cat_ids"] if profile else [])[:3])
    pinned = [cat for cat in cats if cat.id in pinned_ids]
    return {"owner_id": owner_id, "bio": profile["bio"] if profile else "", "show_profile": bool(profile["show_profile"]) if profile else True, "collection_size": len(cats), "rarest": rarest, "pinned": pinned,
            "equipped": await equipped(owner_id, clone_id)}


async def save_profile(owner_id: int, clone_id, bio: str, pinned_cat_ids: list[int], show_profile: bool = True) -> None:
    pool = await _pool()
    async with pool.acquire() as conn:
        owned = await conn.fetch("SELECT id FROM cat_adoptions WHERE owner_id=$1 AND COALESCE(clone_id,-1)=$2 AND id=ANY($3::bigint[])", owner_id, _key(clone_id), pinned_cat_ids[:3])
        pinned = [int(row["id"]) for row in owned]
        await conn.execute(
            "INSERT INTO cat_profiles(owner_id,clone_id,bio,pinned_cat_ids,show_profile) VALUES($1,$2,$3,$4,$5) ON CONFLICT (owner_id,clone_key) DO UPDATE SET bio=EXCLUDED.bio,pinned_cat_ids=EXCLUDED.pinned_cat_ids,show_profile=EXCLUDED.show_profile,updated_at=NOW()",
            owner_id, clone_id, str(bio or "")[:280], pinned, show_profile)
