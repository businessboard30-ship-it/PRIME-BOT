"""My cats: search, filters and sorting (pure, no database, so it can never touch anyone's cats)."""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import List, Optional

from modules import cat_adopt
import logging

logger = logging.getLogger(__name__)

SORTS = {
    "rarest": "Rarest first",
    "newest": "Newest first",
    "oldest": "Oldest first",
    "name": "Name A-Z",
    "rank": "Highest rank",
}
DEFAULT_SORT = "rarest"
QUERY_MAX = 20


@dataclass(frozen=True)
class CatFilter:
    query: str = ""
    rarity: Optional[str] = None
    trait: Optional[str] = None
    sort: str = DEFAULT_SORT

    @property
    def active(self) -> int:
        """How many narrowing filters are on (sorting does not narrow)."""
        return sum(1 for v in (self.query, self.rarity, self.trait) if v)

    def with_(self, **kw) -> "CatFilter":
        return replace(self, **kw)


def clean_query(raw) -> str:
    return " ".join(str(raw or "").split())[:QUERY_MAX].lower()


def matches(cat, f: CatFilter) -> bool:
    if f.rarity and cat.rarity != f.rarity:
        return False
    if f.trait and cat.trait != f.trait:
        return False
    if f.query and f.query not in cat.name.lower() and f.query not in str(cat.id):
        return False
    return True


def _sort_key(f: CatFilter):
    if f.sort == "newest":
        return lambda c: (-_ts(c), -c.id)
    if f.sort == "oldest":
        return lambda c: (_ts(c), c.id)
    if f.sort == "name":
        return lambda c: (c.name.lower(), c.id)
    if f.sort == "rank":
        return lambda c: (-int(c.cat_rank or 0), -cat_adopt.RARITY_RANK.get(c.rarity, 0), c.id)
    return lambda c: (-cat_adopt.RARITY_RANK.get(c.rarity, 0), c.id)


def _ts(cat) -> float:
    at = getattr(cat, "adopted_at", None)
    try:
        return at.timestamp() if at else 0.0
    except Exception:
        logger.debug("swallowed exception in _ts", exc_info=True)
        return 0.0


def apply(cats: List, f: CatFilter) -> List:
    """Filtered, sorted copy. Never mutates or drops anything from the input list."""
    return sorted((c for c in cats if matches(c, f)), key=_sort_key(f))


def describe(f: CatFilter) -> str:
    parts = []
    if f.query:
        parts.append(f"name/ID contains “{f.query}”")
    if f.rarity:
        parts.append(f"{f.rarity}")
    if f.trait:
        parts.append(f"{f.trait.lower()}")
    parts.append(SORTS.get(f.sort, SORTS[DEFAULT_SORT]).lower())
    return " · ".join(parts)
