# path: modules/cat_adopt.py

"""
Cat adoption (virtual only, there is no money, coins or purchase of any kind).

  * Every automatic cat spawn carries an Adopt button. The first person to tap it adopts that cat.
  * An adopted cat gets a cute random name, a rarity and a personality, and goes to the person's collection.
  * Cats can be renamed, released, gifted, or swapped with another member (the other person must accept).

Everything is per bot (main bot vs each clone), like the other economies in this project.
"""

from __future__ import annotations

import logging
import random
import re
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional

logger = logging.getLogger(__name__)

MAX_CATS = 30                   # per person, per bot
NAME_MAX = 20

RARITIES = (("common", 70, "🐱"), ("uncommon", 20, "😺"), ("rare", 8, "😻"), ("legendary", 2, "👑"))
RARITY_RANK = {name: i for i, (name, _, _) in enumerate(RARITIES)}
RARITY_EMOJI = {name: emoji for name, _, emoji in RARITIES}
TRAITS = ("Sleepy", "Zoomies", "Cuddly", "Curious", "Fluffy", "Mischievous", "Royal", "Playful", "Chonky",
          "Sassy", "Gentle", "Brave", "Dreamy", "Snuggly")
NAMES = ("Mochi", "Biscuit", "Luna", "Pumpkin", "Whiskers", "Nala", "Simba", "Cleo", "Peanut", "Muffin", "Oreo",
         "Mittens", "Ziggy", "Pepper", "Tofu", "Cocoa", "Bean", "Waffles", "Noodle", "Smokey", "Ginger", "Poppy",
         "Maple", "Churro", "Pixel", "Boba", "Sushi", "Misty", "Toast", "Marble", "Clover", "Fudge", "Pickles",
         "Daisy", "Bagel", "Cinnamon", "Sprout", "Ember", "Willow", "Jellybean")


def roll_rarity(rng=random) -> str:
    return rng.choices([r[0] for r in RARITIES], weights=[r[1] for r in RARITIES], k=1)[0]


def roll_name(rng=random) -> str:
    return rng.choice(NAMES)


def roll_trait(rng=random) -> str:
    return rng.choice(TRAITS)


def clean_name(raw: str) -> Optional[str]:
    """A safe cat name (letters, numbers, spaces, emoji, a few marks) of 2-20 characters, or None."""
    text = re.sub(r"[\x00-\x1f\x7f@#<>\[\]()`*_~|\\/:]", "", str(raw or ""))
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) < 2 or "http" in text.lower() or "discord.gg" in text.lower():
        return None
    return text[:NAME_MAX].strip()


def rarity_badge(rarity: str) -> str:
    return f"{RARITY_EMOJI.get(rarity, '🐱')} {rarity.title()}"


@dataclass(frozen=True)
class Cat:
    id: int
    owner_id: int
    clone_id: Optional[int]
    pool_id: int
    name: str
    rarity: str
    trait: str
    adopted_at: Optional[datetime] = None
    traded_count: int = 0

    @classmethod
    def from_row(cls, r) -> "Cat":
        return cls(id=int(r["id"]), owner_id=int(r["owner_id"]), clone_id=r["clone_id"], pool_id=int(r["pool_id"]),
                   name=r["name"], rarity=r["rarity"], trait=r["trait"], adopted_at=r["adopted_at"],
                   traded_count=int(r["traded_count"] or 0))


@dataclass(frozen=True)
class AdoptResult:
    status: str                 # ok | taken | gone | limit
    cat: Optional[Cat] = None


async def _pool():
    from database import get_pool
    return await get_pool()


def _key(clone_id) -> int:
    return -1 if clone_id is None else int(clone_id)


async def create_spawn(guild_id: int, clone_id, channel_id: int, pool_id: int) -> int:
    pool = await _pool()
    async with pool.acquire() as conn:
        return int(await conn.fetchval(
            "INSERT INTO cat_spawns (guild_id, clone_id, channel_id, pool_id) VALUES ($1,$2,$3,$4) RETURNING id",
            guild_id, clone_id, channel_id, pool_id))


async def set_spawn_message(spawn_id: int, message_id: int) -> None:
    pool = await _pool()
    async with pool.acquire() as conn:
        await conn.execute("UPDATE cat_spawns SET message_id = $2 WHERE id = $1", spawn_id, message_id)


