# path: modules/market.py

"""
General Marketplace (R1). Play coins only, never real money. ONE listings table for every kind of item. A kind of item is
a small ADAPTER (`ItemAdapter`): it says whether the seller may list it, whether the buyer can receive it, and how it
changes hands. A new item type (house, cosmetic, ...) is one new adapter, not a new market.

Every sale is ONE database transaction: listing row, the item, then both wallets (by owner id) are locked; the buyer
pays, the seller gets the price minus the fee, the fee leaves the game, the item moves, the listing closes. Any failure
rolls all of it back.

Phase R1a: data layer and the cat adapter. The old cat market (modules/cat_market.py) keeps running until the screens
are switched over in R1b, which also migrates open cat listings and points the cat lock checks at this table.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple

MIN_PRICE = 10
MAX_PRICE = 50_000_000
MAX_OPEN_PER_SELLER = 5
FEE_PERCENT = 5                     # share of the price that leaves the game on every sale (0 turns it off)
EXPIRY_DAYS = 14
PAGE_SIZE = 10
WALLET = -1                         # the coin wallet is shared by every bot


def fee_for(price: int) -> int:
    return int(price) * FEE_PERCENT // 100


def seller_gets(price: int) -> int:
    return int(price) - fee_for(price)


def _key(clone_id) -> int:
    return -1 if clone_id is None else int(clone_id)


async def _pool():
    from database import get_pool
    return await get_pool()


# ── adapters ────────────────────────────────────────────────────────────

class ItemAdapter:
    """One kind of tradable item. Methods run INSIDE the caller's transaction on its connection."""
    item_type = ""

    async def check_listable(self, conn, ref: int, seller_id: int, seller_clone_id, price: int) -> Optional[str]:
        """Lock the item and return None when the seller may list it at this price, else a status code
        (not_yours | away | listed | out_of_range)."""
        raise NotImplementedError

    async def check_owned(self, conn, ref: int, seller_id: int, seller_clone_id) -> bool:
        """Lock the item; True while the seller still owns it and it can still be sold."""
        raise NotImplementedError

    async def can_receive(self, conn, buyer_id: int, buyer_clone_id) -> bool:
        """False when the buyer is at a limit for this kind of item."""
        return True

    async def transfer(self, conn, ref: int, buyer_id: int, buyer_clone_id) -> None:
        raise NotImplementedError

    async def describe(self, conn, refs: Iterable[int]) -> Dict[int, str]:
        """Short display names for listing rows."""
        return {int(r): f"{self.item_type} #{int(r)}" for r in refs}


class CatAdapter(ItemAdapter):
    item_type = "cat"

    async def check_listable(self, conn, ref, seller_id, seller_clone_id, price):
        from modules import cat_market as cm
        owner = await conn.fetchrow("SELECT owner_id, clone_id, rarity, cat_rank FROM cat_adoptions WHERE id = $1 FOR UPDATE", ref)
        if owner is None or int(owner["owner_id"]) != seller_id or _key(owner["clone_id"]) != _key(seller_clone_id):
            return "not_yours"
        if not cm.price_ok(owner["rarity"], owner["cat_rank"], int(price)):
            return "out_of_range"                       # checked against the rank the cat has right now
        if await conn.fetchval("SELECT 1 FROM cat_listings WHERE adoption_id = $1 AND status = 'open'", ref):
            return "listed"                             # still on the old cat market
        if await conn.fetchval("SELECT 1 FROM cat_outings WHERE adoption_id = $1 AND claimed_at IS NULL", ref):
            return "away"                               # out on a walk or a heist
        if await conn.fetchval("SELECT 1 FROM cat_fight_requests WHERE cat_id = $1 AND status IN ('open','accepted','locked') "
                               "AND (status <> 'open' OR expires_at > NOW())", ref):
            return "away"                               # held by a fight request
        return None

    async def check_owned(self, conn, ref, seller_id, seller_clone_id):
        cat = await conn.fetchrow("SELECT owner_id, clone_id FROM cat_adoptions WHERE id = $1 FOR UPDATE", ref)
        if cat is None or int(cat["owner_id"]) != seller_id or _key(cat["clone_id"]) != _key(seller_clone_id):
            return False
        if await conn.fetchval("SELECT 1 FROM cat_outings WHERE adoption_id = $1 AND claimed_at IS NULL", ref):
            return False
        if await conn.fetchval("SELECT 1 FROM cat_fight_requests WHERE cat_id = $1 AND status IN ('open','accepted','locked') "
                               "AND (status <> 'open' OR expires_at > NOW())", ref):
            return False
        return True

    async def can_receive(self, conn, buyer_id, buyer_clone_id):
        from modules import cat_adopt as ca
        held = await conn.fetchval("SELECT COUNT(*) FROM cat_adoptions WHERE owner_id = $1 AND COALESCE(clone_id, -1) = $2",
                                   buyer_id, _key(buyer_clone_id))
        return int(held or 0) < ca.MAX_CATS

    async def transfer(self, conn, ref, buyer_id, buyer_clone_id):
        await conn.execute("UPDATE cat_adoptions SET owner_id = $2, clone_id = $3, traded_count = traded_count + 1 WHERE id = $1",
                           ref, buyer_id, buyer_clone_id)

    async def describe(self, conn, refs):
        ids = [int(r) for r in refs]
        out = {i: f"Cat #{i}" for i in ids}
        if ids:
            for r in await conn.fetch("SELECT id, name, rarity FROM cat_adoptions WHERE id = ANY($1::bigint[])", ids):
                out[int(r["id"])] = f"{r['name']} ({r['rarity']})"
        return out


