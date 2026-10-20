# path: modules/anime_browse.py

"""
Anime feed, slice D3: Search and Categories. AniList only; adult titles are filtered in the query AND re-checked on every result;
https-only images; nothing invented when data is missing. Every request goes through the shared limiter in `anime_feed`
(`provider_post`, non-priority, so the posting worker always wins). Results are cached per normalised query / category and the
caches are bounded. The category list is plain data: adding one is a one-line edit, no migration.
"""

from __future__ import annotations

import base64
import logging
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from modules import anime_feed as af

logger = logging.getLogger(__name__)

SEARCH_TTL = 10 * 60
CATEGORY_TTL = 15 * 60
CACHE_MAX = 200
SEARCH_RESULTS = 10
CATEGORY_RESULTS = 12
QUERY_MAX_BYTES = 40            # keeps the query inside a 100-character custom_id once encoded, so Next/Prev survive restarts
POPULAR, TOP = 0, 1             # category sort: popularity (free) or score ("Top picks", an Anime+ extra)


@dataclass(frozen=True)
class Category:
    key: str                    # short, [a-z0-9_]: lives inside custom_ids, never reuse or rename one
    label: str
    emoji: str
    genres: Tuple[str, ...] = ()
    tags: Tuple[str, ...] = ()
    origin: str = ""            # AniList countryOfOrigin ('CN' = donghua)
    fmt: str = ""
    status_in: Tuple[str, ...] = ("RELEASING",)
    season: bool = False        # "New this season": the current season and year


# Curated list. Genre / tag names are AniList's own. Add a category by adding one line here.
CATEGORIES: Tuple[Category, ...] = (
    Category("love", "Love", "💞", genres=("Romance",)),
    Category("scifi", "Sci-Fi", "🛸", genres=("Sci-Fi",)),
    Category("donghua", "Donghua", "🐉", origin="CN"),
    Category("action", "Action", "🥷", genres=("Action",)),
    Category("fantasy", "Fantasy / Isekai", "🧙", genres=("Fantasy",)),
    Category("comedy", "Comedy", "🤹", genres=("Comedy",)),
    Category("slice", "Slice of Life", "🍵", genres=("Slice of Life",)),
    Category("horror", "Horror", "🦇", genres=("Horror", "Thriller")),
    Category("sports", "Sports", "🥍", genres=("Sports",)),
    Category("mecha", "Mecha", "🦾", genres=("Mecha",)),
    Category("mystery", "Mystery", "🕵️", genres=("Mystery",)),
    Category("xianxia", "Cultivation / Xianxia", "🏯", tags=("Xianxia", "Cultivation"), origin="CN"),
    Category("season", "New this season", "🌠", status_in=("RELEASING", "NOT_YET_RELEASED", "FINISHED"), season=True),
    Category("movies", "Movies", "🎞️", fmt="MOVIE", status_in=("RELEASING", "NOT_YET_RELEASED")),
)
_BY_KEY: Dict[str, Category] = {c.key: c for c in CATEGORIES}


def get_category(key: str) -> Optional[Category]:
    return _BY_KEY.get(str(key))


# ── pure helpers ────────────────────────────────────────────────────────

def normalize_query(raw: str) -> str:
    """Trim, collapse spaces, lower-case, and cut to QUERY_MAX_BYTES of UTF-8 without splitting a character."""
    q = " ".join(str(raw or "").split()).lower()
    out = q.encode("utf-8")[:QUERY_MAX_BYTES].decode("utf-8", "ignore")
    return out.strip()


def encode_query(q: str) -> str:
    return base64.urlsafe_b64encode(normalize_query(q).encode("utf-8")).decode("ascii")


def decode_query(token: str) -> str:
    try:
        return normalize_query(base64.urlsafe_b64decode(str(token).encode("ascii")).decode("utf-8"))
    except Exception:
        return ""


