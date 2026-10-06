"""record_catch: the ONE function that grants a new creature (P1-04, rule 0.15).

Every source (wild spawn, encounter, fishing, egg, lootbox, ...) calls this.
It runs in a single transaction and is safe to retry:

* spawn catches claim the spawn row atomically (only one player wins a
  double click); a retry by the winner returns the same creature.
* other sources must pass an ``idem_key``; a retry with the same key
  returns the creature created the first time instead of a second one.

Which counters a source feeds is read from data/catch/counter_matrix.json,
never decided here. Counters whose tables arrive in later phases are
listed there but skipped until their name is added to APPLIED_COUNTERS.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from modules import catch_db, catch_gate
from modules.catch_game import BAIT_BONUS, BALLS, IV_MAX, LEVEL_MAX, LEVEL_MIN, SOURCES, STAT_NAMES

MATRIX_PATH = Path(__file__).resolve().parent.parent / "data" / "catch" / "counter_matrix.json"
APPLIED_COUNTERS = frozenset({"ownership", "dex", "player_total", "streak"})


class CatchBlocked(Exception):
    """The gate refused the catch (feature off, sanction, ...)."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class CatchResult:
    claimed: bool
    owned_id: int | None = None
    species_id: int | None = None
    level: int | None = None
    shiny: bool = False
    new_species: bool = False
    replay: bool = False
    reason: str | None = None


@lru_cache(maxsize=1)
def counter_matrix() -> dict[str, frozenset[str]]:
    with open(MATRIX_PATH, encoding="utf-8") as fh:
        raw = json.load(fh)
    return {source: frozenset(counters) for source, counters in raw["sources"].items()}


def counters_for(source: str) -> frozenset[str]:
    matrix = counter_matrix()
    if source not in matrix:
        raise KeyError(f"source {source!r} missing from counter_matrix.json")
    return matrix[source]


def _check_values(species_id: int, level: int, ivs: list[int]) -> None:
    if not isinstance(species_id, int) or species_id <= 0:
        raise ValueError("species_id must be a positive int")
    if not LEVEL_MIN <= level <= LEVEL_MAX:
        raise ValueError(f"level must be {LEVEL_MIN}..{LEVEL_MAX}")
    if len(ivs) != len(STAT_NAMES) or not all(0 <= v <= IV_MAX for v in ivs):
        raise ValueError(f"ivs must be {len(STAT_NAMES)} ints 0..{IV_MAX}")


async def _owned_result(c, owned_id: int, *, replay: bool) -> CatchResult:
    row = await c.fetchrow(
        "SELECT id, species_id, level, shiny FROM catch_owned WHERE id = $1", owned_id
    )
    return CatchResult(
        claimed=True, owned_id=row["id"], species_id=row["species_id"],
        level=row["level"], shiny=row["shiny"], replay=replay,
    )