ADAPTERS: Dict[str, ItemAdapter] = {}


def register(adapter: ItemAdapter) -> None:
    ADAPTERS[adapter.item_type] = adapter


register(CatAdapter())


# ── data ────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Listing:
    id: int
    item_type: str
    item_ref: int
    seller_id: int
    seller_clone_id: Optional[int]
    price: int
    fee: int
    guild_id: Optional[int]
    channel_id: Optional[int]
    message_id: Optional[int]
    status: str

    @classmethod
    def from_row(cls, r) -> "Listing":
        return cls(int(r["id"]), r["item_type"], int(r["item_ref"]), int(r["seller_id"]), r["seller_clone_id"], int(r["price"]),
                   int(r["fee"] or 0), r["guild_id"], r["channel_id"], r["message_id"], r["status"])


@dataclass(frozen=True)
class BuyResult:
    status: str                         # ok | gone | own | poor | limit
    listing: Optional[Listing] = None
    paid: int = 0
    fee: int = 0


async def get(listing_id: int) -> Optional[Listing]:
    p = await _pool()
    async with p.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM market_listings WHERE id = $1", int(listing_id))
    return Listing.from_row(row) if row else None


async def create_listing(item_type: str, item_ref: int, seller_id: int, seller_clone_id, price: int,
                         guild_id: Optional[int] = None, channel_id: Optional[int] = None) -> Tuple[str, Optional[Listing]]:
    """Returns (status, Listing|None): ok | unknown_type | bad_price | not_yours | listed | away | out_of_range | too_many."""
    ad = ADAPTERS.get(item_type)
    if ad is None:
        return "unknown_type", None
    if not MIN_PRICE <= int(price) <= MAX_PRICE:
        return "bad_price", None
    p = await _pool()
    async with p.acquire() as conn:
        async with conn.transaction():
            bad = await ad.check_listable(conn, int(item_ref), int(seller_id), seller_clone_id, int(price))
            if bad:
                return bad, None
            if await conn.fetchval("SELECT 1 FROM market_listings WHERE item_type = $1 AND item_ref = $2 AND status = 'open'",
                                   item_type, int(item_ref)):
                return "listed", None
            n = await conn.fetchval("SELECT COUNT(*) FROM market_listings WHERE seller_id = $1 AND status = 'open'", int(seller_id))
            if int(n or 0) >= MAX_OPEN_PER_SELLER:
                return "too_many", None
            lid = await conn.fetchval(
                "INSERT INTO market_listings (item_type, item_ref, seller_id, seller_clone_id, price, guild_id, channel_id, expires_at) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, NOW() + $8 * INTERVAL '1 day') RETURNING id",
                item_type, int(item_ref), int(seller_id), seller_clone_id, int(price), guild_id, channel_id, EXPIRY_DAYS)
    return "ok", await get(int(lid))


async def set_message(listing_id: int, channel_id: int, message_id: int) -> None:
    p = await _pool()
    async with p.acquire() as conn:
        await conn.execute("UPDATE market_listings SET channel_id = $2, message_id = $3 WHERE id = $1",
                           int(listing_id), channel_id, message_id)


async def cancel(listing_id: int, seller_id: int) -> bool:
    p = await _pool()
    async with p.acquire() as conn:
        return await conn.fetchval(
            "UPDATE market_listings SET status = 'cancelled', closed_at = NOW() "
            "WHERE id = $1 AND seller_id = $2 AND status = 'open' RETURNING id", int(listing_id), int(seller_id)) is not None


