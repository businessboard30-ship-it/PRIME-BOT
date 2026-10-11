# path: modules/cat_adopt.py

"""
Cat adoption (virtual only: the coins and diamonds come from playing, never from real money; see modules/cat_wallet.py).

  * Every automatic cat spawn carries an Adopt button. The first person to tap it adopts that cat.
  * An adopted cat gets a cute random name, a rarity and a personality, and goes to the person's collection.
  * Cats can be renamed, released, gifted, or swapped with another member (the other person must accept).

Collections and wallets are per bot (main bot vs each clone), but the cats themselves are shared: a picture can be
adopted only once across ALL bots, and cats can be traded or sold between the main bot and the clones.
"""

from __future__ import annotations

import logging
import random
import re
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional

from modules import cat_wallet

logger = logging.getLogger(__name__)

SPAWN_EXPIRE_HOURS = 6            # an unadopted cat wanders off after this long (proposal, adjust freely)
EXPIRED_NOTE = "💨 This cat wandered off before anyone adopted it. Watch for the next one!"
MAX_CATS = 100                  # per person, per bot (lists page through them, so any number is fine)
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
    cat_rank: int = 0               # 0-5, bought with coins (see modules/cat_rank.py)
    hungry_since: Optional[datetime] = None     # care (modules/cat_care.py): None = not hungry
    watered_until: Optional[datetime] = None
    bed_until: Optional[datetime] = None

    @classmethod
    def from_row(cls, r) -> "Cat":
        try:
            rank = int(r["cat_rank"] or 0)
        except (KeyError, IndexError):
            rank = 0
        def opt(key):
            try:
                return r[key]
            except (KeyError, IndexError):
                return None
        return cls(id=int(r["id"]), owner_id=int(r["owner_id"]), clone_id=r["clone_id"], pool_id=int(r["pool_id"]),
                   name=r["name"], rarity=r["rarity"], trait=r["trait"], adopted_at=r["adopted_at"],
                   traded_count=int(r["traded_count"] or 0), cat_rank=rank, hungry_since=opt("hungry_since"),
                   watered_until=opt("watered_until"), bed_until=opt("bed_until"))


@dataclass(frozen=True)
class AdoptResult:
    status: str                 # ok | taken | gone | limit | expired
    cat: Optional[Cat] = None
    reward: Optional[cat_wallet.Reward] = None      # coins or a diamond that came with the cat


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


async def expire_spawns(clone_id, limit: int = 20) -> list:
    """Closes this bot's unadopted spawns that are older than SPAWN_EXPIRE_HOURS (0 = nobody, like a closed duplicate)
    and returns them so their messages can be updated. A returned runaway whose spawn faded goes back in the queue."""
    pool = await _pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            rows = await conn.fetch(
                "UPDATE cat_spawns SET adopted_by = 0 WHERE id IN ("
                " SELECT id FROM cat_spawns WHERE adopted_by IS NULL AND COALESCE(clone_id, -1) = $1 "
                " AND created_at < NOW() - make_interval(hours => $2) ORDER BY id LIMIT $3 FOR UPDATE SKIP LOCKED) "
                "RETURNING id, guild_id, channel_id, message_id, pool_id", _key(clone_id), SPAWN_EXPIRE_HOURS, limit)
            for r in rows:
                await conn.execute("UPDATE cat_runaways SET returned_at = NULL WHERE pool_id = $1 AND claimed_at IS NULL",
                                   r["pool_id"])
    return [dict(r) for r in rows]


async def count_cats(user_id: int, clone_id) -> int:
    pool = await _pool()
    async with pool.acquire() as conn:
        return int(await conn.fetchval(
            "SELECT COUNT(*) FROM cat_adoptions WHERE owner_id = $1 AND COALESCE(clone_id, -1) = $2",
            user_id, _key(clone_id)) or 0)


