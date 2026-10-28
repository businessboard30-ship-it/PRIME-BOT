# path: modules/card_economy.py

"""
Card economy core: global Store purchases/rentals, finite global stock, per-copy ownership, coin ledger.

Everything that moves coins, stock or ownership happens inside ONE database transaction with row locks, so two buyers
racing for the last copy cannot both succeed and a failed purchase never consumes coins or stock. See
database/migrations/088_card_economy_core.sql for the tables.

Rules (kept here so the UI never has to re-implement them):
  * Stock is global (card_catalog.stock_remaining), shared by every server. NULL stock_total = unlimited.
  * Limited-stock cards are permanent purchases only (a rental would have to hand the copy back and re-open stock);
    create_offer() refuses a rental offer for a limited card.
  * A limited copy gets serial_no = number sold so far + 1 ("#07 of 100"). Replenishing raises stock_total and
    stock_remaining together, so serials keep counting up and are never reused.
  * Coins are the existing global card_coins balance (per clone); every movement is written to card_coin_ledger.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger(__name__)


class StoreError(Exception):
    """A purchase/stock rule was not met. .code is stable for the UI; str() is user-safe text."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


async def _pool():
    from database import get_pool
    return await get_pool()


def _clone_key(clone_id) -> int:
    return -1 if clone_id is None else int(clone_id)


async def _debit(conn, user_id: int, clone_id, amount: int, reason: str, ref: Optional[str]) -> int:
    """Deduct coins inside the caller's transaction. Raises StoreError('funds') when the balance can't cover it."""
    if amount < 0:
        raise ValueError("amount must be >= 0")
    await conn.execute(
        "INSERT INTO card_coins (user_id, clone_id, balance) VALUES ($1, $2, 0) ON CONFLICT DO NOTHING",
        user_id, clone_id)
    row = await conn.fetchrow(
        "SELECT balance FROM card_coins WHERE user_id = $1 AND COALESCE(clone_id, -1) = $2 FOR UPDATE",
        user_id, _clone_key(clone_id))
    if row is None or row["balance"] < amount:
        raise StoreError("funds", "Not enough coins.")
    new_balance = row["balance"] - amount
    await conn.execute(
        "UPDATE card_coins SET balance = $3 WHERE user_id = $1 AND COALESCE(clone_id, -1) = $2",
        user_id, _clone_key(clone_id), new_balance)
    await conn.execute(
        "INSERT INTO card_coin_ledger (user_id, clone_id, delta, balance, reason, ref) VALUES ($1,$2,$3,$4,$5,$6)",
        user_id, clone_id, -amount, new_balance, reason, ref)
    return new_balance


async def _credit(conn, user_id: int, clone_id, amount: int, reason: str, ref: Optional[str]) -> int:
    if amount < 0:
        raise ValueError("amount must be >= 0")
    await conn.execute(
        "INSERT INTO card_coins (user_id, clone_id, balance) VALUES ($1, $2, 0) ON CONFLICT DO NOTHING",
        user_id, clone_id)
    row = await conn.fetchrow(
        "UPDATE card_coins SET balance = balance + $3 WHERE user_id = $1 AND COALESCE(clone_id, -1) = $2 "
        "RETURNING balance", user_id, _clone_key(clone_id), amount)
    await conn.execute(
        "INSERT INTO card_coin_ledger (user_id, clone_id, delta, balance, reason, ref) VALUES ($1,$2,$3,$4,$5,$6)",
        user_id, clone_id, amount, row["balance"], reason, ref)
    return row["balance"]


async def create_offer(card_id: int, kind: str, price: int, rental_days: Optional[int] = None) -> int:
    """Add (or re-price) a store offer. Returns offer_id."""
    if kind not in ("permanent", "rental"):
        raise StoreError("bad_kind", "Offer kind must be permanent or rental.")
    if price < 0:
        raise StoreError("bad_price", "Price can't be negative.")
    if (kind == "rental") != (rental_days is not None):
        raise StoreError("bad_kind", "Rental offers need rental_days; permanent offers must not have it.")
    pool = await _pool()
    async with pool.acquire() as conn:
        card = await conn.fetchrow("SELECT card_id, stock_total FROM card_catalog WHERE card_id = $1", card_id)
        if card is None:
            raise StoreError("no_card", "Unknown card.")
        if kind == "rental" and card["stock_total"] is not None:
            raise StoreError("limited_rental", "Limited-stock cards can't be rented.")
        row = await conn.fetchrow(
            "INSERT INTO card_offers (card_id, kind, rental_days, price) VALUES ($1,$2,$3,$4) "
            "ON CONFLICT (card_id, kind, COALESCE(rental_days, 0)) DO UPDATE SET price = EXCLUDED.price, active = TRUE "
            "RETURNING offer_id", card_id, kind, rental_days, price)
        return row["offer_id"]


async def set_limited_stock(card_id: int, total: Optional[int]) -> None:
    """Make a card limited (total copies) or unlimited (None). Only allowed before any copy exists for it."""
    pool = await _pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            card = await conn.fetchrow("SELECT card_id FROM card_catalog WHERE card_id = $1 FOR UPDATE", card_id)
            if card is None:
                raise StoreError("no_card", "Unknown card.")
            sold = await conn.fetchval("SELECT COUNT(*) FROM card_copies WHERE card_id = $1", card_id)
            if sold:
                raise StoreError("has_copies", "Stock type can't change once copies exist; use replenish_stock.")
            await conn.execute(
                "UPDATE card_catalog SET stock_total = $2, stock_remaining = $2 WHERE card_id = $1", card_id, total)