def current_season(now: Optional[float] = None) -> Tuple[str, int]:
    t = time.gmtime(now if now is not None else time.time())
    return ("WINTER", "SPRING", "SUMMER", "FALL")[(t.tm_mon - 1) // 3], t.tm_year


@dataclass(frozen=True)
class Entry:
    anime_id: int
    title: str
    url: str
    synopsis: str
    fmt: str = ""
    status: str = ""
    episodes: Optional[int] = None
    genres: Tuple[str, ...] = ()
    origin: str = ""
    banner: Optional[str] = None
    cover: Optional[str] = None
    color: Optional[int] = None
    next_episode: Optional[int] = None
    next_at: Optional[int] = None

    @property
    def image(self) -> Optional[str]:
        return self.banner or self.cover


def parse_media(payload: dict) -> List[Entry]:
    """Provider JSON -> entries. Adult, id-less or title-less items are dropped; odd shapes never raise."""
    out: List[Entry] = []
    try:
        media = payload["data"]["Page"]["media"] or []
    except (KeyError, TypeError):
        return out
    for m in media:
        try:
            if not m or m.get("isAdult") or not m.get("id"):
                continue
            t = m.get("title") or {}
            title = (t.get("english") or t.get("romaji") or t.get("native") or "").strip()
            if not title:
                continue
            aid = int(m["id"])
            url = m.get("siteUrl") or ""
            if not url.startswith("https://anilist.co/"):
                url = f"https://anilist.co/anime/{aid}"
            cov = m.get("coverImage") or {}
            nxt = m.get("nextAiringEpisode") or {}
            out.append(Entry(aid, title[:120], url, af.clean_synopsis(m.get("description")),
                             str(m.get("format") or "").replace("_", " ").title(), str(m.get("status") or "").replace("_", " ").title(),
                             int(m["episodes"]) if m.get("episodes") else None, tuple(str(g) for g in (m.get("genres") or [])[:4]),
                             str(m.get("countryOfOrigin") or "")[:2].upper(), af._https(m.get("bannerImage")),
                             af._https(cov.get("extraLarge")) or af._https(cov.get("large")), af._color(cov.get("color")),
                             int(nxt["episode"]) if nxt.get("episode") else None, int(nxt["airingAt"]) if nxt.get("airingAt") else None))
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
    return out


_QUERY = """
query ($search: String, $genre_in: [String], $tag_in: [String], $country: CountryCode, $format: MediaFormat,
       $status_in: [MediaStatus], $season: MediaSeason, $year: Int, $sort: [MediaSort], $per: Int) {
  Page(page: 1, perPage: $per) {
    media(type: ANIME, isAdult: false, search: $search, genre_in: $genre_in, tag_in: $tag_in, countryOfOrigin: $country,
          format: $format, status_in: $status_in, season: $season, seasonYear: $year, sort: $sort) {
      id siteUrl isAdult format status episodes genres countryOfOrigin bannerImage
      title { romaji english native }
      coverImage { large extraLarge color }
      description(asHtml: false)
      nextAiringEpisode { episode airingAt }
    }
  }
}
"""


def search_variables(q: str) -> dict:
    return {"search": q, "sort": ["SEARCH_MATCH"], "per": SEARCH_RESULTS}


def category_variables(c: Category, sort: int = POPULAR, now: Optional[float] = None) -> dict:
    v: dict = {"sort": ["SCORE_DESC" if sort == TOP else "POPULARITY_DESC"], "per": CATEGORY_RESULTS, "status_in": list(c.status_in)}
    if c.genres:
        v["genre_in"] = list(c.genres)
    if c.tags:
        v["tag_in"] = list(c.tags)
    if c.origin:
        v["country"] = c.origin
    if c.fmt:
        v["format"] = c.fmt
    if c.season:
        v["season"], v["year"] = current_season(now)
    return v


# ── cached, limited lookups ─────────────────────────────────────────────

_cache: "dict[tuple, Tuple[float, List[Entry]]]" = {}


def _remember(key: tuple, items: List[Entry]) -> None:
    if len(_cache) >= CACHE_MAX:
        for k in sorted(_cache, key=lambda k: _cache[k][0])[:CACHE_MAX // 4]:     # drop the oldest quarter
            _cache.pop(k, None)
    _cache[key] = (time.time(), items)


async def _lookup(key: tuple, variables: dict, ttl: int) -> Tuple[List[Entry], bool]:
    """(entries, ok). Fresh cache first; on a failure or a busy limiter an older copy is better than nothing; ok=False when there is neither."""
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return list(hit[1]), True
    payload = await af.provider_post(_QUERY, variables, priority=False)
    if payload is None:
        return (list(hit[1]), True) if hit else ([], False)
    items = parse_media(payload)
    _remember(key, items)
    return list(items), True


async def search(raw: str) -> Tuple[List[Entry], bool]:
    q = normalize_query(raw)
    if len(q) < 2:
        return [], True
    return await _lookup(("s", q), search_variables(q), SEARCH_TTL)


async def category(key: str, sort: int = POPULAR) -> Tuple[List[Entry], bool]:
    c = get_category(key)
    if c is None:
        return [], True
    sort = TOP if sort == TOP else POPULAR
    # "New this season" must not be served from last season's cache: the season is part of the key
    return await _lookup(("c", c.key, sort, current_season()), category_variables(c, sort), CATEGORY_TTL)