async def record_catch(
    *,
    user_id: int,
    clone_id: int | None,
    guild_id: int | None,
    source: str,
    species_id: int | None = None,
    level: int | None = None,
    shiny: bool = False,
    ivs: list[int] | None = None,
    special: bool = False,
    spawn_id: int | None = None,
    idem_key: str | None = None,
    ball: str = "capsule_basic",
    bait: str | None = None,
    conn=None,
) -> CatchResult:
    if source not in SOURCES:
        raise KeyError(f"unknown source: {source}")
    if ball not in BALLS:
        raise ValueError(f"unknown ball: {ball}")
    if bait is not None and bait not in BAIT_BONUS:
        raise ValueError(f"unknown bait: {bait}")
    feeds = counters_for(source)
    if (spawn_id is None) == (idem_key is None):
        raise ValueError("pass exactly one of spawn_id or idem_key")

    async with catch_db.transaction(conn) as c:
        gate = await catch_gate.check_player_allowed(user_id, guild_id, "catch", clone_id, conn=c)
        if not gate.allowed:
            raise CatchBlocked(gate.reason or "blocked")

        if spawn_id is not None:
            spawn = await c.fetchrow(
                """
                UPDATE catch_spawns SET caught_by = $2, caught_at = now()
                WHERE id = $1 AND caught_by IS NULL AND NOT fled AND expires_at > now()
                  AND clone_key = $3
                  AND (owner_user_id IS NULL OR owner_user_id = $2)
                RETURNING species_id, level, shiny, ivs, guild_id
                """,
                spawn_id, user_id, catch_db.clone_key(clone_id),
            )
            if spawn is None:
                existing = await c.fetchval(
                    "SELECT o.id FROM catch_owned o JOIN catch_spawns s ON s.id = o.spawn_id "
                    "WHERE o.spawn_id = $1 AND s.caught_by = $2",
                    spawn_id, user_id,
                )
                if existing is not None:
                    return await _owned_result(c, existing, replay=True)
                return CatchResult(claimed=False, reason="spawn_unavailable")
            # The spawn row is the truth; never trust caller-supplied rolls here.
            species_id, level, shiny = spawn["species_id"], spawn["level"], spawn["shiny"]
            ivs, guild_id = list(spawn["ivs"]), spawn["guild_id"]
            for item_key in [ball] + ([bait] if bait else []):
                consumed = await c.fetchval(
                    """
                    UPDATE catch_inventory SET quantity = quantity - 1, updated_at = now()
                    WHERE user_id = $1 AND clone_key = $2 AND item_key = $3 AND quantity > 0
                    RETURNING item_key
                    """,
                    user_id, catch_db.clone_key(clone_id), item_key,
                )
                if consumed is None:
                    raise ValueError(f"{item_key} is not available")
        else:
            if species_id is None or level is None or ivs is None:
                raise ValueError("species_id, level and ivs are required without a spawn")
            existing = await c.fetchval(
                "SELECT id FROM catch_owned WHERE idem_key = $1", idem_key
            )
            if existing is not None:
                return await _owned_result(c, existing, replay=True)

        _check_values(species_id, level, ivs)
        owned_id = await c.fetchval(
            """
            INSERT INTO catch_owned (user_id, clone_id, species_id, level, shiny, special,
                ivs, source, caught_in_guild, spawn_id, idem_key)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
            ON CONFLICT DO NOTHING
            RETURNING id
            """,
            user_id, clone_id, species_id, level, shiny, special, ivs,
            SOURCES[source], guild_id, spawn_id, idem_key,
        )
        if owned_id is None:
            conflict_key = spawn_id if spawn_id is not None else idem_key
            existing = await c.fetchrow(
                "SELECT id, user_id FROM catch_owned "
                f"WHERE {'spawn_id' if spawn_id is not None else 'idem_key'} = $1",
                conflict_key,
            )
            if existing is not None and existing["user_id"] == user_id:
                return await _owned_result(c, existing["id"], replay=True)
            return CatchResult(claimed=False, reason="already_claimed")

        new_species = False
        if "dex" in feeds:
            caught_count = await c.fetchval(
                """
                INSERT INTO catch_dex (user_id, clone_id, species_id, seen, caught_count,
                    shiny_caught, first_caught_at)
                VALUES ($1, $2, $3, TRUE, 1, $4, now())
                ON CONFLICT (user_id, clone_key, species_id) DO UPDATE SET
                    seen = TRUE,
                    caught_count = catch_dex.caught_count + 1,
                    shiny_caught = catch_dex.shiny_caught + EXCLUDED.shiny_caught,
                    first_caught_at = COALESCE(catch_dex.first_caught_at, now())
                RETURNING caught_count
                """,
                user_id, clone_id, species_id, 1 if shiny else 0,
            )
            new_species = caught_count == 1

        total_inc = 1 if "player_total" in feeds else 0
        streak_inc = 1 if "streak" in feeds else 0
        await c.execute(
            """
            INSERT INTO catch_players (user_id, clone_id, total_catches, catch_streak, best_streak)
            VALUES ($1, $2, $3, $4, $4)
            ON CONFLICT (user_id, clone_key) DO UPDATE SET
                total_catches = catch_players.total_catches + $3,
                catch_streak = catch_players.catch_streak + $4,
                best_streak = GREATEST(catch_players.best_streak, catch_players.catch_streak + $4),
                updated_at = now()
            """,
            user_id, clone_id, total_inc, streak_inc,
        )

        await c.execute(
            """
            INSERT INTO catch_audit (actor_id, user_id, clone_id, guild_id, action, ref_id, detail)
            VALUES ($1, $1, $2, $3, 'catch', $4,
                jsonb_build_object('source', $5::text, 'species_id', $6::int,
                                   'shiny', $7::boolean, 'new_species', $8::boolean))
            """,
            user_id, clone_id, guild_id, owned_id, source, species_id, shiny, new_species,
        )

    return CatchResult(
        claimed=True, owned_id=owned_id, species_id=species_id, level=level,
        shiny=shiny, new_species=new_species,
    )


async def break_streak(user_id: int, clone_id: int | None, conn=None) -> None:
    """Called on a failed throw (Phase 2)."""
    async with catch_db.transaction(conn) as c:
        await c.execute(
            "UPDATE catch_players SET catch_streak = 0, updated_at = now() "
            "WHERE user_id = $1 AND clone_key = $2",
            user_id, catch_db.clone_key(clone_id),
        )
