"""Sell creatures for coins (economy slice).

* ``sell_creature`` is the ONLY code path that pays coins for a creature. One transaction:
  ``SELECT ... FOR UPDATE`` on the creature row (scoped by id, user_id and clone_key in SQL,
  so a forged or stale id can never sell someone else's creature), refusal of favourites and
  locked creatures, ``DELETE`` of the row, the coin credit, and a ``sell`` audit row. Two
  taps at once serialise on the row lock: the second finds no row and pays nothing. The
  coins are only credited if the ``DELETE ... RETURNING`` really removed the row, so even
  without the lock a creature can never be paid for twice.
* The sale value is computed here from the species rarity (``Rarity.sell_value``) and the
  shiny / special multipliers in ``catch_game``; the client never sends a price.
* If the player row is missing the sale raises and rolls back, so a creature is never
  deleted without its coins being paid.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from modules import catch_db, catch_emoji
from modules.catch_game import (
    RARITY_BY_KEY,
    SHINY_SELL_MULTIPLIER,
    SPECIAL_SELL_MULTIPLIER,
)
from modules.catch_species import all_species

SELL_PAGE_SIZE = 10
RARITY_MARK = catch_emoji.RARITY  # one shared table: restyle it in modules/catch_emoji.py


@dataclass(frozen=True, slots=True)
class SellRow:
    id: int
    species_id: int
    name: str
    rarity: str
    level: int
    nickname: str | None
    shiny: bool
    special: bool
    value: int


@dataclass(frozen=True, slots=True)
class SellResult:
    ok: bool
    reason: str | None = None  # not_found, favorite, locked, unknown_species
    name: str = ""
    value: int = 0
    coins_left: int = 0


def _species_info(species_id: int) -> tuple[str, str | None]:
    sp = all_species().get(int(species_id)) or {}
    rarity = sp.get("rarity")
    return str(sp.get("name") or f"Species {species_id}"), (str(rarity) if rarity in RARITY_BY_KEY else None)


def sale_value(rarity_key: str, shiny: bool = False, special: bool = False) -> int:
    """Coins paid for one creature. Special beats shiny; the two never stack."""
    base = RARITY_BY_KEY[rarity_key].sell_value
    if special:
        return base * SPECIAL_SELL_MULTIPLIER
    if shiny:
        return base * SHINY_SELL_MULTIPLIER
    return base


def display_name(row: SellRow) -> str:
    prefix = "Shiny " if row.shiny else ""
    return f"{prefix}{row.nickname or row.name}"


async def list_sellable(
    user_id: int, clone_id: int | None, *, page: int = 0, per_page: int = SELL_PAGE_SIZE, conn=None,
) -> tuple[list[SellRow], int, int]:
    """Return (rows, total, page) for creatures that may be sold (not favourite, not locked)."""
    key = catch_db.clone_key(clone_id)
    async with catch_db.connection(conn) as db:
        total = int(await db.fetchval(
            "SELECT count(*) FROM catch_owned "
            "WHERE user_id = $1 AND clone_key = $2 AND NOT favorite AND NOT locked",
            user_id, key,
        ) or 0)
        pages = max(1, -(-total // per_page))
        page = min(max(0, page), pages - 1)
        records = await db.fetch(
            "SELECT id, species_id, level, nickname, shiny, special FROM catch_owned "
            "WHERE user_id = $1 AND clone_key = $2 AND NOT favorite AND NOT locked "
            "ORDER BY caught_at DESC, id DESC LIMIT $3 OFFSET $4",
            user_id, key, per_page, page * per_page,
        )
    rows: list[SellRow] = []
    for rec in records:
        name, rarity = _species_info(rec["species_id"])
        if rarity is None:
            continue  # a species we cannot price is never offered
        rows.append(SellRow(
            int(rec["id"]), int(rec["species_id"]), name, rarity, int(rec["level"]), rec["nickname"],
            bool(rec["shiny"]), bool(rec["special"]), sale_value(rarity, bool(rec["shiny"]), bool(rec["special"])),
        ))
    return rows, total, page


async def sell_creature(
    owned_id: int, user_id: int, clone_id: int | None, *, guild_id: int | None = None, conn=None,
) -> SellResult:
    """Sell one creature. Never raises for a refusal; returns ok=False with a reason."""
    key = catch_db.clone_key(clone_id)
    async with catch_db.transaction(conn) as c:
        row = await c.fetchrow(
            "SELECT id, species_id, level, shiny, special, favorite, locked FROM catch_owned "
            "WHERE id = $1 AND user_id = $2 AND clone_key = $3 FOR UPDATE",
            owned_id, user_id, key,
        )
        if row is None:
            return SellResult(False, "not_found")
        name, rarity = _species_info(row["species_id"])
        if row["favorite"]:
            return SellResult(False, "favorite", name)
        if row["locked"]:
            return SellResult(False, "locked", name)
        if rarity is None:
            return SellResult(False, "unknown_species", name)
        value = sale_value(rarity, bool(row["shiny"]), bool(row["special"]))
        deleted = await c.fetchval(
            "DELETE FROM catch_owned WHERE id = $1 AND user_id = $2 AND clone_key = $3 RETURNING id",
            owned_id, user_id, key,
        )
        if deleted is None:  # someone else sold it first; pay nothing (belt and braces with the row lock)
            return SellResult(False, "not_found")
        coins_left = await c.fetchval(
            "UPDATE catch_players SET coins = coins + $3, updated_at = now() "
            "WHERE user_id = $1 AND clone_key = $2 RETURNING coins",
            user_id, key, value,
        )
        if coins_left is None:
            raise RuntimeError("player row missing; sale rolled back")
        await c.execute(
            "INSERT INTO catch_audit (actor_id, user_id, clone_id, guild_id, action, detail) "
            "VALUES ($1, $1, $2, $3, 'sell', $4::jsonb)",
            user_id, clone_id, guild_id,
            json.dumps({
                "owned_id": owned_id, "species_id": int(row["species_id"]), "name": name, "rarity": rarity,
                "level": int(row["level"]), "shiny": bool(row["shiny"]), "special": bool(row["special"]),
                "coins": value,
            }),
        )
    return SellResult(True, None, name, value, int(coins_left))


__all__ = [
    "RARITY_MARK", "SELL_PAGE_SIZE", "SellResult", "SellRow", "display_name", "list_sellable",
    "sale_value", "sell_creature",
]
