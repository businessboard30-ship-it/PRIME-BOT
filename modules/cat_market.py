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
  * The asking price must sit inside the range for the cat's rarity and rank (price_range): a higher rank and a rarer
    cat can ask for more, and nobody can list a top-rank cat for next to nothing or a common cat for a fortune.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import List, Optional

from modules import cat_adopt as ca
from modules import cat_rank as cr
from modules import market as _market

logger = logging.getLogger(__name__)

MIN_PRICE = 10
MAX_PRICE = 50_000_000                  # the top ranks of a legendary cat are worth tens of millions
MAX_OPEN_PER_SELLER = 5
FEE_PERCENT = _market.FEE_PERCENT       # one set of numbers, shown on the screens
EXPIRY_DAYS = _market.EXPIRY_DAYS
PAGE_SIZE = 10


# Allowed asking price = a range that grows with the cat's rarity and with the coins that went into its ranks.
RARITY_BASE_VALUE = {"common": 50, "uncommon": 150, "rare": 500, "legendary": 2_000}
LOW_SHARE_OF_INVESTED = 0.10        # the cheapest ask: 10% of what its ranks cost, plus a fifth of the base value
HIGH_SHARE_OF_INVESTED = 1.5        # the dearest ask: 1.5x what its ranks cost, plus 3x the base value


def price_range(rarity: str, rank) -> tuple:
    """(lowest, highest) asking price in coins for a cat of this rarity and rank (0-25)."""
    base = RARITY_BASE_VALUE.get(rarity, RARITY_BASE_VALUE["common"])
    r = cr.clamp(rank)
    invested = sum(cr.cost_to_reach(n, rarity) for n in range(1, r + 1))
    low = max(MIN_PRICE, int(round(base * 0.2 + invested * LOW_SHARE_OF_INVESTED)))
    high = min(MAX_PRICE, int(round(base * 3 + invested * HIGH_SHARE_OF_INVESTED)))
    return low, max(high, low * 2 if low * 2 <= MAX_PRICE else MAX_PRICE)


def price_ok(rarity: str, rank, price: int) -> bool:
    low, high = price_range(rarity, rank)
    return low <= int(price) <= high


def price_choices(rarity: str, rank) -> List[int]:
    """Five tidy preset prices across the allowed range: lowest, 25%, 50%, 75%, highest."""
    low, high = price_range(rarity, rank)
    out = []
    for share in (0.0, 0.25, 0.5, 0.75, 1.0):
        raw = low + (high - low) * share
        digits = len(str(int(raw)))
        step = 10 ** max(0, digits - 2)                       # two significant figures: 1,200 not 1,187
        p = max(low, min(high, int(round(raw / step) * step)))
        if p not in out:
            out.append(p)
    return out


def parse_price(raw) -> Optional[int]:
    """'1500', '1,500', '1.5k', '2m' -> coins, or None when it isn't a price in MIN_PRICE..MAX_PRICE."""
    text = str(raw or "").strip().lower().replace(",", "").replace(" ", "")
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([km]?)", text)
    if not m:
        return None
    value = float(m.group(1)) * {"": 1, "k": 1_000, "m": 1_000_000}[m.group(2)]
    price = int(round(value))
    return price if MIN_PRICE <= price <= MAX_PRICE else None


def _fee(r) -> int:
    try:
        return int(r["fee"] or 0)
    except (KeyError, TypeError):
        return 0


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
    fee: int = 0

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
                   channel_id=r["channel_id"], message_id=r["message_id"], status=r["status"], cat=cat,
                   fee=_fee(r))


@dataclass(frozen=True)
class BuyResult:
    status: str                         # ok | gone | own | poor | limit
    listing: Optional[Listing] = None
    fee: int = 0


_JOIN = ("SELECT l.id, l.item_ref AS adoption_id, l.seller_id, l.seller_clone_id, l.price, l.fee, l.guild_id, l.channel_id, "
         "l.message_id, l.status, a.name, a.rarity, a.trait, a.pool_id, a.traded_count, a.cat_rank FROM market_listings l "
         "JOIN cat_adoptions a ON a.id = l.item_ref AND l.item_type = 'cat' ")


async def _pool():
    from database import get_pool
    return await get_pool()


def _key(clone_id) -> int:
    return -1 if clone_id is None else int(clone_id)


# The database part now lives in modules/market.py (one table for every item type). These wrappers keep the cat screens
# and their persistent buttons working unchanged: listing ids are the same, a cat listing is a market_listings row.

async def create_listing(cat_id: int, seller_id: int, seller_clone_id, price: int,
                         guild_id: Optional[int], channel_id: Optional[int]) -> tuple:
    """Returns (status, Listing|None): ok | not_yours | listed | away | too_many | bad_price | out_of_range."""
    from modules import market
    status, l = await market.create_listing("cat", int(cat_id), int(seller_id), seller_clone_id, int(price), guild_id, channel_id)
    if status != "ok" or l is None:
        return status, None
    return "ok", await get(l.id)


async def set_message(listing_id: int, channel_id: int, message_id: int) -> None:
    from modules import market
    await market.set_message(listing_id, channel_id, message_id)


async def get(listing_id: int) -> Optional[Listing]:
    pool = await _pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(_JOIN + "WHERE l.id = $1", listing_id)
    return Listing.from_row(row) if row else None


async def cancel(listing_id: int, seller_id: int) -> bool:
    from modules import market
    return await market.cancel(listing_id, seller_id)


async def list_open(page: int = 0, size: int = PAGE_SIZE) -> tuple:
    """(listings, total_open) for one page, cheapest first. Listings past their expiry are not shown."""
    pool = await _pool()
    async with pool.acquire() as conn:
        total = await conn.fetchval("SELECT COUNT(*) FROM market_listings WHERE status = 'open' AND item_type = 'cat' "
                                    "AND expires_at > NOW()")
        rows = await conn.fetch(_JOIN + "WHERE l.status = 'open' AND l.expires_at > NOW() ORDER BY l.price, l.id LIMIT $1 OFFSET $2",
                                size, max(0, page) * size)
    return [Listing.from_row(r) for r in rows], int(total or 0)


async def buy(listing_id: int, buyer_id: int, buyer_clone_id) -> BuyResult:
    """All or nothing (see market.buy): coins leave the buyer, the seller gets the price minus the market fee, the cat
    changes hands, or none of it happens."""
    from modules import market
    res = await market.buy(int(listing_id), int(buyer_id), buyer_clone_id)
    if res.status != "ok":
        return BuyResult(res.status)
    return BuyResult("ok", await get(int(listing_id)), res.fee)
