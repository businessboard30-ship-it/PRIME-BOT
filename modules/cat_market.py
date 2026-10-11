# path: modules/cat_market.py

"""
Cat market (play coins only, never real money).

  * A cat's owner puts it up for sale at a price. The bot posts it like a spawn (picture, price tag, Buy button).
  * The first person who can pay takes the cat; the seller receives exactly the price. One database transaction moves
    the cat and both wallets, so a cat can never be sold twice or paid for without arriving.
  * A listing belongs to the whole cat world, not to one bot: the main bot and every clone sell to each other. A cat
    lands in its new owner's collection on the bot they bought it with, and the coins move between the two wallets
    (each wallet belongs to one person on one bot).
  * A cat that is for sale can't be released or swapped until the listing is taken down.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import List, Optional

from modules import cat_adopt as ca

logger = logging.getLogger(__name__)

MIN_PRICE = 10
MAX_PRICE = 1_000_000
MAX_OPEN_PER_SELLER = 5
PAGE_SIZE = 10


def parse_price(raw) -> Optional[int]:
    """'1500', '1,500', '1.5k', '2m' -> coins, or None when it isn't a price in MIN_PRICE..MAX_PRICE."""
    text = str(raw or "").strip().lower().replace(",", "").replace(" ", "")
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([km]?)", text)
    if not m:
        return None
    value = float(m.group(1)) * {"": 1, "k": 1_000, "m": 1_000_000}[m.group(2)]
    price = int(round(value))
    return price if MIN_PRICE <= price <= MAX_PRICE else None


@dataclass(frozen=True)
class Listing:
    id: int
    adoption_id: int
    seller_id: int
    seller_clone_id: Optional[int]
    price: int
    guild_id: Optional[int]
    channel_id: Optional[int]
    message_id: Optional[int]
    status: str
    cat: Optional[ca.Cat] = None

    @classmethod
    def from_row(cls, r) -> "Listing":
        cat = None
        try:
            if r["name"] is not None:
                cat = ca.Cat(id=int(r["adoption_id"]), owner_id=int(r["seller_id"]), clone_id=r["seller_clone_id"],
                             pool_id=int(r["pool_id"]), name=r["name"], rarity=r["rarity"], trait=r["trait"],
                             traded_count=int(r["traded_count"] or 0), cat_rank=int(r.get("cat_rank") or 0))
        except (KeyError, TypeError):
            cat = None
        return cls(id=int(r["id"]), adoption_id=int(r["adoption_id"]), seller_id=int(r["seller_id"]),
                   seller_clone_id=r["seller_clone_id"], price=int(r["price"]), guild_id=r["guild_id"],
                   channel_id=r["channel_id"], message_id=r["message_id"], status=r["status"], cat=cat)


@dataclass(frozen=True)
class BuyResult:
    status: str                         # ok | gone | own | poor | limit
    listing: Optional[Listing] = None


_JOIN = ("SELECT l.*, a.name, a.rarity, a.trait, a.pool_id, a.traded_count, a.cat_rank FROM cat_listings l "
         "JOIN cat_adoptions a ON a.id = l.adoption_id ")


async def _pool():
    from database import get_pool
    return await get_pool()


def _key(clone_id) -> int:
    return -1 if clone_id is None else int(clone_id)


async def create_listing(cat_id: int, seller_id: int, seller_clone_id, price: int,
                         guild_id: Optional[int], channel_id: Optional[int]) -> tuple:
    """Returns (status, Listing|None): ok | not_yours | listed | too_many | bad_price."""
    if not MIN_PRICE <= int(price) <= MAX_PRICE:
        return "bad_price", None
    pool = await _pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            owner = await conn.fetchrow("SELECT owner_id, clone_id FROM cat_adoptions WHERE id = $1 FOR UPDATE", cat_id)
            if owner is None or int(owner["owner_id"]) != seller_id or _key(owner["clone_id"]) != _key(seller_clone_id):
                return "not_yours", None
            if await conn.fetchval("SELECT 1 FROM cat_listings WHERE adoption_id = $1 AND status = 'open'", cat_id):
                return "listed", None
            open_n = await conn.fetchval("SELECT COUNT(*) FROM cat_listings WHERE seller_id = $1 AND status = 'open'",
                                         seller_id)
            if int(open_n or 0) >= MAX_OPEN_PER_SELLER:
                return "too_many", None
            lid = await conn.fetchval(
                "INSERT INTO cat_listings (adoption_id, seller_id, seller_clone_id, price, guild_id, channel_id) "
                "VALUES ($1,$2,$3,$4,$5,$6) RETURNING id", cat_id, seller_id, seller_clone_id, int(price), guild_id,
                channel_id)
    return "ok", await get(int(lid))


