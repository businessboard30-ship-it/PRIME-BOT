"""Trainer profile card (Phase 3: P3-07). Strictly read-only.

Four SELECTs scoped by ``(user_id, clone_key)``: the player row, the buddy (joined to the
creature so a buddy that was sold or deleted reads as "no buddy"), the rarest creature, and
the collection counts; the Dex comes from the existing ``load_dex``. A player with no row
reads as zeros and nothing is created. Badges and crew are Phase 4/5 features and are not
shown until they exist.
"""

from __future__ import annotations

from dataclasses import dataclass

from modules import catch_db
from modules.catch_collection import dex_summary, load_dex
from modules.catch_species import all_species


@dataclass(frozen=True, slots=True)
class ProfileCreature:
    id: int
    name: str
    rarity: str
    level: int
    nickname: str | None
    shiny: bool
    special: bool
    element: str = ""
    element2: str | None = None
    species_id: int = 0

    @property
    def display_name(self) -> str:
        return self.nickname or self.name


@dataclass(frozen=True, slots=True)
class TrainerProfile:
    total_catches: int = 0
    catch_streak: int = 0
    best_streak: int = 0
    daily_streak: int = 0
    owned: int = 0
    shinies: int = 0
    specials: int = 0
    dex_caught: int = 0
    dex_seen: int = 0
    dex_total: int = 0
    buddy: ProfileCreature | None = None
    rarest: ProfileCreature | None = None

    @property
    def dex_percent(self) -> int:
        return 0 if self.dex_total <= 0 else int(100 * self.dex_caught / self.dex_total)


def _creature(rec) -> ProfileCreature | None:
    if rec is None:
        return None
    species = all_species().get(int(rec["species_id"]))
    if species is None:
        return None
    return ProfileCreature(
        int(rec["id"]), str(species["name"]), str(species["rarity"]), int(rec["level"]),
        rec["nickname"], bool(rec["shiny"]), bool(rec["special"]),
        str(species.get("element") or ""), species.get("element2") or None, int(rec["species_id"]),
    )


async def load_profile(user_id: int, clone_id: int | None, *, conn=None) -> TrainerProfile:
    key = catch_db.clone_key(clone_id)
    async with catch_db.connection(conn) as db:
        player = await db.fetchrow(
            "SELECT total_catches, catch_streak, best_streak, daily_streak, buddy_id "
            "FROM catch_players WHERE user_id = $1 AND clone_key = $2",
            user_id, key,
        )
        counts = await db.fetchrow(
            "SELECT count(*) AS owned, count(*) FILTER (WHERE shiny) AS shinies, "
            "count(*) FILTER (WHERE special) AS specials "
            "FROM catch_owned WHERE user_id = $1 AND clone_key = $2",
            user_id, key,
        )
        rarest = await db.fetchrow(
            "SELECT o.id, o.species_id, o.level, o.nickname, o.shiny, o.special "
            "FROM catch_owned o JOIN catch_species s ON s.id = o.species_id "
            "WHERE o.user_id = $1 AND o.clone_key = $2 "
            "ORDER BY o.special DESC, s.rarity DESC, o.shiny DESC, o.level DESC, o.id ASC LIMIT 1",
            user_id, key,
        )
        buddy = None
        if player is not None and player["buddy_id"] is not None:
            buddy = await db.fetchrow(
                "SELECT id, species_id, level, nickname, shiny, special FROM catch_owned "
                "WHERE id = $1 AND user_id = $2 AND clone_key = $3",
                int(player["buddy_id"]), user_id, key,
            )
        entries = await load_dex(user_id, clone_id, conn=db)
    caught, seen, total = dex_summary(entries)
    return TrainerProfile(
        total_catches=int(player["total_catches"]) if player else 0,
        catch_streak=int(player["catch_streak"]) if player else 0,
        best_streak=int(player["best_streak"]) if player else 0,
        daily_streak=int(player["daily_streak"]) if player else 0,
        owned=int(counts["owned"]) if counts else 0,
        shinies=int(counts["shinies"]) if counts else 0,
        specials=int(counts["specials"]) if counts else 0,
        dex_caught=caught, dex_seen=seen, dex_total=total,
        buddy=_creature(buddy), rarest=_creature(rarest),
    )


__all__ = ["ProfileCreature", "TrainerProfile", "load_profile"]
