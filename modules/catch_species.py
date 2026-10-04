"""Species roster loader (P1-05).

data/catch/species.json is the source of truth. On boot ``sync_to_db``
validates it and upserts every species into catch_species. Species removed
from the file are disabled, never deleted, because catch_owned rows still
reference them. A content hash stored in admin_config skips the write when
the file has not changed, so twelve clones booting together cost one read.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

from modules import catch_db
from modules.catch_game import (
    ELEMENTS,
    EXCLUSIVE_SOURCES,
    HABITATS,
    RARITY_BY_KEY,
    STAT_NAMES,
)

logger = logging.getLogger(__name__)

SPECIES_PATH = Path(__file__).resolve().parent.parent / "data" / "catch" / "species.json"
HASH_CONFIG_KEY = "catch_species_hash"

_cache: dict[int, dict] | None = None


def load_file(path: Path = SPECIES_PATH) -> list[dict]:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)["species"]


def validate(species: list[dict]) -> list[str]:
    """Return every problem found (empty list = valid)."""
    problems: list[str] = []
    ids: set[int] = set()
    seen_slugs: set[str] = set()
    seen_names: set[str] = set()
    for sp in species:
        sid = sp.get("id")
        label = f"species {sid!r}"
        if not isinstance(sid, int) or not 1 <= sid <= 32767:
            problems.append(f"{label}: id must be an int 1..32767")
        elif sid in ids:
            problems.append(f"{label}: duplicate id")
        ids.add(sid)
        slug, name = sp.get("slug", ""), sp.get("name", "")
        if not slug or slug != slug.lower() or not slug.replace("_", "").isalnum():
            problems.append(f"{label}: slug must be lowercase letters/digits/_")
        if slug in seen_slugs:
            problems.append(f"{label}: duplicate slug {slug}")
        seen_slugs.add(slug)
        if not name or len(name) > 32:
            problems.append(f"{label}: name must be 1..32 chars")
        if name.lower() in seen_names:
            problems.append(f"{label}: duplicate name {name}")
        seen_names.add(name.lower())
        if sp.get("rarity") not in RARITY_BY_KEY:
            problems.append(f"{label}: unknown rarity {sp.get('rarity')!r}")
        if sp.get("element") not in ELEMENTS:
            problems.append(f"{label}: unknown element {sp.get('element')!r}")
        el2 = sp.get("element2")
        if el2 is not None and (el2 not in ELEMENTS or el2 == sp.get("element")):
            problems.append(f"{label}: element2 must be a different known element or null")
        if sp.get("habitat") not in HABITATS:
            problems.append(f"{label}: unknown habitat {sp.get('habitat')!r}")
        stats = sp.get("base_stats")
        if (
            not isinstance(stats, list)
            or len(stats) != len(STAT_NAMES)
            or not all(isinstance(s, int) and 1 <= s <= 255 for s in stats)
        ):
            problems.append(f"{label}: base_stats must be {len(STAT_NAMES)} ints 1..255")
        exclusive = sp.get("exclusive_to")
        if exclusive is not None and exclusive not in EXCLUSIVE_SOURCES:
            problems.append(f"{label}: unknown exclusive_to {exclusive!r}")
        if not isinstance(sp.get("spawn_weight", 0), int) or sp.get("spawn_weight", 0) < 0:
            problems.append(f"{label}: spawn_weight must be a non-negative int")
        mod = sp.get("catch_rate_mod", 1.0)
        if not isinstance(mod, (int, float)) or not 0.1 <= mod <= 3.0:
            problems.append(f"{label}: catch_rate_mod must be 0.1..3.0")

    by_id = {sp.get("id"): sp for sp in species}
    for sp in species:
        target = sp.get("evolves_to")
        if target is None:
            continue
        if target not in by_id:
            problems.append(f"species {sp.get('id')}: evolves_to {target} does not exist")
        elif target == sp.get("id"):
            problems.append(f"species {sp.get('id')}: cannot evolve into itself")
        if not sp.get("evolve_level") and not sp.get("evolve_item"):
            problems.append(f"species {sp.get('id')}: evolution needs evolve_level or evolve_item")
    for sp in species:
        hops, cur = 0, sp
        while cur and cur.get("evolves_to") is not None and hops <= len(species):
            cur = by_id.get(cur["evolves_to"])
            hops += 1
        if hops > len(species):
            problems.append(f"species {sp.get('id')}: evolution chain loops")
    return problems


def _file_hash(path: Path = SPECIES_PATH) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


async def sync_to_db(conn=None, *, path: Path = SPECIES_PATH, force: bool = False) -> int:
    """Validate and upsert the roster. Returns rows written (0 if unchanged)."""
    species = load_file(path)
    problems = validate(species)
    if problems:
        raise ValueError("species.json is invalid: " + "; ".join(problems[:10]))
    digest = _file_hash(path)
    async with catch_db.transaction(conn) as c:
        if not force:
            stored = await c.fetchval("SELECT value FROM admin_config WHERE key = $1", HASH_CONFIG_KEY)
            if stored == digest:
                return 0
        rows = [
            (
                sp["id"], sp["slug"], sp["name"], RARITY_BY_KEY[sp["rarity"]].code,
                sp["element"], sp.get("element2"), sp["habitat"], sp["base_stats"],
                sp.get("evolves_to"), sp.get("evolve_level"), sp.get("evolve_item"),
                float(sp.get("catch_rate_mod", 1.0)), int(sp.get("spawn_weight", 100)),
                sp.get("exclusive_to"), sp.get("exclusive_to") == "egg" or bool(sp.get("egg_pool", False)),
                bool(sp.get("enabled", True)),
            )
            for sp in species
        ]
        await c.executemany(
            """
            INSERT INTO catch_species (id, slug, name, rarity, element, element2, habitat,
                base_stats, evolves_to, evolve_level, evolve_item, catch_rate_mod,
                spawn_weight, exclusive_to, egg_pool, enabled, updated_at)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16, now())
            ON CONFLICT (id) DO UPDATE SET
                slug = EXCLUDED.slug, name = EXCLUDED.name, rarity = EXCLUDED.rarity,
                element = EXCLUDED.element, element2 = EXCLUDED.element2,
                habitat = EXCLUDED.habitat, base_stats = EXCLUDED.base_stats,
                evolves_to = EXCLUDED.evolves_to, evolve_level = EXCLUDED.evolve_level,
                evolve_item = EXCLUDED.evolve_item, catch_rate_mod = EXCLUDED.catch_rate_mod,
                spawn_weight = EXCLUDED.spawn_weight, exclusive_to = EXCLUDED.exclusive_to,
                egg_pool = EXCLUDED.egg_pool,
                enabled = EXCLUDED.enabled, updated_at = now()
            """,
            rows,
        )
        await c.execute(
            "UPDATE catch_species SET enabled = FALSE, updated_at = now() "
            "WHERE NOT (id = ANY($1::smallint[])) AND enabled",
            [sp["id"] for sp in species],
        )
        await c.execute(
            """
            INSERT INTO admin_config (key, value, updated_at) VALUES ($1, $2, now())
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
            """,
            HASH_CONFIG_KEY,
            digest,
        )
    invalidate_cache()
    logger.info("catch_species synced: %d species", len(rows))
    return len(rows)


def invalidate_cache() -> None:
    global _cache
    _cache = None


def all_species() -> dict[int, dict]:
    """In-memory roster from the file (read-only, cached)."""
    global _cache
    if _cache is None:
        _cache = {sp["id"]: sp for sp in load_file()}
    return _cache


def get(species_id: int) -> dict | None:
    return all_species().get(species_id)