async def set_message(listing_id: int, channel_id: int, message_id: int) -> None:
    pool = await _pool()
    async with pool.acquire() as conn:
        await conn.execute("UPDATE cat_listings SET channel_id = $2, message_id = $3 WHERE id = $1",
                           listing_id, channel_id, message_id)


async def get(listing_id: int) -> Optional[Listing]:
    pool = await _pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(_JOIN + "WHERE l.id = $1", listing_id)
    return Listing.from_row(row) if row else None


async def cancel(listing_id: int, seller_id: int) -> bool:
    pool = await _pool()
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "UPDATE cat_listings SET status = 'cancelled', closed_at = NOW() "
            "WHERE id = $1 AND seller_id = $2 AND status = 'open' RETURNING id", listing_id, seller_id) is not None


async def list_open(page: int = 0, size: int = PAGE_SIZE) -> tuple:
    """(listings, total_open) for one page, cheapest first."""
    pool = await _pool()
    async with pool.acquire() as conn:
        total = await conn.fetchval("SELECT COUNT(*) FROM cat_listings WHERE status = 'open'")
        rows = await conn.fetch(_JOIN + "WHERE l.status = 'open' ORDER BY l.price, l.id LIMIT $1 OFFSET $2",
                                size, max(0, page) * size)
    return [Listing.from_row(r) for r in rows], int(total or 0)


async def buy(listing_id: int, buyer_id: int, buyer_clone_id) -> BuyResult:
    """All or nothing: the coins leave the buyer, reach the seller, and the cat changes hands, or none of it happens."""
    pool = await _pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow("SELECT * FROM cat_listings WHERE id = $1 FOR UPDATE", listing_id)
            if row is None or row["status"] != "open":
                return BuyResult("gone")
            seller_id, seller_clone, price = int(row["seller_id"]), row["seller_clone_id"], int(row["price"])
            if seller_id == buyer_id:
                return BuyResult("own")
            cat = await conn.fetchrow("SELECT owner_id, clone_id FROM cat_adoptions WHERE id = $1 FOR UPDATE",
                                      row["adoption_id"])
            if cat is None or int(cat["owner_id"]) != seller_id or _key(cat["clone_id"]) != _key(seller_clone):
                await conn.execute("UPDATE cat_listings SET status = 'void', closed_at = NOW() WHERE id = $1", listing_id)
                return BuyResult("gone")
            held = await conn.fetchval(
                "SELECT COUNT(*) FROM cat_adoptions WHERE owner_id = $1 AND COALESCE(clone_id, -1) = $2",
                buyer_id, _key(buyer_clone_id))
            if int(held or 0) >= ca.MAX_CATS:
                return BuyResult("limit")
            paid = await conn.fetchval(
                "UPDATE cat_wallets SET coins = coins - $3, updated_at = NOW() "
                "WHERE owner_id = $1 AND COALESCE(clone_id, -1) = $2 AND coins >= $3 RETURNING coins",
                buyer_id, _key(buyer_clone_id), price)
            if paid is None:
                return BuyResult("poor")
            await conn.execute(
                "INSERT INTO cat_wallets (owner_id, clone_id, coins, diamonds) VALUES ($1, $2, $3, 0) "
                "ON CONFLICT (owner_id, (COALESCE(clone_id, -1))) DO UPDATE SET "
                "coins = cat_wallets.coins + EXCLUDED.coins, updated_at = NOW()", seller_id, seller_clone, price)
            await conn.execute("UPDATE cat_adoptions SET owner_id = $2, clone_id = $3, traded_count = traded_count + 1 "
                               "WHERE id = $1", row["adoption_id"], buyer_id, buyer_clone_id)
            await conn.execute("UPDATE cat_listings SET status = 'sold', buyer_id = $2, buyer_clone_id = $3, "
                               "closed_at = NOW() WHERE id = $1", listing_id, buyer_id, buyer_clone_id)
    return BuyResult("ok", await get(listing_id))
