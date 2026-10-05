"""Dex extras (Phase 3 leftovers): per-rarity completion and the species info page.

Pure and read-only. Everything is derived from the ``DexEntry`` list the Dex screen
already loaded (scoped to the player and clone) plus the shipped species roster, so
there is no extra query. What a player may see follows what they have discovered:

* unknown species: not listed and not openable;
* seen but not caught: name, rarity and element only;
* caught: also habitat, base stats, their record and the evolution line, where an
  evolution target or source is named only if the player has already seen it.
"""

from __future__ import annotations

from dataclasses import dataclass

from modules.catch_collection import DexEntry
from modules.catch_game import RARITIES, STAT_NAMES
from modules.catch_species import all_species

UNKNOWN_NAME = "???"


@dataclass(frozen=True, slots=True)
class RarityProgress:
    rarity: str
    caught: int
    total: int


@dataclass(frozen=True, slots=True)
class SpeciesInfo:
    species_id: int
    name: str
    rarity: str
    element: str
    element2: str | None
    caught: bool
    caught_count: int
    shiny_caught: int
    habitat: str | None  # caught only
    stats: dict[str, int] | None  # caught only
    evolves_to: str | None  # name, or "???" if the target is undiscovered; None = final form
    evolve_level: int | None
    evolve_item: str | None
    evolves_from: str | None  # only when that earlier form has been seen
    evolution_known: bool  # caught: the evolution line is shown


def rarity_completion(entries: list[DexEntry]) -> list[RarityProgress]:
    """Caught / total per rarity, in rarity order; rarities with no species are skipped."""
    out = []
    for r in RARITIES:
        mine = [e for e in entries if e.rarity == r.key]
        if mine:
            out.append(RarityProgress(r.key, sum(1 for e in mine if e.caught), len(mine)))
    return out


def _discovered(entry: DexEntry | None) -> bool:
    return bool(entry and (entry.seen or entry.caught))


def species_info(entries: list[DexEntry], species_id: int) -> SpeciesInfo | None:
    """Info for one species, or None when it is not listed or not yet discovered."""
    by_id = {e.species_id: e for e in entries}
    entry = by_id.get(int(species_id))
    sp = all_species().get(int(species_id))
    if entry is None or sp is None or not _discovered(entry):
        return None
    caught = entry.caught
    stats = None
    habitat = None
    to_name = from_name = level = item = None
    if caught:
        habitat = str(sp.get("habitat") or "") or None
        base = list(sp.get("base_stats") or [])
        if len(base) == len(STAT_NAMES):
            stats = {name: int(value) for name, value in zip(STAT_NAMES, base)}
        target = sp.get("evolves_to")
        if target is not None:
            target_entry = by_id.get(int(target))
            target_sp = all_species().get(int(target)) or {}
            to_name = str(target_sp.get("name")) if _discovered(target_entry) and target_sp.get("name") else UNKNOWN_NAME
            level = sp.get("evolve_level")
            item = sp.get("evolve_item")
        for other_id, other in all_species().items():
            if other.get("evolves_to") == int(species_id) and _discovered(by_id.get(other_id)):
                from_name = str(other["name"])
                break
    return SpeciesInfo(
        int(species_id), entry.name, entry.rarity, entry.element, sp.get("element2"), caught,
        entry.caught_count, entry.shiny_caught, habitat, stats, to_name,
        int(level) if level else None, str(item) if item else None, from_name, caught,
    )


__all__ = ["RarityProgress", "SpeciesInfo", "UNKNOWN_NAME", "rarity_completion", "species_info"]
