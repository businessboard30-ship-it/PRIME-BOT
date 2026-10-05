"""Collection and Dex queries plus pure page formatting (Phase 3 slice).

All reads are scoped to (user_id, clone_key). The only write is ``set_favorite``,
which re-validates ownership in SQL: an owned id from a custom_id/select value is
never trusted on its own.
"""

from __future__ import annotations

from dataclasses import dataclass

from modules import catch_db, catch_emoji
from modules.catch_species import all_species

COLLECTION_PAGE_SIZE = 10
DEX_PAGE_SIZE = 12

# Whitelisted ORDER BY fragments: the sort key comes from a select value, so it is
# never interpolated unless it is a key of this table.
SORTS: dict[str, str] = {
    "recent": "o.caught_at DESC, o.id DESC",
    "level": "o.level DESC, o.id DESC",
    "rarity": "s.rarity DESC, o.level DESC, o.id DESC",
    "favorites": "o.favorite DESC, o.caught_at DESC, o.id DESC",
}
RARITY_MARK = catch_emoji.RARITY  # one shared table: restyle it in modules/catch_emoji.py


@dataclass(frozen=True, slots=True)
class OwnedRow:
    id: int
    species_id: int
    name: str
    rarity: str
    level: int
    nickname: str | None
    shiny: bool
    special: bool
    favorite: bool
    locked: bool


@dataclass(frozen=True, slots=True)
class DexEntry:
    species_id: int
    name: str
    rarity: str
    element: str
    seen: bool
    caught_count: int
    shiny_caught: int

    @property
    def caught(self) -> bool:
        return self.caught_count > 0


def page_count(total: int, per_page: int) -> int:
    return max(1, -(-max(0, total) // per_page))


def clamp_page(page: int, total: int, per_page: int) -> int:
    return min(max(0, page), page_count(total, per_page) - 1)


def _species_info(species_id: int) -> tuple[str, str]:
    sp = all_species().get(int(species_id)) or {}
    return str(sp.get("name") or f"Species {species_id}"), str(sp.get("rarity") or "common")


async def list_owned(
    user_id: int, clone_id: int | None, *, page: int = 0, sort: str = "recent",
    per_page: int = COLLECTION_PAGE_SIZE, conn=None,
) -> tuple[list[OwnedRow], int, int]:
    """Return (rows, total, page) with the page clamped into range."""
    order = SORTS.get(sort, SORTS["recent"])
    async with catch_db.connection(conn) as db:
        total = int(await db.fetchval(
            "SELECT count(*) FROM catch_owned WHERE user_id=$1 AND clone_key=$2",
            user_id, catch_db.clone_key(clone_id),
        ) or 0)
        page = clamp_page(page, total, per_page)
        records = await db.fetch(
            "SELECT o.id, o.species_id, o.level, o.nickname, o.shiny, o.special, o.favorite, o.locked "
            "FROM catch_owned o JOIN catch_species s ON s.id = o.species_id "
            f"WHERE o.user_id=$1 AND o.clone_key=$2 ORDER BY {order} LIMIT $3 OFFSET $4",  # noqa: S608 - order is whitelisted
            user_id, catch_db.clone_key(clone_id), per_page, page * per_page,
        )
    rows = []
    for rec in records:
        name, rarity = _species_info(rec["species_id"])
        rows.append(OwnedRow(
            int(rec["id"]), int(rec["species_id"]), name, rarity, int(rec["level"]), rec["nickname"],
            bool(rec["shiny"]), bool(rec["special"]), bool(rec["favorite"]), bool(rec["locked"]),
        ))
    return rows, total, page


async def set_favorite(owned_id: int, user_id: int, clone_id: int | None, *, conn=None) -> bool | None:
    """Flip the favourite flag; returns the new value, or None if the user doesn't own it."""
    async with catch_db.transaction(conn) as db:
        value = await db.fetchval(
            "UPDATE catch_owned SET favorite = NOT favorite "
            "WHERE id=$1 AND user_id=$2 AND clone_key=$3 RETURNING favorite",
            owned_id, user_id, catch_db.clone_key(clone_id),
        )
    return None if value is None else bool(value)


async def load_dex(user_id: int, clone_id: int | None, *, conn=None) -> list[DexEntry]:
    """Every wild-visible species with this player's progress, in species-id order.

    Exclusive species stay hidden until the player has seen one.
    """
    async with catch_db.connection(conn) as db:
        records = await db.fetch(
            "SELECT species_id, seen, caught_count, shiny_caught FROM catch_dex WHERE user_id=$1 AND clone_key=$2",
            user_id, catch_db.clone_key(clone_id),
        )
    progress = {int(r["species_id"]): r for r in records}
    entries = []
    for species_id, sp in sorted(all_species().items()):
        rec = progress.get(species_id)
        if sp.get("exclusive_to") and rec is None:
            continue
        entries.append(DexEntry(
            species_id, str(sp["name"]), str(sp["rarity"]), str(sp["element"]),
            bool(rec["seen"]) if rec else False,
            int(rec["caught_count"]) if rec else 0,
            int(rec["shiny_caught"]) if rec else 0,
        ))
    return entries


def dex_summary(entries: list[DexEntry]) -> tuple[int, int, int]:
    """(caught, seen-or-caught, total)."""
    caught = sum(1 for e in entries if e.caught)
    seen = sum(1 for e in entries if e.seen or e.caught)
    return caught, seen, len(entries)


def format_owned_line(row: OwnedRow) -> str:
    mark = RARITY_MARK.get(row.rarity, catch_emoji.FALLBACK)
    label = f"{row.nickname} ({row.name})" if row.nickname else row.name
    flags = catch_emoji.flags(shiny=row.shiny, special=row.special, favorite=row.favorite, locked=row.locked)
    return f"{mark} `#{row.id}` **{label}** · Lv.{row.level}{(' ' + flags) if flags else ''}"


def format_dex_line(entry: DexEntry) -> str:
    number = f"`{entry.species_id:03d}`"
    if entry.caught:
        shiny = f" ✨×{entry.shiny_caught}" if entry.shiny_caught else ""
        return f"{number} ✅ **{entry.name}** · {entry.rarity.title()} · {entry.element.title()} · ×{entry.caught_count}{shiny}"
    if entry.seen:
        return f"{number} 👀 **{entry.name}** · seen, not caught yet"
    return f"{number} ❓ ???"


def dex_page(entries: list[DexEntry], page: int, per_page: int = DEX_PAGE_SIZE) -> tuple[list[DexEntry], int]:
    page = clamp_page(page, len(entries), per_page)
    return entries[page * per_page:(page + 1) * per_page], page


__all__ = [
    "COLLECTION_PAGE_SIZE", "DEX_PAGE_SIZE", "DexEntry", "OwnedRow", "SORTS", "clamp_page",
    "dex_page", "dex_summary", "format_dex_line", "format_owned_line", "list_owned", "load_dex",
    "page_count", "set_favorite",
]
