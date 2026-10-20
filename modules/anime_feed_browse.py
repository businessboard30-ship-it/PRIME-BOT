# path: modules/anime_feed_browse.py

"""
Anime feed D3: Search and Categories data. AniList only; adult titles are filtered at the provider AND re-checked here; image
URLs are https-only; nothing is invented when data is missing. Results are cached (search 10 minutes, categories 15) and shared
by everyone, bounded in size, with an old copy served if AniList fails. Every request goes through the shared limiter, where the
posting worker has priority; when the budget is used up the caller is told "busy" instead of queueing.
No migration: the category list lives in code, one line each (add more by adding a line).
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, NamedTuple, Optional, Tuple

from modules.anime_feed import SOURCE_NAME, _color, _https, clean_synopsis

logger = logging.getLogger(__name__)

MAX_RESULTS = 10
MAX_QUERY = 60
MIN_QUERY = 2
SEARCH_TTL = 10 * 60
CATEGORY_TTL = 15 * 60
CACHE_MAX = 200
SEARCH_COOLDOWN = 10.0
BROWSE_COOLDOWN = 2.0


class Category(NamedTuple):
    key: str                        # short and stable: it is part of a button id
    label: str
    emoji: str
    genres: Tuple[str, ...] = ()    # AniList genre names (any of)
    tags: Tuple[str, ...] = ()      # AniList tag names (any of)
    origin: str = ""                # countryOfOrigin ('CN' = donghua)
    fmt: str = ""                   # AniList format enum ('MOVIE')
    status: str = "RELEASING"       # AniList status enum; '' = any
    season_now: bool = False        # only the current season
    sort: str = "POPULARITY_DESC"


# One line per category. AniList's own genre/tag names, no mapping invented. Tag names ('Isekai', 'Cultivation') are AniList's;
# they were not checked live from the build sandbox (AniList is unreachable there), so an empty category says so plainly.
CATEGORIES: Tuple[Category, ...] = (
    Category("love", "Love", "💕", genres=("Romance",)),
    Category("scifi", "Sci-Fi", "🚀", genres=("Sci-Fi",)),
    Category("donghua", "Donghua", "🐉", origin="CN"),
    Category("action", "Action", "⚔️", genres=("Action",)),
    Category("fantasy", "Fantasy / Isekai", "🧙", genres=("Fantasy",)),
    Category("comedy", "Comedy", "😂", genres=("Comedy",)),
    Category("slice", "Slice of Life", "🍵", genres=("Slice of Life",)),
    Category("horror", "Horror / Thriller", "👻", genres=("Horror", "Thriller")),
    Category("sports", "Sports", "⚽", genres=("Sports",)),
    Category("mecha", "Mecha", "🤖", genres=("Mecha",)),
    Category("mystery", "Mystery", "🔍", genres=("Mystery",)),
    Category("cultivation", "Cultivation / Xianxia", "☯️", tags=("Cultivation",)),
    Category("season", "New this season", "🌸", status="", season_now=True),
    Category("movies", "Movies", "🎬", fmt="MOVIE", status="", sort="TRENDING_DESC"),
)
_BY_KEY = {c.key: c for c in CATEGORIES}


def get_category(key: str) -> Optional[Category]:
    return _BY_KEY.get(str(key))


@dataclass(frozen=True)
class Entry:
    anime_id: int
    title: str
    url: str
    synopsis: str = ""
    fmt: str = ""
    status: str = ""
    episodes: Optional[int] = None
    genres: Tuple[str, ...] = ()
    origin: str = ""
    banner: Optional[str] = None
    cover_xl: Optional[str] = None
    cover: Optional[str] = None
    color: Optional[int] = None
    next_episode: Optional[int] = None
    next_at: Optional[int] = None

    @property
    def image(self) -> Optional[str]:
        return self.banner or self.cover_xl or self.cover


# ── pure helpers ────────────────────────────────────────────────────────

def normalise_query(raw: Optional[str]) -> str:
    """Trimmed, single-spaced, lower-case, capped at MAX_QUERY: the cache key and what is sent to the provider."""
    return re.sub(r"\s+", " ", str(raw or "")).strip().lower()[:MAX_QUERY]


def valid_query(q: str) -> bool:
    return len(q) >= MIN_QUERY and any(ch.isalnum() for ch in q)


def season_of(now_ts: int) -> Tuple[str, int]:
    """AniList's season for a date: WINTER Jan-Mar, SPRING Apr-Jun, SUMMER Jul-Sep, FALL Oct-Dec."""
    d = datetime.fromtimestamp(int(now_ts), timezone.utc)
    return ("WINTER", "SPRING", "SUMMER", "FALL")[(d.month - 1) // 3], d.year


_MEDIA = """id siteUrl isAdult format status episodes genres countryOfOrigin bannerImage
        title { romaji english native }
        coverImage { large extraLarge color }
        description(asHtml: false)
        nextAiringEpisode { episode airingAt }"""


def search_query() -> str:
    return ("query ($q: String, $perPage: Int) { Page(page: 1, perPage: $perPage) { media(search: $q, type: ANIME, isAdult: false, sort: SEARCH_MATCH) { "
            + _MEDIA + " } } }")


def category_query(cat: Category, now_ts: int) -> Tuple[str, dict]:
    """(query, variables). Only constants from CATEGORIES are written into the query text; user-visible values go through variables."""
    decl, args, variables = ["$perPage: Int"], ["type: ANIME", "isAdult: false", f"sort: {cat.sort}"], {"perPage": MAX_RESULTS}
    if cat.genres:
        decl.append("$genres: [String]")
        args.append("genre_in: $genres")
        variables["genres"] = list(cat.genres)
    if cat.tags:
        decl.append("$tags: [String]")
        args.append("tag_in: $tags")
        variables["tags"] = list(cat.tags)
    if cat.origin:
        decl.append("$origin: CountryCode")
        args.append("countryOfOrigin: $origin")
        variables["origin"] = cat.origin
    if cat.fmt:
        args.append(f"format: {cat.fmt}")
    if cat.status:
        args.append(f"status: {cat.status}")
    if cat.season_now:
        season, year = season_of(now_ts)
        decl += ["$season: MediaSeason", "$year: Int"]
        args += ["season: $season", "seasonYear: $year"]
        variables.update(season=season, year=year)
    q = f"query ({', '.join(decl)}) {{ Page(page: 1, perPage: $perPage) {{ media({', '.join(args)}) {{ {_MEDIA} }} }} }}"
    return q, variables


def parse_entries(payload: dict) -> List[Entry]:
    """Provider JSON -> entries. Skips adult titles and rows without an id or a title; never raises on odd shapes."""
    out: List[Entry] = []
    try:
        rows = payload["data"]["Page"]["media"] or []
    except (KeyError, TypeError):
        return out
    seen = set()
    for m in rows:
        try:
            if not isinstance(m, dict) or m.get("isAdult") or not m.get("id") or int(m["id"]) in seen:
                continue
            t = m.get("title") or {}
            title = (t.get("english") or t.get("romaji") or t.get("native") or "").strip()
            if not title:
                continue
            seen.add(int(m["id"]))
            url = m.get("siteUrl") or ""
            if not url.startswith("https://anilist.co/"):
                url = f"https://anilist.co/anime/{int(m['id'])}"
            ci, nxt = m.get("coverImage") or {}, m.get("nextAiringEpisode") or {}
            out.append(Entry(int(m["id"]), title[:120], url, clean_synopsis(m.get("description")),
                             str(m.get("format") or "").replace("_", " ").title(), str(m.get("status") or "").replace("_", " ").title(),
                             int(m["episodes"]) if m.get("episodes") else None, tuple(str(g) for g in (m.get("genres") or [])[:3]),
                             str(m.get("countryOfOrigin") or "")[:2].upper(), _https(m.get("bannerImage")), _https(ci.get("extraLarge")),
                             _https(ci.get("large")), _color(ci.get("color")),
                             int(nxt["episode"]) if nxt.get("episode") else None, int(nxt["airingAt"]) if nxt.get("airingAt") else None))
        except (TypeError, ValueError, AttributeError, KeyError):
            continue
    return out[:MAX_RESULTS]


# ── provider (cached, bounded, rate limited) ────────────────────────────

_cache: "Dict[str, Tuple[float, List[Entry]]]" = {}
_locks: "Dict[str, asyncio.Lock]" = {}


def _trim() -> None:
    if len(_cache) >= CACHE_MAX:
        for k in sorted(_cache, key=lambda k: _cache[k][0])[:CACHE_MAX // 4]:     # drop the oldest quarter
            _cache.pop(k, None)
            _locks.pop(k, None)


async def _request(query: str, variables: dict) -> Optional[dict]:
    """One AniList request through the shared limiter. None when the budget is used up ('busy') or AniList can't be reached."""
    from anime_service import get_session
    from config import ANILIST_ENDPOINT
    from modules.anime_feed_limiter import anilist
    if not anilist.try_acquire():                                         # not priority: the posting worker keeps its reserve
        raise BusyError()
    session = await get_session()
    async with session.post(ANILIST_ENDPOINT, json={"query": query, "variables": variables}) as resp:
        if resp.status != 200:
            logger.warning("[anime-feed] AniList answered %s for a browse request", resp.status)
            return None
        return await resp.json()


class BusyError(Exception):
    pass


async def _cached(key: str, ttl: float, query: str, variables: dict) -> Tuple[List[Entry], str]:
    """(entries, state): state is 'ok', 'busy' (our request budget is used up, try again in a moment) or 'down' (AniList unreachable).
    A stale copy is served as 'ok' when a refresh fails, and simultaneous callers for one key share a single request."""
    now = time.time()
    hit = _cache.get(key)
    if hit and now - hit[0] < ttl:
        return list(hit[1]), "ok"
    lock = _locks.setdefault(key, asyncio.Lock())
    async with lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < ttl:
            return list(hit[1]), "ok"
        try:
            payload = await _request(query, variables)
        except BusyError:
            return (list(hit[1]), "ok") if hit else ([], "busy")
        except Exception:
            logger.warning("[anime-feed] browse request failed", exc_info=True)
            payload = None
        if payload is None:
            return (list(hit[1]), "ok") if hit else ([], "down")
        entries = parse_entries(payload)
        _trim()
        _cache[key] = (time.time(), entries)
        return list(entries), "ok"


async def search(raw_query: str) -> Tuple[List[Entry], str]:
    """Search by title. state: 'ok' | 'short' (query too short) | 'busy' | 'down'."""
    q = normalise_query(raw_query)
    if not valid_query(q):
        return [], "short"
    return await _cached(f"s:{q}", SEARCH_TTL, search_query(), {"q": q, "perPage": MAX_RESULTS})


async def category(key: str) -> Tuple[List[Entry], str]:
    """The titles of one curated category (popular first). state as in `search`; 'unknown' for a key that isn't in CATEGORIES."""
    cat = get_category(key)
    if cat is None:
        return [], "unknown"
    now = int(time.time())
    query, variables = category_query(cat, now)
    return await _cached(f"c:{cat.key}:{season_of(now)[0] if cat.season_now else ''}", CATEGORY_TTL, query, variables)