async def replenish_stock(card_id: int, amount: int, actor_id: Optional[int] = None) -> int:
    """Explicitly add copies to a limited card. Returns the new remaining stock."""
    if amount <= 0:
        raise StoreError("bad_amount", "Replenish amount must be positive.")
    pool = await _pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT stock_total FROM card_catalog WHERE card_id = $1 FOR UPDATE", card_id)
            if row is None or row["stock_total"] is None:
                raise StoreError("unlimited", "That card has no limited stock to replenish.")
            new = await conn.fetchrow(
                "UPDATE card_catalog SET stock_total = stock_total + $2, stock_remaining = stock_remaining + $2 "
                "WHERE card_id = $1 RETURNING stock_remaining", card_id, amount)
            await conn.execute(
                "INSERT INTO card_stock_events (card_id, delta, reason, actor_id) VALUES ($1,$2,'replenish',$3)",
                card_id, amount, actor_id)
            return new["stock_remaining"]


async def buy_offer(user_id: int, clone_id: Optional[int], offer_id: int, user_level: int) -> dict:
    """Atomic purchase/rental. Raises StoreError on any failed rule; on failure nothing is consumed.

    user_level must be read server-side by the caller (never from a button/custom_id)."""
    pool = await _pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            offer = await conn.fetchrow("SELECT * FROM card_offers WHERE offer_id = $1", offer_id)
            if offer is None or not offer["active"]:
                raise StoreError("no_offer", "That offer is no longer available.")
            # Lock the definition row: serialises every buyer of this card, which is what makes the last copy safe.
            card = await conn.fetchrow(
                "SELECT * FROM card_catalog WHERE card_id = $1 FOR UPDATE", offer["card_id"])
            if card is None or not card["active"] or not card["store_enabled"]:
                raise StoreError("not_for_sale", "That card isn't in the store.")
            if user_level < card["min_level"]:
                raise StoreError("min_level", f"Requires level {card['min_level']}.")
            if card["max_level"] is not None and user_level > card["max_level"]:
                raise StoreError("max_level", f"Only available up to level {card['max_level']}.")

            serial = None
            if card["stock_total"] is not None:
                if card["stock_remaining"] <= 0:
                    raise StoreError("out_of_stock", "OUT OF STOCK")
                serial = card["stock_total"] - card["stock_remaining"] + 1
                await conn.execute(
                    "UPDATE card_catalog SET stock_remaining = stock_remaining - 1 WHERE card_id = $1",
                    card["card_id"])

            balance = await _debit(conn, user_id, clone_id, offer["price"], "store_purchase", f"offer:{offer_id}")

            expires = None
            if offer["kind"] == "rental":
                expires = datetime.now(timezone.utc) + timedelta(days=offer["rental_days"])
            copy = await conn.fetchrow(
                "INSERT INTO card_copies (card_id, clone_id, owner_id, serial_no, source, rental_expires_at) "
                "VALUES ($1,$2,$3,$4,'store',$5) RETURNING copy_id",
                card["card_id"], clone_id, user_id, serial, expires)
            await conn.execute(
                "INSERT INTO card_purchases (user_id, clone_id, card_id, offer_id, copy_id, kind, price) "
                "VALUES ($1,$2,$3,$4,$5,$6,$7)",
                user_id, clone_id, card["card_id"], offer_id, copy["copy_id"], offer["kind"], offer["price"])
            return {"copy_id": copy["copy_id"], "card_id": card["card_id"], "serial_no": serial,
                    "stock_total": card["stock_total"], "kind": offer["kind"], "price": offer["price"],
                    "rental_expires_at": expires, "balance": balance}


async def expire_rentals(now: Optional[datetime] = None) -> int:
    """Remove rental copies past expiry. Returns how many were removed. Safe to run repeatedly."""
    now = now or datetime.now(timezone.utc)
    pool = await _pool()
    async with pool.acquire() as conn:
        res = await conn.execute(
            "DELETE FROM card_copies WHERE rental_expires_at IS NOT NULL AND rental_expires_at <= $1 "
            "AND NOT trade_locked", now)
        return int(res.split()[-1])


async def list_store(clone_id: Optional[int], limit: int = 10, offset: int = 0) -> list[dict]:
    """One page of store cards with their offers (paginated; never loads the whole catalog)."""
    pool = await _pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT c.*, COALESCE(json_agg(json_build_object('offer_id', o.offer_id, 'kind', o.kind, "
            "'rental_days', o.rental_days, 'price', o.price)) FILTER (WHERE o.offer_id IS NOT NULL), '[]') AS offers "
            "FROM card_catalog c LEFT JOIN card_offers o ON o.card_id = c.card_id AND o.active "
            "WHERE c.active AND c.store_enabled AND COALESCE(c.clone_id, -1) = $1 "
            "GROUP BY c.card_id ORDER BY c.base_value DESC, c.card_id LIMIT $2 OFFSET $3",
            _clone_key(clone_id), limit, offset)
        return [dict(r) for r in rows]
