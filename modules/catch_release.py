"""Release a creature back into the wild (Phase 3 follow-up).

* ``release_creature`` is the ONLY code path that removes a creature without paying for it.
  Sell (``catch_sell``) pays coins; Release pays ``RELEASE_COINS`` (0 by default, a tuning
  constant). One transaction, lock order player row first, then creature row:
  lock the player row, lock the creature row (scoped by id, user_id and clone_key in SQL, so
  a forged or stale id can never release someone else's creature), refuse favourites and
  locked creatures, ``DELETE ... RETURNING``, clear ``buddy_id`` when it pointed at this
  creature, pay ``RELEASE_COINS`` if any, and write a ``release`` audit row.
* Two taps at once serialise on the row lock: the second finds no row and does nothing.
* If the player row is missing the release raises and rolls back, so a creature is never
  deleted without its buddy pointer and audit trail being handled.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from modules import catch_db
from modules.catch_species import all_species

RELEASE_COINS = 0  # placeholder for balance tuning; Sell is the coin path


@dataclass(frozen=True, slots=True)
class ReleaseResult:
    ok: bool
    reason: str | None = None  # not_found, favorite, locked, no_player
    name: str = ""
    was_buddy: bool = False
    coins: int = 0


def _species_name(species_id: int) -> str:
    sp = all_species().get(int(species_id)) or {}
    return str(sp.get("name") or f"Species {species_id}")


async def release_creature(
    owned_id: int, user_id: int, clone_id: int | None, *, guild_id: int | None = None, conn=None,
) -> ReleaseResult:
    """Release one creature. Never raises for a refusal; returns ok=False with a reason."""
    key = catch_db.clone_key(clone_id)
    async with catch_db.transaction(conn) as c:
        player = await c.fetchrow(
            "SELECT buddy_id FROM catch_players WHERE user_id = $1 AND clone_key = $2 FOR UPDATE",
            user_id, key,
        )
        row = await c.fetchrow(
            "SELECT id, species_id, level, nickname, shiny, special, favorite, locked FROM catch_owned "
            "WHERE id = $1 AND user_id = $2 AND clone_key = $3 FOR UPDATE",
            owned_id, user_id, key,
        )
        if row is None:
            return ReleaseResult(False, "not_found")
        name = _species_name(row["species_id"])
        if player is None:
            return ReleaseResult(False, "no_player", name)
        if row["favorite"]:
            return ReleaseResult(False, "favorite", name)
        if row["locked"]:
            return ReleaseResult(False, "locked", name)
        deleted = await c.fetchval(
            "DELETE FROM catch_owned WHERE id = $1 AND user_id = $2 AND clone_key = $3 RETURNING id",
            owned_id, user_id, key,
        )
        if deleted is None:
            return ReleaseResult(False, "not_found")
        was_buddy = player["buddy_id"] is not None and int(player["buddy_id"]) == int(owned_id)
        updated = await c.fetchval(
            "UPDATE catch_players SET "
            "buddy_id = CASE WHEN buddy_id = $3 THEN NULL ELSE buddy_id END, "
            "coins = coins + $4, updated_at = now() "
            "WHERE user_id = $1 AND clone_key = $2 RETURNING 1",
            user_id, key, owned_id, RELEASE_COINS,
        )
        if updated is None:
            raise RuntimeError("player row missing; release rolled back")
        await c.execute(
            "INSERT INTO catch_audit (actor_id, user_id, clone_id, guild_id, action, ref_id, detail) "
            "VALUES ($1, $1, $2, $3, 'release', $4, $5::jsonb)",
            user_id, clone_id, guild_id, owned_id,
            json.dumps({
                "owned_id": owned_id, "species_id": int(row["species_id"]), "name": name,
                "level": int(row["level"]), "shiny": bool(row["shiny"]), "special": bool(row["special"]),
                "was_buddy": was_buddy, "coins": RELEASE_COINS,
            }),
        )
    return ReleaseResult(True, None, name, was_buddy, RELEASE_COINS)


__all__ = ["RELEASE_COINS", "ReleaseResult", "release_creature"]
