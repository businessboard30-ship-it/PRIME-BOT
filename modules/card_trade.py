# path: modules/card_trade.py

"""
Card trading (global + local) and coin transfers.

Flow: the initiator proposes (their confirmation is implied) -> offered cards are locked -> the recipient accepts
(second confirmation) -> ONE transaction re-checks everything and swaps cards + coins. Either party can cancel while
pending; offers expire after TRADE_TTL. Coins are checked and moved only at accept time (no escrow), so a failed
accept changes nothing. Boost level and serial travel with the copy.

Local trades: the CALLER must verify guild membership server-side and pass recipient_in_guild (this module has no
Discord access). Global trades need no membership.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional, Sequence

from modules import card_economy as _ce
from modules.card_economy import StoreError, _clone_key

TRADE_TTL = timedelta(minutes=30)
MAX_TRANSFER = 1_000_000
DAILY_TRANSFER_CAP = 5_000_000


async def _lock_balances(conn, clone_id, *user_ids):
    """Lock card_coins rows in a fixed order so opposite-direction trades can't deadlock."""
    for uid in sorted(set(user_ids)):
        await conn.execute("INSERT INTO card_coins (user_id, clone_id, balance) VALUES ($1,$2,0) ON CONFLICT DO NOTHING",
                           uid, clone_id)
        await conn.fetchrow("SELECT balance FROM card_coins WHERE user_id = $1 AND COALESCE(clone_id,-1) = $2 "
                            "FOR UPDATE", uid, _clone_key(clone_id))


async def _move_coins(conn, clone_id, src, dst, amount, reason, ref):
    if amount:
        await _ce._debit(conn, src, clone_id, amount, reason, ref)
        await _ce._credit(conn, dst, clone_id, amount, reason, ref)


async def propose_trade(initiator_id: int, recipient_id: int, clone_id: Optional[int], scope: str,
                        give: Sequence[int], get: Sequence[int], give_coins: int = 0, get_coins: int = 0,
                        guild_id: Optional[int] = None, recipient_in_guild: bool = False) -> int:
    if scope not in ("global", "local"):
        raise StoreError("bad_scope", "Trade must be global or local.")
    if initiator_id == recipient_id:
        raise StoreError("self", "You can't trade with yourself.")
    if scope == "local" and (guild_id is None or not recipient_in_guild):
        raise StoreError("not_in_server", "That user isn't a member of this server.")
    if give_coins < 0 or get_coins < 0 or (give_coins and get_coins):
        raise StoreError("bad_coins", "Offer coins one way only, and not negative.")
    give, get = list(dict.fromkeys(give)), list(dict.fromkeys(get))
    if not (give or give_coins) or not (get or get_coins):
        raise StoreError("empty", "Both sides of a trade must offer something.")
    pool = await _ce._pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            ids = sorted(give + get)
            rows = await conn.fetch(
                "SELECT cp.*, cc.tradeable FROM card_copies cp JOIN card_catalog cc USING (card_id) "
                "WHERE cp.copy_id = ANY($1::bigint[]) ORDER BY cp.copy_id FOR UPDATE OF cp", ids)
            by = {r["copy_id"]: r for r in rows}
            for cid, owner in [(c, initiator_id) for c in give] + [(c, recipient_id) for c in get]:
                r = by.get(cid)
                if r is None or r["owner_id"] != owner:
                    raise StoreError("not_owner", "A card in this trade isn't owned by the expected user.")
                if r["rental_expires_at"] is not None:
                    raise StoreError("rented", "Rented cards can't be traded.")
                if not r["tradeable"]:
                    raise StoreError("not_tradeable", "A card in this trade can't be traded.")
                if r["trade_locked"]:
                    raise StoreError("locked", "A card is already in another pending trade.")
            t = await conn.fetchrow(
                "INSERT INTO card_trades (clone_id, scope, guild_id, initiator_id, recipient_id, give_coins, get_coins,"
                " expires_at) VALUES ($1,$2,$3,$4,$5,$6,$7,$8) RETURNING trade_id",
                clone_id, scope, guild_id if scope == "local" else None, initiator_id, recipient_id,
                give_coins, get_coins, datetime.now(timezone.utc) + TRADE_TTL)
            for cid in give:
                await conn.execute("INSERT INTO card_trade_items VALUES ($1,$2,'initiator')", t["trade_id"], cid)
            for cid in get:
                await conn.execute("INSERT INTO card_trade_items VALUES ($1,$2,'recipient')", t["trade_id"], cid)
            await conn.execute("UPDATE card_copies SET trade_locked = TRUE WHERE copy_id = ANY($1::bigint[])", ids)
            return t["trade_id"]


async def _release(conn, trade_id):
    await conn.execute("UPDATE card_copies SET trade_locked = FALSE WHERE copy_id IN "
                       "(SELECT copy_id FROM card_trade_items WHERE trade_id = $1)", trade_id)