async def adopt(spawn_id: int, user_id: int, clone_id, guild_id: Optional[int] = None) -> AdoptResult:
    """First person wins. All-or-nothing: the spawn is claimed and the cat created in one transaction."""
    name, rarity, trait = roll_name(), roll_rarity(), roll_trait()
    came_back = False
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
                "WHERE id = $1 AND adopted_by IS NULL AND created_at > NOW() - make_interval(hours => $3) "
                "RETURNING pool_id", spawn_id, user_id, SPAWN_EXPIRE_HOURS)
            if row is None:
                known = await conn.fetchrow("SELECT adopted_by FROM cat_spawns WHERE id = $1", spawn_id)
                if known is None:
                    return AdoptResult("gone")
                return AdoptResult("expired" if known["adopted_by"] is None else "taken")
            runaway = await conn.fetchrow(
                "UPDATE cat_runaways SET claimed_at = NOW() WHERE pool_id = $1 AND claimed_at IS NULL "
                "RETURNING cat_name, rarity, trait", row["pool_id"])
            if runaway is not None:             # a cat that left home keeps who it is
                name, rarity, trait, came_back = runaway["cat_name"], runaway["rarity"], runaway["trait"], True
            if await conn.fetchval("SELECT 1 FROM cat_adoptions WHERE pool_id = $1", row["pool_id"]):
                dup = True                          # this picture already has an owner (maybe on another bot)
            else:
                dup = False
                cat = await conn.fetchrow(
                    "INSERT INTO cat_adoptions (owner_id, clone_id, pool_id, name, rarity, trait, adopted_guild_id) "
                    "VALUES ($1,$2,$3,$4,$5,$6,$7) RETURNING *",
                    user_id, clone_id, row["pool_id"], name, rarity, trait, guild_id)
            if dup:
                # Close the spawn for everybody (0 = nobody) and keep that, instead of rolling it back.
                await conn.execute("UPDATE cat_spawns SET adopted_by = 0 WHERE id = $1", spawn_id)
                return AdoptResult("taken")
            reward = None
            if not came_back:                           # a cat that came back brings no reward: welcomed home, not farmed
                try:
                    async with conn.transaction():      # a savepoint: a wallet problem must never undo the adoption
                        reward = cat_wallet.roll_reward(rarity)
                        await cat_wallet.add_reward(conn, user_id, clone_id, reward)
                except Exception:
                    reward = None
                    logger.warning("[cat-adopt] couldn't grant the adoption reward", exc_info=True)
    return AdoptResult("ok", Cat.from_row(cat), reward)


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
            "UPDATE cat_adoptions SET name = $3 WHERE id = $1 AND owner_id = $2 AND NOT EXISTS "
            "(SELECT 1 FROM cat_listings l WHERE l.adoption_id = $1 AND l.status = 'open') RETURNING id",
            cat_id, owner_id, name) is not None


async def release(cat_id: int, owner_id: int) -> bool:
    pool = await _pool()
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "DELETE FROM cat_adoptions WHERE id = $1 AND owner_id = $2 AND NOT EXISTS "
            "(SELECT 1 FROM cat_listings l WHERE l.adoption_id = $1 AND l.status = 'open') RETURNING id",
            cat_id, owner_id) is not None


_SAME = object()


async def trade(give_id: int, giver_id: int, take_id: Optional[int], taker_id: int, clone_id,
                taker_clone_id=_SAME) -> str:
    """Swap `give_id` (owned by giver) for `take_id` (owned by taker), or gift it when take_id is None.
    `clone_id` is the giver's bot, `taker_clone_id` the taker's (default: the same bot). The two can differ: a cat
    that moves lands in its new owner's bot, so main bot and clones trade with each other.
    One transaction with both rows locked, so a cat can never be traded twice or while it is for sale.
    Returns ok | gone | limit."""
    if taker_clone_id is _SAME:
        taker_clone_id = clone_id
    ids = [give_id] + ([take_id] if take_id is not None else [])
    pool = await _pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            rows = await conn.fetch(
                "SELECT id, owner_id, clone_id FROM cat_adoptions WHERE id = ANY($1::bigint[]) FOR UPDATE", ids)
            have = {int(r["id"]): (int(r["owner_id"]), _key(r["clone_id"])) for r in rows}
            if have.get(give_id) != (giver_id, _key(clone_id)) or \
                    (take_id is not None and have.get(take_id) != (taker_id, _key(taker_clone_id))):
                return "gone"
            if await conn.fetchval("SELECT 1 FROM cat_listings WHERE adoption_id = ANY($1::bigint[]) "
                                   "AND status = 'open'", ids):
                return "gone"                       # a cat that is for sale can't be swapped at the same time
            if take_id is None:
                held = await conn.fetchval(
                    "SELECT COUNT(*) FROM cat_adoptions WHERE owner_id = $1 AND COALESCE(clone_id, -1) = $2",
                    taker_id, _key(taker_clone_id))
                if int(held or 0) >= MAX_CATS:
                    return "limit"
            await conn.execute("UPDATE cat_adoptions SET owner_id = $2, clone_id = $3, traded_count = traded_count + 1 "
                               "WHERE id = $1", give_id, taker_id, taker_clone_id)
            if take_id is not None:
                await conn.execute("UPDATE cat_adoptions SET owner_id = $2, clone_id = $3, "
                                   "traded_count = traded_count + 1 WHERE id = $1", take_id, giver_id, clone_id)
    return "ok"
