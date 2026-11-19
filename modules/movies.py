"""Movies & series info (TMDB) and free public-domain films (Internet Archive). Legal sources only.

TMDB gives posters, plot, cast, rating, trailer and where a title streams legally per country. The Internet Archive's
feature_films collection is public-domain. This module never touches a piracy site and never downloads a film.
Pure parsing helpers are separate from the network calls so they can be tested without a network.
"""
from __future__ import annotations

import logging
from typing import List, Optional
from urllib.parse import quote

import aiohttp

import config

logger = logging.getLogger(__name__)

TMDB = "https://api.themoviedb.org/3"
IMG = "https://image.tmdb.org/t/p/w500"
ARCHIVE = "https://archive.org/advancedsearch.php"
ATTRIBUTION = "This product uses the TMDB API but is not endorsed or certified by TMDB."
REGIONS = {"US": "United States", "GB": "United Kingdom", "GH": "Ghana", "NG": "Nigeria", "ZA": "South Africa",
           "KE": "Kenya", "CA": "Canada", "AU": "Australia", "IN": "India", "DE": "Germany", "FR": "France",
           "BR": "Brazil", "AE": "UAE", "PH": "Philippines", "ID": "Indonesia"}
DEFAULT_REGION = "US"


def valid_region(code) -> str:
    c = str(code or "").upper()
    return c if c in REGIONS else DEFAULT_REGION


def _year(date: Optional[str]) -> str:
    return (date or "")[:4]


def parse_results(data: dict, limit: int = 5) -> List[dict]:
    """TMDB search/trending JSON -> up to `limit` movies or series (people and junk dropped)."""
    out = []
    for r in (data or {}).get("results", []) or []:
        kind = r.get("media_type") or ("movie" if r.get("title") else "tv")
        if kind not in ("movie", "tv") or not r.get("id"):
            continue
        out.append({"id": int(r["id"]), "type": kind, "title": str(r.get("title") or r.get("name") or "?"),
                    "year": _year(r.get("release_date") or r.get("first_air_date")),
                    "rating": round(float(r.get("vote_average") or 0), 1)})
        if len(out) >= limit:
            break
    return out


def parse_details(data: dict, kind: str, region: str) -> dict:
    """TMDB details (with credits, videos and watch/providers appended) -> what the embed shows."""
    d = data or {}
    region = valid_region(region)
    prov = ((d.get("watch/providers") or {}).get("results") or {}).get(region) or {}
    names = lambda key: [p.get("provider_name") for p in prov.get(key, []) if p.get("provider_name")][:6]
    trailer = next((v for v in (d.get("videos") or {}).get("results", [])
                    if v.get("site") == "YouTube" and v.get("type") == "Trailer" and v.get("key")), None)
    runtime = d.get("runtime") or (d.get("episode_run_time") or [None])[0]
    return {
        "id": d.get("id"), "type": kind,
        "title": str(d.get("title") or d.get("name") or "?"),
        "year": _year(d.get("release_date") or d.get("first_air_date")),
        "tagline": str(d.get("tagline") or ""),
        "overview": str(d.get("overview") or "No plot available.")[:700],
        "rating": round(float(d.get("vote_average") or 0), 1),
        "genres": [g.get("name") for g in d.get("genres", []) if g.get("name")][:4],
        "runtime": int(runtime) if runtime else None,
        "seasons": d.get("number_of_seasons"),
        "cast": [c.get("name") for c in (d.get("credits") or {}).get("cast", [])[:5] if c.get("name")],
        "poster": f"{IMG}{d['poster_path']}" if d.get("poster_path") else None,
        "trailer": f"https://www.youtube.com/watch?v={trailer['key']}" if trailer else None,
        "stream": names("flatrate"), "rent": names("rent"), "buy": names("buy"),
        "watch_link": prov.get("link"),
        "page": f"https://www.themoviedb.org/{kind}/{d.get('id')}",
    }


def parse_archive(data: dict, limit: int = 5) -> List[dict]:
    docs = ((data or {}).get("response") or {}).get("docs", []) or []
    return [{"title": str(x.get("title") or x["identifier"])[:100], "year": str(x.get("year") or ""),
             "url": f"https://archive.org/details/{quote(str(x['identifier']))}"}
            for x in docs if x.get("identifier")][:limit]


def _auth(headers: dict, params: dict) -> None:
    key = (config.TMDB_API_KEY or "").strip()
    if key.startswith("eyJ"):                 # TMDB "read access token" (JWT) goes in the header
        headers["Authorization"] = f"Bearer {key}"
    else:
        params["api_key"] = key


def configured() -> bool:
    return bool((config.TMDB_API_KEY or "").strip())


async def _get(session: aiohttp.ClientSession, url: str, params: Optional[dict] = None, tmdb: bool = False):
    headers, params = {}, dict(params or {})
    if tmdb:
        _auth(headers, params)
    async with session.get(url, params=params, headers=headers) as r:
        if r.status != 200:
            logger.warning("[movies] %s -> HTTP %s", url.split("?")[0], r.status)
            return None
        return await r.json(content_type=None)


async def search(query: str) -> List[dict]:
    if not configured():
        return []
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
            return parse_results(await _get(s, f"{TMDB}/search/multi", {"query": query[:100], "include_adult": "false"}, True))
    except Exception as e:
        logger.warning("[movies] search failed (%s)", type(e).__name__)
        return []


async def trending() -> List[dict]:
    if not configured():
        return []
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
            return parse_results(await _get(s, f"{TMDB}/trending/all/week", None, True), limit=8)
    except Exception as e:
        logger.warning("[movies] trending failed (%s)", type(e).__name__)
        return []


async def details(kind: str, tmdb_id: int, region: str) -> Optional[dict]:
    if not configured() or kind not in ("movie", "tv"):
        return None
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
            data = await _get(s, f"{TMDB}/{kind}/{int(tmdb_id)}", {"append_to_response": "credits,videos,watch/providers"}, True)
        return parse_details(data, kind, region) if data else None
    except Exception as e:
        logger.warning("[movies] details failed (%s)", type(e).__name__)
        return None


async def free_classics(query: str) -> List[dict]:
    """Public-domain films from the Internet Archive's feature_films collection."""
    q = "".join(ch for ch in query if ch.isalnum() or ch in " -'")[:60].strip()
    if not q:
        return []
    params = {"q": f'title:({q}) AND collection:feature_films AND mediatype:movies', "fl[]": ["identifier", "title", "year"],
              "rows": "5", "output": "json", "sort[]": "downloads desc"}
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
            return parse_archive(await _get(s, ARCHIVE, params))
    except Exception as e:
        logger.warning("[movies] archive failed (%s)", type(e).__name__)
        return []
