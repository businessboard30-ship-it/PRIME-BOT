"""Status: a read-only snapshot of one player's catch progress.

Coins, catch counts, streaks, Daily readiness, collection size and Dex progress. Every
read is scoped to ``(user_id, clone_key)``. Nothing here writes, and a player with no row
yet simply reads as zeros (looking at Status never creates a player).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from modules import catch_db
from modules.catch_collection import dex_summary, load_dex
from modules.catch_items import DAILY_COOLDOWN_SECONDS


@dataclass(frozen=True, slots=True)
class PlayerStatus:
    coins: int = 0
    total_catches: int = 0
    catch_streak: int = 0
    best_streak: int = 0
    daily_streak: int = 0
    daily_ready_at: datetime | None = None  # None means a Daily can be claimed now
    owned: int = 0
    shinies: int = 0
    favourites: int = 0
    dex_caught: int = 0
    dex_seen: int = 0
    dex_total: int = 0


def daily_ready_at(last_daily_at: datetime | None, now: datetime | None = None) -> datetime | None:
    """When the next Daily opens, or None if it is open now."""
    if last_daily_at is None:
        return None
    ready = last_daily_at + timedelta(seconds=DAILY_COOLDOWN_SECONDS)
    return ready if ready > (now or datetime.now(timezone.utc)) else None


async def load_status(user_id: int, clone_id: int | None, *, conn=None) -> PlayerStatus:
    key = catch_db.clone_key(clone_id)
    async with catch_db.connection(conn) as db:
        player = await db.fetchrow(
            "SELECT coins, total_catches, catch_streak, best_streak, daily_streak, last_daily_at "
            "FROM catch_players WHERE user_id=$1 AND clone_key=$2",
            user_id, key,
        )
        owned = await db.fetchrow(
            "SELECT count(*) AS owned, count(*) FILTER (WHERE shiny) AS shinies, "
            "count(*) FILTER (WHERE favorite) AS favourites "
            "FROM catch_owned WHERE user_id=$1 AND clone_key=$2",
            user_id, key,
        )
        entries = await load_dex(user_id, clone_id, conn=db)
    caught, seen, total = dex_summary(entries)
    p = player or {}
    o = owned or {}
    return PlayerStatus(
        coins=int(p.get("coins") or 0), total_catches=int(p.get("total_catches") or 0),
        catch_streak=int(p.get("catch_streak") or 0), best_streak=int(p.get("best_streak") or 0),
        daily_streak=int(p.get("daily_streak") or 0), daily_ready_at=daily_ready_at(p.get("last_daily_at")),
        owned=int(o.get("owned") or 0), shinies=int(o.get("shinies") or 0), favourites=int(o.get("favourites") or 0),
        dex_caught=caught, dex_seen=seen, dex_total=total,
    )


__all__ = ["PlayerStatus", "daily_ready_at", "load_status"]