async def list_open(item_type: Optional[str] = None, page: int = 0, size: int = PAGE_SIZE) -> Tuple[List[Listing], int]:
    """(listings, total_open) for one page, cheapest first. Listings past their expiry are not shown."""
    p = await _pool()
    async with p.acquire() as conn:
        total = await conn.fetchval("SELECT COUNT(*) FROM market_listings WHERE status = 'open' AND expires_at > NOW() "
                                    "AND ($1::text IS NULL OR item_type = $1)", item_type)
        rows = await conn.fetch("SELECT * FROM market_listings WHERE status = 'open' AND expires_at > NOW() "
                                "AND ($1::text IS NULL OR item_type = $1) ORDER BY price, id LIMIT $2 OFFSET $3",
                                item_type, size, max(0, page) * size)
    return [Listing.from_row(r) for r in rows], int(total or 0)


async def my_open(seller_id: int) -> List[Listing]:
    p = await _pool()
    async with p.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM market_listings WHERE seller_id = $1 AND status = 'open' ORDER BY id", int(seller_id))
    return [Listing.from_row(r) for r in rows]


async def describe(listings: Iterable[Listing]) -> Dict[int, str]:
    """listing id -> display name, one query per item type."""
    by_type: Dict[str, List[Listing]] = {}
    for l in listings:
        by_type.setdefault(l.item_type, []).append(l)
    out: Dict[int, str] = {}
    p = await _pool()
    async with p.acquire() as conn:
        for t, ls in by_type.items():
            ad = ADAPTERS.get(t)
            names = await ad.describe(conn, [l.item_ref for l in ls]) if ad else {}
            for l in ls:
                out[l.id] = names.get(l.item_ref, f"{t} #{l.item_ref}")
    return out


async def expire_due() -> int:
    """Close open listings past their expiry. Safe to run from a sweeper. Returns how many closed."""
    p = await _pool()
    async with p.acquire() as conn:
        rows = await conn.fetch("UPDATE market_listings SET status = 'expired', closed_at = NOW() "
                                "WHERE status = 'open' AND expires_at <= NOW() RETURNING id")
    return len(rows)


async def buy(listing_id: int, buyer_id: int, buyer_clone_id) -> BuyResult:
    """All or nothing. Lock order: listing row, the item, then both wallets by owner id."""
    p = await _pool()
    async with p.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow("SELECT *, (expires_at <= NOW()) AS is_expired FROM market_listings WHERE id = $1 FOR UPDATE", int(listing_id))
            if row is None or row["status"] != "open":
                return BuyResult("gone")
            l = Listing.from_row(row)
            if l.seller_id == int(buyer_id):
                return BuyResult("own")
            ad = ADAPTERS.get(l.item_type)
            if ad is None:
                return BuyResult("gone")
            if row["is_expired"]:
                await conn.execute("UPDATE market_listings SET status = 'expired', closed_at = NOW() WHERE id = $1", l.id)
                return BuyResult("gone")
            if not await ad.check_owned(conn, l.item_ref, l.seller_id, l.seller_clone_id):
                await conn.execute("UPDATE market_listings SET status = 'void', closed_at = NOW() WHERE id = $1", l.id)
                return BuyResult("gone")
            if not await ad.can_receive(conn, int(buyer_id), buyer_clone_id):
                return BuyResult("limit")
            await conn.fetch("SELECT owner_id FROM cat_wallets WHERE owner_id = ANY($1::bigint[]) AND COALESCE(clone_id, -1) = $2 "
                             "ORDER BY owner_id FOR UPDATE", [int(buyer_id), l.seller_id], WALLET)
            paid = await conn.fetchval("UPDATE cat_wallets SET coins = coins - $3, updated_at = NOW() "
                                       "WHERE owner_id = $1 AND COALESCE(clone_id, -1) = $2 AND coins >= $3 RETURNING coins",
                                       int(buyer_id), WALLET, l.price)
            if paid is None:
                return BuyResult("poor")
            fee = fee_for(l.price)
            await conn.execute("INSERT INTO cat_wallets (owner_id, clone_id, coins, diamonds) VALUES ($1, $2, $3, 0) "
                               "ON CONFLICT (owner_id, (COALESCE(clone_id, -1))) DO UPDATE SET "
                               "coins = cat_wallets.coins + EXCLUDED.coins, updated_at = NOW()", l.seller_id, None, l.price - fee)
            await ad.transfer(conn, l.item_ref, int(buyer_id), buyer_clone_id)
            await conn.execute("UPDATE market_listings SET status = 'sold', buyer_id = $2, buyer_clone_id = $3, fee = $4, "
                               "closed_at = NOW() WHERE id = $1", l.id, int(buyer_id), buyer_clone_id, fee)
    return BuyResult("ok", await get(l.id), l.price, fee)
