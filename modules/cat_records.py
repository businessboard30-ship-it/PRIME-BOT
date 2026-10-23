# path: modules/cat_records.py

"""
Cat fight records (R3). Play game only. Each cat keeps fights, wins, losses, knockouts and streaks. Experience gives a
small, capped "Veteran" bonus: +1% damage power per 10 fights fought, at most +5%. It comes from fights FOUGHT (not
wins), so it never snowballs. Records are written inside the settlement transaction (cat_arena._fight_and_settle).
Practice vs NPC does not count.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, Optional
import logging

logger = logging.getLogger(__name__)

VET_EVERY = 10          # fights per step
VET_STEP_PCT = 1        # percent per step
VET_CAP_PCT = 5         # never more than this


def bonus_pct(fights: int) -> int:
    """Veteran bonus in percent for a number of fights fought."""
    return max(0, min(VET_CAP_PCT, (max(0, int(fights)) // VET_EVERY) * VET_STEP_PCT))


@dataclass(frozen=True)
class Record:
    cat_id: int
    fights: int = 0
    wins: int = 0
    losses: int = 0
    kos: int = 0
    streak: int = 0
    best_streak: int = 0

    @property
    def bonus(self) -> int:
        return bonus_pct(self.fights)

    def to_next(self) -> Optional[int]:
        """Fights until the next Veteran step, None at the cap."""
        if self.bonus >= VET_CAP_PCT:
            return None
        return VET_EVERY - (self.fights % VET_EVERY)

    @classmethod
    def from_row(cls, r) -> "Record":
        return cls(int(r["cat_id"]), int(r["fights"]), int(r["wins"]), int(r["losses"]), int(r["kos"]),
                   int(r["streak"]), int(r["best_streak"]))


async def _pool():
    from database import get_pool
    return await get_pool()


async def fights_in(conn, cat_ids: Iterable[int]) -> Dict[int, int]:
    """Fights fought per cat (0 when a cat has no record yet). Works on a connection, inside or outside a transaction."""
    ids = [int(i) for i in cat_ids]
    out = {i: 0 for i in ids}
    if ids:
        for r in await conn.fetch("SELECT cat_id, fights FROM cat_records WHERE cat_id = ANY($1::bigint[])", ids):
            out[int(r["cat_id"])] = int(r["fights"])
    return out


async def bonuses(cat_ids: Iterable[int]) -> Dict[int, int]:
    """Veteran bonus percent per cat (own connection). Never raises: a read problem means no bonus."""
    ids = [int(i) for i in cat_ids]
    try:
        p = await _pool()
        async with p.acquire() as conn:
            return {i: bonus_pct(n) for i, n in (await fights_in(conn, ids)).items()}
    except Exception:
        logger.warning("swallowed exception in bonuses", exc_info=True)
        return {i: 0 for i in ids}


async def get(cat_id: int) -> Record:
    try:
        p = await _pool()
        async with p.acquire() as conn:
            r = await conn.fetchrow("SELECT * FROM cat_records WHERE cat_id = $1", int(cat_id))
        return Record.from_row(r) if r else Record(int(cat_id))
    except Exception:
        logger.warning("swallowed exception in get", exc_info=True)
        return Record(int(cat_id))


_UPSERT_WIN = ("INSERT INTO cat_records (cat_id, fights, wins, losses, kos, streak, best_streak, last_fight_at) "
               "VALUES ($1, 1, 1, 0, $2, 1, 1, NOW()) ON CONFLICT (cat_id) DO UPDATE SET "
               "fights = cat_records.fights + 1, wins = cat_records.wins + 1, kos = cat_records.kos + $2, "
               "streak = cat_records.streak + 1, best_streak = GREATEST(cat_records.best_streak, cat_records.streak + 1), "
               "last_fight_at = NOW()")
_UPSERT_LOSS = ("INSERT INTO cat_records (cat_id, fights, wins, losses, kos, streak, best_streak, last_fight_at) "
                "VALUES ($1, 1, 0, 1, 0, 0, 0, NOW()) ON CONFLICT (cat_id) DO UPDATE SET "
                "fights = cat_records.fights + 1, losses = cat_records.losses + 1, streak = 0, last_fight_at = NOW()")


async def record_fight(conn, winner_cat: int, loser_cat: int, ko: bool) -> None:
    """Write both cats' records. Call INSIDE the settlement transaction so it commits or rolls back with the coins."""
    await conn.execute(_UPSERT_WIN, int(winner_cat), 1 if ko else 0)
    await conn.execute(_UPSERT_LOSS, int(loser_cat))


def line(rec: Record) -> str:
    """One short line for a cat card or result."""
    b = f" · Veteran +{rec.bonus}%" if rec.bonus else ""
    return f"{rec.wins}W {rec.losses}L · {rec.kos} KO · best streak {rec.best_streak}{b}"