async def count_cats(user_id: int, clone_id) -> int:
    pool = await _pool()
    async with pool.acquire() as conn:
        return int(await conn.fetchval(
            "SELECT COUNT(*) FROM cat_adoptions WHERE owner_id = $1 AND COALESCE(clone_id, -1) = $2",
            user_id, _key(clone_id)) or 0)


async def adopt(spawn_id: int, user_id: int, clone_id, guild_id: Optional[int] = None) -> AdoptResult:
    """First person wins. All-or-nothing: the spawn is claimed and the cat created in one transaction."""
    name, rarity, trait = roll_name(), roll_rarity(), roll_trait()
    pool = await _pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            owned = await conn.fetchval(
                "SELECT COUNT(*) FROM cat_adoptions WHERE owner_id = $1 AND COALESCE(clone_id, -1) = $2",
                user_id, _key(clone_id))
            if int(owned or 0) >= MAX_CATS:
                return AdoptResult("limit")
            row = await conn.fetchrow(
                "UPDATE cat_spawns SET adopted_by = $2, adopted_at = NOW() "
                "WHERE id = $1 AND adopted_by IS NULL RETURNING pool_id", spawn_id, user_id)
            if row is None:
                known = await conn.fetchval("SELECT 1 FROM cat_spawns WHERE id = $1", spawn_id)
                return AdoptResult("taken" if known else "gone")
            cat = await conn.fetchrow(
                "INSERT INTO cat_adoptions (owner_id, clone_id, pool_id, name, rarity, trait, adopted_guild_id) "
                "VALUES ($1,$2,$3,$4,$5,$6,$7) RETURNING *",
                user_id, clone_id, row["pool_id"], name, rarity, trait, guild_id)
    return AdoptResult("ok", Cat.from_row(cat))


async def list_cats(user_id: int, clone_id) -> List[Cat]:
    pool = await _pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM cat_adoptions WHERE owner_id = $1 AND COALESCE(clone_id, -1) = $2 ORDER BY adopted_at, id",
            user_id, _key(clone_id))
    cats = [Cat.from_row(r) for r in rows]
    cats.sort(key=lambda c: -RARITY_RANK.get(c.rarity, 0))          # rarest first; the query already ordered by age
    return cats


async def rename(cat_id: int, owner_id: int, new_name: str) -> bool:
    name = clean_name(new_name)
    if not name:
        return False
    pool = await _pool()
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "UPDATE cat_adoptions SET name = $3 WHERE id = $1 AND owner_id = $2 RETURNING id",
            cat_id, owner_id, name) is not None


async def release(cat_id: int, owner_id: int) -> bool:
    pool = await _pool()
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "DELETE FROM cat_adoptions WHERE id = $1 AND owner_id = $2 RETURNING id", cat_id, owner_id) is not None


async def trade(give_id: int, giver_id: int, take_id: Optional[int], taker_id: int, clone_id) -> str:
    """Swap `give_id` (owned by giver) for `take_id` (owned by taker), or gift it when take_id is None.
    One transaction with both rows locked, so a cat can never be traded twice. Returns ok | gone | limit."""
    ids = [give_id] + ([take_id] if take_id is not None else [])
    pool = await _pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            rows = await conn.fetch(
                "SELECT id, owner_id FROM cat_adoptions WHERE id = ANY($1::bigint[]) "
                "AND COALESCE(clone_id, -1) = $2 FOR UPDATE", ids, _key(clone_id))
            owners = {int(r["id"]): int(r["owner_id"]) for r in rows}
            if owners.get(give_id) != giver_id or (take_id is not None and owners.get(take_id) != taker_id):
                return "gone"
            if take_id is None:
                held = await conn.fetchval(
                    "SELECT COUNT(*) FROM cat_adoptions WHERE owner_id = $1 AND COALESCE(clone_id, -1) = $2",
                    taker_id, _key(clone_id))
                if int(held or 0) >= MAX_CATS:
                    return "limit"
            await conn.execute("UPDATE cat_adoptions SET owner_id = $2, traded_count = traded_count + 1 WHERE id = $1",
                               give_id, taker_id)
            if take_id is not None:
                await conn.execute("UPDATE cat_adoptions SET owner_id = $2, traded_count = traded_count + 1 "
                                   "WHERE id = $1", take_id, giver_id)
    return "ok"
