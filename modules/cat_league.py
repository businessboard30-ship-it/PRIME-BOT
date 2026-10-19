# path: modules/cat_league.py

"""
Cat League, slice A (Phase 5 of the fights plan): the data layer only, no screens and no settlement hook yet.

A player pays a one-time entry fee (a coin sink, never refunded) to join the current 28-day season of their bot. They are
placed in one of five divisions by their Arena rating. Fights between two entrants will award points in the next slice
(`award` is here and tested, but nothing calls it yet). Play coins only. Pure rules at the top, queries below.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Optional

from modules import cat_arena

logger = logging.getLogger(__name__)

SEASON_DAYS = 28
DIVISIONS = ("Bronze", "Silver", "Gold", "Platinum", "Diamond")
DIVISION_FROM_RATING = (0, 1100, 1250, 1400, 1600)      # lowest rating for each division
ENTRY_FEE = 1_000                                       # coins, once per season, not refunded
POINTS_WIN, POINTS_LOSS = 3, 1                          # a loss still earns a point, so playing always counts
STANDINGS_SIZE = 10

STATUS_TEXT = {
    "me_off": cat_arena.STATUS_TEXT["me_off"],
    "poor": cat_arena.STATUS_TEXT["poor"],
    "already": "You are already in this season's League.",
}


# ── pure rules ──────────────────────────────────────────────────────────

def division_for(rating: int) -> int:
    """0..4 from the Arena rating."""
    d = 0
    for i, floor in enumerate(DIVISION_FROM_RATING):
        if int(rating) >= floor:
            d = i
    return d


def division_name(division: int) -> str:
    return DIVISIONS[max(0, min(len(DIVISIONS) - 1, int(division)))]


def points_for(won: bool) -> int:
    return POINTS_WIN if won else POINTS_LOSS


def rank_key(points: int, wins: int, joined_order: int) -> tuple:
    """Sort key for standings: more points, then more wins, then who joined first."""
    return (-int(points), -int(wins), int(joined_order))


# ── data ────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Entry:
    season_id: int
    owner_id: int
    division: int
    points: int = 0
    wins: int = 0
    losses: int = 0


@dataclass(frozen=True)
class JoinResult:
    status: str                 # ok | me_off | poor | already
    season_id: int = 0
    division: int = 0
    coins: Optional[int] = None


async def _pool():
    from database import get_pool
    return await get_pool()


async def _current_season(conn, key: int) -> int:
    """The active season of this bot; closes one that ran out and starts the next. Needs the caller's transaction."""
    await conn.execute("UPDATE cat_league_seasons SET status = 'closed' WHERE clone_key = $1 AND status = 'active' AND ends_at <= NOW()", key)
    sid = await conn.fetchval("SELECT id FROM cat_league_seasons WHERE clone_key = $1 AND status = 'active'", key)
    if sid is None:
        sid = await conn.fetchval("INSERT INTO cat_league_seasons (clone_key, ends_at) VALUES ($1, NOW() + $2 * INTERVAL '1 day') "
                                  "ON CONFLICT DO NOTHING RETURNING id", key, SEASON_DAYS)
        if sid is None:                                          # another bot process made it first
            sid = await conn.fetchval("SELECT id FROM cat_league_seasons WHERE clone_key = $1 AND status = 'active'", key)
    return int(sid)


async def join(owner_id: int, clone_id) -> JoinResult:
    """Pay the entry fee and enter the current season, in ONE transaction. Reads first, then the fee, then the entry."""
    key = cat_arena._bk(clone_id)
    p = await _pool()
    async with p.acquire() as conn:
        async with conn.transaction():
            me = await conn.fetchrow("SELECT arena_on, rating FROM cat_arena WHERE owner_id = $1 AND bot_key = $2", int(owner_id), key)
            if not me or not me["arena_on"]:
                return JoinResult("me_off")
            sid = await _current_season(conn, key)
            if await conn.fetchval("SELECT 1 FROM cat_league_entries WHERE season_id = $1 AND owner_id = $2", sid, int(owner_id)):
                return JoinResult("already", sid)
            row = await conn.fetchrow("UPDATE cat_wallets SET coins = coins - $3, updated_at = NOW() "
                                      "WHERE owner_id = $1 AND COALESCE(clone_id, -1) = $2 AND coins >= $3 RETURNING coins",
                                      int(owner_id), -1, ENTRY_FEE)
            if row is None:
                return JoinResult("poor", sid)
            div = division_for(int(me["rating"]))
            await conn.execute("INSERT INTO cat_league_entries (season_id, owner_id, division, fee) VALUES ($1, $2, $3, $4)",
                               sid, int(owner_id), div, ENTRY_FEE)
    return JoinResult("ok", sid, div, int(row["coins"]))


async def award(conn, season_id: int, owner_id: int, won: bool) -> bool:
    """Add one fight's points to an entrant (inside the caller's transaction). False when they aren't entered."""
    got = await conn.fetchval("UPDATE cat_league_entries SET points = points + $3, wins = wins + $4, losses = losses + $5 "
                              "WHERE season_id = $1 AND owner_id = $2 RETURNING 1", int(season_id), int(owner_id),
                              points_for(won), 1 if won else 0, 0 if won else 1)
    return got is not None


async def my_entry(owner_id: int, clone_id) -> Optional[Entry]:
    """This player's entry in the active season, or None. Never raises."""
    try:
        p = await _pool()
        async with p.acquire() as conn:
            r = await conn.fetchrow("SELECT e.season_id, e.owner_id, e.division, e.points, e.wins, e.losses FROM cat_league_entries e "
                                    "JOIN cat_league_seasons s ON s.id = e.season_id WHERE e.owner_id = $1 AND s.clone_key = $2 "
                                    "AND s.status = 'active' AND s.ends_at > NOW()", int(owner_id), cat_arena._bk(clone_id))
        return Entry(**{k: int(r[k]) for k in ("season_id", "owner_id", "division", "points", "wins", "losses")}) if r else None
    except Exception:
        logger.debug("[cat-league] couldn't read my entry", exc_info=True)
        return None


async def standings(season_id: int, division: int, limit: int = STANDINGS_SIZE) -> List[Entry]:
    """Top entrants of one division. Never raises."""
    try:
        p = await _pool()
        async with p.acquire() as conn:
            rows = await conn.fetch("SELECT season_id, owner_id, division, points, wins, losses FROM cat_league_entries "
                                    "WHERE season_id = $1 AND division = $2 ORDER BY points DESC, wins DESC, joined_at, owner_id LIMIT $3",
                                    int(season_id), int(division), int(limit))
        return [Entry(**{k: int(r[k]) for k in ("season_id", "owner_id", "division", "points", "wins", "losses")}) for r in rows]
    except Exception:
        logger.debug("[cat-league] couldn't read standings", exc_info=True)
        return []
