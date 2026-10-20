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
from datetime import datetime
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


PROMOTE_SHARE = 5               # one in five (top and bottom) moves each season


def moves(ranked: List[tuple], division: int) -> dict:
    """New divisions for one division's ranked entrants [(owner_id, points), ...] best first. The top fifth go up (only with
    at least one point, never above Diamond), the bottom fifth go down (never below Bronze). Needs 5+ entrants to move anyone."""
    n = len(ranked)
    k = n // PROMOTE_SHARE
    out = {int(o): int(division) for o, _ in ranked}
    if k <= 0:
        return out
    if division < len(DIVISIONS) - 1:
        for o, pts in ranked[:k]:
            if int(pts) > 0:
                out[int(o)] = int(division) + 1
    if division > 0:
        for o, _ in ranked[n - k:]:
            out[int(o)] = int(division) - 1
    return out


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
    ends_at: Optional[datetime] = None


@dataclass(frozen=True)
class JoinResult:
    status: str                 # ok | me_off | poor | already
    season_id: int = 0
    division: int = 0
    coins: Optional[int] = None


async def _pool():
    from database import get_pool
    return await get_pool()


async def _close_season(conn, season_id: int, key: int) -> None:
    """Close a season that ran out: promote the top fifth of each division, relegate the bottom fifth, and remember where
    everyone starts next time (cat_league_next). Inside the caller's transaction."""
    await conn.execute("UPDATE cat_league_seasons SET status = 'closed' WHERE id = $1 AND status = 'active'", int(season_id))
    rows = await conn.fetch("SELECT owner_id, division, points FROM cat_league_entries WHERE season_id = $1 "
                            "ORDER BY division, points DESC, wins DESC, joined_at, owner_id", int(season_id))
    by_div: dict = {}
    for r in rows:
        by_div.setdefault(int(r["division"]), []).append((int(r["owner_id"]), int(r["points"])))
    owners, divs = [], []
    for div, ranked in by_div.items():
        for owner, new_div in moves(ranked, div).items():
            owners.append(owner)
            divs.append(new_div)
    if owners:                                       # ONE statement for everyone (it used to be one per player)
        await conn.execute("INSERT INTO cat_league_next (clone_key, owner_id, division) "
                           "SELECT $1, u, d FROM unnest($2::bigint[], $3::int[]) AS t(u, d) "
                           "ON CONFLICT (clone_key, owner_id) DO UPDATE SET division = EXCLUDED.division", int(key), owners, divs)


async def _current_season(conn, key: int) -> int:
    """The active season of this bot. One that ran out is closed (with promotions) and the next one starts. Needs the
    caller's transaction."""
    row = await conn.fetchrow("SELECT id, (ends_at <= NOW()) AS ended FROM cat_league_seasons WHERE clone_key = $1 AND status = 'active' "
                              "FOR UPDATE", key)
    if row is not None and row["ended"]:
        await _close_season(conn, int(row["id"]), key)
        row = None
    if row is not None:
        return int(row["id"])
    sid = await conn.fetchval("INSERT INTO cat_league_seasons (clone_key, ends_at) VALUES ($1, NOW() + $2 * INTERVAL '1 day') "
                              "ON CONFLICT DO NOTHING RETURNING id", key, SEASON_DAYS)
    if sid is None:                                              # another bot process made it first
        sid = await conn.fetchval("SELECT id FROM cat_league_seasons WHERE clone_key = $1 AND status = 'active'", key)
    return int(sid)


async def rollover(limit: int = 5) -> int:
    """Close seasons that ran out (main-bot sweep). Each one in its own transaction; rows another worker holds are skipped."""
    done = 0
    p = await _pool()
    async with p.acquire() as conn:
        ids = [(int(r["id"]), int(r["clone_key"])) for r in await conn.fetch(
            "SELECT id, clone_key FROM cat_league_seasons WHERE status = 'active' AND ends_at <= NOW() ORDER BY id LIMIT $1", int(limit))]
    for sid, key in ids:
        try:
            async with p.acquire() as conn:
                async with conn.transaction():
                    row = await conn.fetchrow("SELECT id FROM cat_league_seasons WHERE id = $1 AND status = 'active' AND ends_at <= NOW() "
                                              "FOR UPDATE SKIP LOCKED", sid)
                    if row is None:
                        continue
                    await _close_season(conn, sid, key)
                    done += 1
        except Exception:
            logger.warning("[cat-league] couldn't close season %s", sid, exc_info=True)
    return done


async def play_points(conn, key: int, winner_id: int, loser_id: int) -> None:
    """Called inside a fight's settlement: when BOTH players are entered in this bot's active season, each gets their points.
    Never raises into the settlement (a League hiccup must not undo a fight), and reads before it writes."""
    try:
        sid = await conn.fetchval("SELECT id FROM cat_league_seasons WHERE clone_key = $1 AND status = 'active' AND ends_at > NOW()", int(key))
        if sid is None:
            return
        n = await conn.fetchval("SELECT COUNT(*) FROM cat_league_entries WHERE season_id = $1 AND owner_id = ANY($2::bigint[])",
                                int(sid), [int(winner_id), int(loser_id)])
        if int(n or 0) != 2:
            return
        await award(conn, int(sid), int(winner_id), True)
        await award(conn, int(sid), int(loser_id), False)
    except Exception:
        logger.warning("[cat-league] couldn't award points", exc_info=True)


async def season_info(clone_id):
    """(season_id, ends_at) of this bot's active season, or None when nobody has joined yet. Never raises."""
    try:
        p = await _pool()
        async with p.acquire() as conn:
            r = await conn.fetchrow("SELECT id, ends_at FROM cat_league_seasons WHERE clone_key = $1 AND status = 'active' AND ends_at > NOW()",
                                    cat_arena._bk(clone_id))
        return (int(r["id"]), r["ends_at"]) if r else None
    except Exception:
        logger.debug("[cat-league] couldn't read the season", exc_info=True)
        return None


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
            kept = await conn.fetchval("SELECT division FROM cat_league_next WHERE clone_key = $1 AND owner_id = $2", key, int(owner_id))
            div = int(kept) if kept is not None else division_for(int(me["rating"]))
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
            r = await conn.fetchrow("SELECT e.season_id, e.owner_id, e.division, e.points, e.wins, e.losses, s.ends_at FROM cat_league_entries e "
                                    "JOIN cat_league_seasons s ON s.id = e.season_id WHERE e.owner_id = $1 AND s.clone_key = $2 "
                                    "AND s.status = 'active' AND s.ends_at > NOW()", int(owner_id), cat_arena._bk(clone_id))
        return Entry(**{k: int(r[k]) for k in ("season_id", "owner_id", "division", "points", "wins", "losses")}, ends_at=r["ends_at"]) if r else None
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
