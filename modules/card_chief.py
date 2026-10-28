# path: modules/card_chief.py

"""
Buy a Chief Seat with coins.

A server has 5 chief seats (discord_clan_chiefs). They are normally earned by XP rank. Here a member can instead LEASE one
for coins:

  * Eligibility: the buyer's level in THIS server must be at least CHIEF_MIN_LEVEL (read from discord_xp, never trusted
    from the UI).
  * A purchase can displace an XP-earned chief. It can NOT displace someone else's still-running lease.
  * A member holds one seat at a time. Buying the seat you already hold renews it, but the lease can never run past
    CHIEF_SEAT_MAX_LEASE_DAYS from now, so a seat can't be bought out for good.
  * While lease_until is in the future database.recompute_clan_chiefs leaves the seat alone; afterwards the seat returns
    to the normal top-5 rules.
  * Coins, the seat swap and the history row happen in ONE transaction. A per-buyer advisory lock stops one person buying
    two seats at once; the seat row lock stops two buyers taking the same seat. A failure consumes nothing.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import config as app_config
from modules import card_economy as ce
from modules.card_economy import StoreError, _debit

logger = logging.getLogger(__name__)


def seat_price(seat_rank: int) -> int:
    prices = app_config.CHIEF_SEAT_PRICES
    return int(prices[min(max(int(seat_rank), 1), len(prices)) - 1])


async def _ensure_seats(conn, guild_id: int, clone_id: Optional[int]) -> None:
    from modules.clan_cards import CLAN_CARDS
    for seat_rank, (_filename, clan_label, *_rest) in enumerate(CLAN_CARDS, start=1):
        await conn.execute(
            "INSERT INTO discord_clan_chiefs (guild_id, clone_id, seat_rank, clan_slug, user_id, since) "
            "VALUES ($1,$2,$3,$4,NULL,NULL) ON CONFLICT (guild_id, (COALESCE(clone_id, -1)), seat_rank) DO NOTHING",
            guild_id, clone_id, seat_rank, clan_label)


async def list_seats(guild_id: int, clone_id: Optional[int]) -> list[dict]:
    """The server's seats with price and whether each is leased right now. Creates the seats on first use."""
    pool = await ce._pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await _ensure_seats(conn, guild_id, clone_id)
            rows = await conn.fetch(
                "SELECT seat_rank, clan_slug, user_id, since, lease_until, "
                "(lease_until IS NOT NULL AND lease_until > NOW()) AS leased "
                "FROM discord_clan_chiefs WHERE guild_id = $1 AND clone_id IS NOT DISTINCT FROM $2 ORDER BY seat_rank",
                guild_id, clone_id)
    return [{**dict(r), "price": seat_price(r["seat_rank"])} for r in rows]


async def buy_seat(user_id: int, clone_id: Optional[int], guild_id: int, seat_rank: int) -> dict:
    """Atomic lease. Raises StoreError on any failed rule; on failure no coins or seats change."""
    if not app_config.CHIEF_SEAT_PURCHASE_ENABLED:
        raise StoreError("disabled", "Buying chief seats is switched off.")
    if not guild_id:
        raise StoreError("no_guild", "Open the Card Hub inside a server to buy a chief seat.")
    if not 1 <= int(seat_rank) <= len(app_config.CHIEF_SEAT_PRICES):
        raise StoreError("no_seat", "That seat doesn't exist.")
    pool = await ce._pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1))",
                               f"chief:{guild_id}:{-1 if clone_id is None else clone_id}:{user_id}")
            await _ensure_seats(conn, guild_id, clone_id)
            level = await conn.fetchval(
                "SELECT level FROM discord_xp WHERE guild_id = $1 AND clone_id IS NOT DISTINCT FROM $2 AND user_id = $3",
                guild_id, clone_id, user_id) or 0
            if level < app_config.CHIEF_MIN_LEVEL:
                raise StoreError("min_level", f"You need level {app_config.CHIEF_MIN_LEVEL} in this server to hold a chief seat.")
            seat = await conn.fetchrow(
                "SELECT *, (lease_until IS NOT NULL AND lease_until > NOW()) AS leased, NOW() AS db_now "
                "FROM discord_clan_chiefs WHERE guild_id = $1 AND clone_id IS NOT DISTINCT FROM $2 AND seat_rank = $3 "
                "FOR UPDATE", guild_id, clone_id, int(seat_rank))
            if seat is None:
                raise StoreError("no_seat", "That seat doesn't exist.")
            mine = await conn.fetchrow(
                "SELECT seat_rank FROM discord_clan_chiefs WHERE guild_id = $1 AND clone_id IS NOT DISTINCT FROM $2 "
                "AND user_id = $3", guild_id, clone_id, user_id)
            if mine is not None and mine["seat_rank"] != seat["seat_rank"]:
                raise StoreError("holds_other", f"You already hold seat {mine['seat_rank']}. One seat per person.")
            renewing = seat["user_id"] == user_id
            if seat["leased"] and not renewing:
                raise StoreError("leased", "Someone has this seat leased right now. Try again when their lease ends.")

            now = seat["db_now"]
            start = seat["lease_until"] if (renewing and seat["leased"]) else now
            lease_until = start + timedelta(days=app_config.CHIEF_SEAT_LEASE_DAYS)
            if lease_until > now + timedelta(days=app_config.CHIEF_SEAT_MAX_LEASE_DAYS):
                raise StoreError("max_lease", f"You can't lease more than {app_config.CHIEF_SEAT_MAX_LEASE_DAYS} days ahead.")

            price = seat_price(seat_rank)
            balance = await _debit(conn, user_id, clone_id, price, "chief_seat", f"seat:{guild_id}:{seat_rank}")
            displaced = None if renewing else seat["user_id"]
            await conn.execute(
                "UPDATE discord_clan_chiefs SET user_id = $4, since = $5, lease_until = $6 "
                "WHERE guild_id = $1 AND clone_id IS NOT DISTINCT FROM $2 AND seat_rank = $3",
                guild_id, clone_id, int(seat_rank), user_id, seat["since"] if renewing and seat["since"] else now, lease_until)
            await conn.execute(
                "INSERT INTO card_chief_purchases (guild_id, clone_id, seat_rank, buyer_id, displaced_id, price, lease_days, "
                "lease_until, kind) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)",
                guild_id, clone_id, int(seat_rank), user_id, displaced, price, app_config.CHIEF_SEAT_LEASE_DAYS,
                lease_until, "renew" if renewing else "buy")
            return {"seat_rank": int(seat_rank), "clan_slug": seat["clan_slug"], "price": price, "balance": balance,
                    "lease_until": lease_until, "displaced_id": displaced, "renewed": renewing}
