# path: modules/anime_feed_spawn.py

"""Picks the anime that spawns in a server (the cat game's spawn idea, for anime).

A spawn is a random popular title from one of the curated AniList categories (modules/anime_feed_browse.py), filtered by what the
server posts (anime / donghua / both) and never one the server saw recently. Reuses the browse cache and the shared AniList rate
limiter, so a spawn adds no extra provider load beyond a cached category list. Pure picking logic is split out so it is testable.
"""

from __future__ import annotations

import logging
import random
from typing import List, Optional, Sequence, Tuple

from modules import anime_feed as af
from modules import anime_feed_browse as ab

logger = logging.getLogger(__name__)

DONGHUA_KEYS = ("donghua", "cultivation")
TRIES = 3                           # categories tried before giving up on one spawn


def category_keys(kinds: str) -> List[str]:
    kinds = af.clean_kinds(kinds)
    keys = [c.key for c in ab.CATEGORIES]
    if kinds == "donghua":
        return [k for k in keys if k in DONGHUA_KEYS]
    if kinds == "anime":
        return [k for k in keys if k not in DONGHUA_KEYS]
    return keys


def to_preview(e: ab.Entry) -> af.Preview:
    """A browse entry as a card. airing_at == 0 marks it as a spawn (card_text shows no airing line)."""
    return af.Preview(e.anime_id, e.title, 0, 0, e.synopsis, e.cover, e.url, e.genres, e.fmt, e.episodes, e.banner, e.cover_xl, e.color, e.origin)


def choose(entries: Sequence[ab.Entry], kinds: str, recent: Sequence[int], rng=random) -> Optional[ab.Entry]:
    """One entry the server has not seen lately that fits its kinds; None when nothing qualifies. Pure."""
    seen = set(recent)
    pool = [e for e in entries if e.anime_id not in seen and af.kind_allows(kinds, e.origin)]
    return rng.choice(pool) if pool else None


async def pick(kinds: str, recent: Sequence[int], rng=random) -> Tuple[Optional[af.Preview], bool]:
    """(preview, provider_ok). preview None with ok=True means nothing new fit; ok=False means AniList could not be reached."""
    keys = category_keys(kinds)
    rng.shuffle(keys)
    reached = False
    for key in keys[:TRIES]:
        entries, state = await ab.category(key)
        if state in ("busy", "down"):
            continue
        reached = True
        e = choose(entries, kinds, recent, rng)
        if e is not None:
            return to_preview(e), True
    return None, reached


async def sample(kinds: str, n: int = 8, rng=random) -> Tuple[List[af.Preview], bool]:
    """A few distinct spawn candidates for the setup Preview screen."""
    out: List[af.Preview] = []
    seen: set = set()
    keys = category_keys(kinds)
    rng.shuffle(keys)
    ok = False
    for key in keys[:TRIES + 1]:
        entries, state = await ab.category(key)
        if state in ("busy", "down"):
            continue
        ok = True
        pool = [e for e in entries if e.anime_id not in seen and af.kind_allows(kinds, e.origin)]
        rng.shuffle(pool)
        for e in pool[:3]:
            seen.add(e.anime_id)
            out.append(to_preview(e))
        if len(out) >= n:
            break
    return out[:n], ok