async def accept_trade(trade_id: int, user_id: int, recipient_in_guild: bool = True) -> dict:
    """Recipient's confirmation; performs the whole exchange atomically."""
    pool = await _ce._pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            t = await conn.fetchrow("SELECT * FROM card_trades WHERE trade_id = $1 FOR UPDATE", trade_id)
            if t is None or t["status"] != "pending":
                raise StoreError("not_pending", "This trade is no longer open.")
            if user_id != t["recipient_id"]:
                raise StoreError("not_recipient", "Only the recipient can accept.")
            if t["expires_at"] <= datetime.now(timezone.utc):
                raise StoreError("expired", "This trade offer has expired.")
            if t["scope"] == "local" and not recipient_in_guild:
                raise StoreError("not_in_server", "You're no longer a member of the server for this trade.")
            items = await conn.fetch("SELECT i.copy_id, i.side FROM card_trade_items i WHERE trade_id = $1 "
                                     "ORDER BY copy_id", trade_id)
            copies = {r["copy_id"]: r for r in await conn.fetch(
                "SELECT * FROM card_copies WHERE copy_id = ANY($1::bigint[]) ORDER BY copy_id FOR UPDATE",
                [i["copy_id"] for i in items])}
            for i in items:
                want = t["initiator_id"] if i["side"] == "initiator" else t["recipient_id"]
                c = copies.get(i["copy_id"])
                if c is None or c["owner_id"] != want or c["rental_expires_at"] is not None:
                    raise StoreError("changed", "A card in this trade is no longer available.")
            await _lock_balances(conn, t["clone_id"], t["initiator_id"], t["recipient_id"])
            ref = f"trade:{trade_id}"
            await _move_coins(conn, t["clone_id"], t["initiator_id"], t["recipient_id"], t["give_coins"], "trade", ref)
            await _move_coins(conn, t["clone_id"], t["recipient_id"], t["initiator_id"], t["get_coins"], "trade", ref)
            for i in items:
                new_owner = t["recipient_id"] if i["side"] == "initiator" else t["initiator_id"]
                await conn.execute("UPDATE card_copies SET owner_id = $2, trade_locked = FALSE WHERE copy_id = $1",
                                   i["copy_id"], new_owner)
            await conn.execute("UPDATE card_trades SET status='completed', resolved_at=NOW() WHERE trade_id=$1", trade_id)
            return {"trade_id": trade_id, "scope": t["scope"], "cards": len(items)}


async def cancel_trade(trade_id: int, user_id: int) -> None:
    pool = await _ce._pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            t = await conn.fetchrow("SELECT * FROM card_trades WHERE trade_id = $1 FOR UPDATE", trade_id)
            if t is None or t["status"] != "pending":
                raise StoreError("not_pending", "This trade is no longer open.")
            if user_id not in (t["initiator_id"], t["recipient_id"]):
                raise StoreError("not_party", "You're not part of this trade.")
            await _release(conn, trade_id)
            await conn.execute("UPDATE card_trades SET status='cancelled', resolved_at=NOW() WHERE trade_id=$1", trade_id)


async def expire_trades(now: Optional[datetime] = None) -> int:
    now = now or datetime.now(timezone.utc)
    pool = await _ce._pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            rows = await conn.fetch("SELECT trade_id FROM card_trades WHERE status='pending' AND expires_at <= $1 "
                                    "ORDER BY trade_id FOR UPDATE SKIP LOCKED", now)
            for r in rows:
                await _release(conn, r["trade_id"])
                await conn.execute("UPDATE card_trades SET status='expired', resolved_at=NOW() WHERE trade_id=$1",
                                   r["trade_id"])
            return len(rows)


async def transfer_coins(from_user: int, to_user: int, clone_id: Optional[int], amount: int) -> int:
    """Returns the sender's new balance. Validates amount, self-transfer, balance and a rolling 24h cap."""
    if from_user == to_user:
        raise StoreError("self", "You can't send coins to yourself.")
    if amount <= 0 or amount > MAX_TRANSFER:
        raise StoreError("bad_amount", f"Amount must be between 1 and {MAX_TRANSFER:,}.")
    pool = await _ce._pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await _lock_balances(conn, clone_id, from_user, to_user)
            sent = await conn.fetchval(
                "SELECT COALESCE(SUM(amount),0) FROM card_coin_transfers WHERE from_user=$1 "
                "AND COALESCE(clone_id,-1)=$2 AND created_at > NOW() - interval '24 hours'",
                from_user, _clone_key(clone_id))
            if sent + amount > DAILY_TRANSFER_CAP:
                raise StoreError("daily_cap", "Daily transfer limit reached.")
            bal = await _ce._debit(conn, from_user, clone_id, amount, "transfer_out", f"to:{to_user}")
            await _ce._credit(conn, to_user, clone_id, amount, "transfer_in", f"from:{from_user}")
            await conn.execute("INSERT INTO card_coin_transfers (clone_id, from_user, to_user, amount) "
                               "VALUES ($1,$2,$3,$4)", clone_id, from_user, to_user, amount)
            return bal
