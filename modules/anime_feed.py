# path: modules/anime_feed.py

"""
Anime release feed, slice A: data for previews and the per-server setup.

AniList (https://anilist.co, public GraphQL API) is the one provider. Airing times come straight from its schedule; when it
has none, nothing is invented. Adult titles are filtered out. Results are cached for 15 minutes and shared by every server
(one request serves all of them), with a timeout and no retry storm: on any failure the caller gets ok=False and says so.
Attribution: every card names AniList and links to the title's AniList page. This module only READS metadata and links out;
it hosts no episodes.
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

CACHE_TTL = 15 * 60
WINDOW_HOURS = 48
LOOKBACK_HOURS = 3                  # episodes that aired in the last 3 hours are still "just aired"; older ones are never posted
MAX_ITEMS = 12
SYNOPSIS_CHARS = 280
SOURCE_NAME = "AniList"

_QUERY = """
query ($from: Int, $to: Int, $perPage: Int) {
  Page(page: 1, perPage: $perPage) {
    airingSchedules(airingAt_greater: $from, airingAt_lesser: $to, sort: TIME) {
      airingAt
      episode
      media {
        id siteUrl isAdult format episodes genres countryOfOrigin bannerImage
        title { romaji english native }
        coverImage { large extraLarge color }
        description(asHtml: false)
      }
    }
  }
}
"""


@dataclass(frozen=True)
class Preview:
    anime_id: int
    title: str
    episode: int
    airing_at: int                  # unix seconds, straight from the provider
    synopsis: str
    cover: Optional[str]
    url: str
    genres: Tuple[str, ...] = ()
    fmt: str = ""
    episodes: Optional[int] = None
    banner: Optional[str] = None    # wide banner from the provider (https only)
    cover_xl: Optional[str] = None  # big cover, the fallback when there is no banner
    color: Optional[int] = None     # the cover's dominant colour, used as the card accent
    origin: str = ""                # 'JP' / 'CN' / 'KR' ... (countryOfOrigin)

    @property
    def image(self) -> Optional[str]:
        """The one big picture for the card: wide banner, else the big cover, else the normal cover, else nothing."""
        return self.banner or self.cover_xl or self.cover


# ── pure helpers ────────────────────────────────────────────────────────

def clean_synopsis(raw: Optional[str], limit: int = SYNOPSIS_CHARS) -> str:
    """Plain text, tags and source notes removed, cut at a word boundary with an ellipsis. Empty when there is none."""
    if not raw:
        return ""
    text = html.unescape(re.sub(r"<[^>]+>", " ", str(raw)))
    text = re.sub(r"\(Source:[^)]*\)|\[Written by[^\]]*\]", " ", text, flags=re.I)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0].rstrip(",;:.- ")
    return cut + "…"


def parse_airing(payload: dict) -> List[Preview]:
    """Provider JSON -> previews. Skips adult titles and rows missing an id, a time or a title; never raises on odd shapes."""
    out: List[Preview] = []
    try:
        rows = payload["data"]["Page"]["airingSchedules"] or []
    except (KeyError, TypeError):
        return out
    seen = set()
    for r in rows:
        try:
            m = r.get("media") or {}
            if m.get("isAdult") or not m.get("id") or not r.get("airingAt") or not r.get("episode"):
                continue
            t = m.get("title") or {}
            title = (t.get("english") or t.get("romaji") or "").strip()
            if not title:
                continue
            key = (int(m["id"]), int(r["episode"]))
            if key in seen:
                continue
            seen.add(key)
            url = m.get("siteUrl") or f"https://anilist.co/anime/{int(m['id'])}"
            if not url.startswith("https://anilist.co/"):
                url = f"https://anilist.co/anime/{int(m['id'])}"
            ci = m.get("coverImage") or {}
            out.append(Preview(int(m["id"]), title[:120], int(r["episode"]), int(r["airingAt"]), clean_synopsis(m.get("description")),
                               _https(ci.get("large")), url,
                               tuple(str(g) for g in (m.get("genres") or [])[:3]), str(m.get("format") or "").replace("_", " ").title(),
                               int(m["episodes"]) if m.get("episodes") else None,
                               _https(m.get("bannerImage")), _https(ci.get("extraLarge")), _color(ci.get("color")),
                               str(m.get("countryOfOrigin") or "")[:2].upper()))
        except (TypeError, ValueError, AttributeError):
            continue
    return out[:MAX_ITEMS]


ORIGIN_LABELS = {"JP": "Anime", "CN": "Donghua", "KR": "Korean animation", "TW": "Taiwanese animation"}


def origin_label(p: Preview) -> str:
    """'Donghua' for Chinese animation, 'Anime' for Japanese, empty when the provider says nothing (never guessed)."""
    return ORIGIN_LABELS.get((p.origin or "").upper(), "")


def _https(url) -> Optional[str]:
    return url if isinstance(url, str) and url.startswith("https://") and " " not in url and len(url) <= 500 else None


def _color(raw) -> Optional[int]:
    """AniList sends '#e4a15d'; anything else is ignored."""
    m = re.fullmatch(r"#?([0-9a-fA-F]{6})", str(raw or "").strip())
    return int(m.group(1), 16) if m else None


def episode_label(p: Preview) -> str:
    total = f" of {p.episodes}" if p.episodes else ""
    return f"Episode {p.episode}{total}"


def upcoming(items: List[Preview], now: int) -> List[Preview]:
    return [p for p in items if p.airing_at > now]


# ── provider (cached, shared across servers) ────────────────────────────

_cache: Tuple[float, List[Preview]] = (0.0, [])
_lock: Optional[asyncio.Lock] = None


async def _fetch(now: int) -> Optional[dict]:
    from anime_service import get_session
    from config import ANILIST_ENDPOINT
    session = await get_session()
    body = {"query": _QUERY, "variables": {"from": now - LOOKBACK_HOURS * 3600, "to": now + WINDOW_HOURS * 3600, "perPage": 40}}
    async with session.post(ANILIST_ENDPOINT, json=body) as resp:
        if resp.status != 200:
            logger.warning("[anime-feed] AniList answered %s", resp.status)
            return None
        return await resp.json()


async def airing_soon() -> Tuple[List[Preview], bool]:
    """Titles that aired in the last 3 hours or air in the next 48, soonest first (use `upcoming` for only the future). (items, ok). ok=False means the provider could not be reached;
    an empty list with ok=True means nothing is scheduled. Fresh cache is reused; simultaneous callers share one request."""
    global _cache, _lock
    if _lock is None:
        _lock = asyncio.Lock()
    stamp, items = _cache
    if items and time.time() - stamp < CACHE_TTL:
        return list(items), True
    async with _lock:
        stamp, items = _cache
        if items and time.time() - stamp < CACHE_TTL:
            return list(items), True
        try:
            payload = await _fetch(int(time.time()))
        except Exception:
            logger.warning("[anime-feed] couldn't reach AniList", exc_info=True)
            payload = None
        if payload is None:
            return (list(items), True) if items else ([], False)      # an old list beats nothing
        parsed = parse_airing(payload)
        _cache = (time.time(), parsed)
        return list(parsed), True


# ── full details for "Read more" (one title, cached, bounded) ───────────

DETAIL_TTL = 6 * 3600
DETAIL_MAX = 300
READ_MORE_CHARS = 3500
_DETAIL_QUERY = """
query ($id: Int) {
  Media(id: $id, type: ANIME) {
    id siteUrl isAdult format status episodes genres countryOfOrigin
    title { romaji english native }
    description(asHtml: false)
    nextAiringEpisode { episode airingAt }
  }
}
"""
_details: "dict[int, Tuple[float, Optional[Detail]]]" = {}


@dataclass(frozen=True)
class Detail:
    anime_id: int
    title: str
    native: str
    url: str
    synopsis: str                   # the FULL text (cut only at READ_MORE_CHARS), not the short card version
    fmt: str
    status: str
    episodes: Optional[int]
    genres: Tuple[str, ...]
    origin: str
    next_episode: Optional[int]
    next_at: Optional[int]


def parse_detail(payload: dict) -> Optional[Detail]:
    """Provider JSON -> Detail. None for missing data or adult titles; never raises on odd shapes."""
    try:
        m = payload["data"]["Media"]
        if not m or m.get("isAdult") or not m.get("id"):
            return None
        t = m.get("title") or {}
        title = (t.get("english") or t.get("romaji") or t.get("native") or "").strip()
        if not title:
            return None
        url = m.get("siteUrl") or f"https://anilist.co/anime/{int(m['id'])}"
        if not url.startswith("https://anilist.co/"):
            url = f"https://anilist.co/anime/{int(m['id'])}"
        nxt = m.get("nextAiringEpisode") or {}
        return Detail(int(m["id"]), title[:120], str(t.get("native") or "")[:120], url,
                      clean_synopsis(m.get("description"), READ_MORE_CHARS),
                      str(m.get("format") or "").replace("_", " ").title(), str(m.get("status") or "").replace("_", " ").title(),
                      int(m["episodes"]) if m.get("episodes") else None,
                      tuple(str(g) for g in (m.get("genres") or [])[:6]), str(m.get("countryOfOrigin") or "")[:2].upper(),
                      int(nxt["episode"]) if nxt.get("episode") else None, int(nxt["airingAt"]) if nxt.get("airingAt") else None)
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


async def get_detail(anime_id: int) -> Tuple[Optional[Detail], bool]:
    """(detail, ok). ok=False means the provider could not be reached (an older cached copy is still returned when there is one).
    ok=True with None means there is nothing to show (unknown or adult title). Cached 6 hours, at most DETAIL_MAX titles."""
    aid, now = int(anime_id), time.time()
    hit = _details.get(aid)
    if hit and now - hit[0] < DETAIL_TTL:
        return hit[1], True
    try:
        from anime_service import get_session
        from config import ANILIST_ENDPOINT
        session = await get_session()
        async with session.post(ANILIST_ENDPOINT, json={"query": _DETAIL_QUERY, "variables": {"id": aid}}) as resp:
            if resp.status != 200:
                logger.warning("[anime-feed] AniList answered %s for a detail", resp.status)
                return (hit[1], True) if hit and hit[1] else (None, False)
            payload = await resp.json()
    except Exception:
        logger.warning("[anime-feed] couldn't fetch a detail", exc_info=True)
        return (hit[1], True) if hit and hit[1] else (None, False)
    detail = parse_detail(payload)
    if len(_details) >= DETAIL_MAX:
        for k in sorted(_details, key=lambda k: _details[k][0])[:DETAIL_MAX // 4]:   # drop the oldest quarter
            _details.pop(k, None)
    _details[aid] = (now, detail)
    return detail, True


_cooldowns: "dict[tuple, float]" = {}


def cooldown_left(user_id: int, key: str, seconds: float) -> float:
    """Seconds the person must still wait for this action (0 when allowed, and then the clock starts). Bounded memory."""
    now, k = time.monotonic(), (int(user_id), key)
    until = _cooldowns.get(k, 0.0)
    if until > now:
        return until - now
    if len(_cooldowns) > 5000:
        for old in [x for x, t in _cooldowns.items() if t <= now]:
            _cooldowns.pop(old, None)
    _cooldowns[k] = now + seconds
    return 0.0


# ── per-server setup ────────────────────────────────────────────────────

@dataclass
class FeedConfig:
    guild_id: int
    clone_id: Optional[int] = None
    enabled: bool = False
    channel_id: Optional[int] = None
    enabled_at: Optional[datetime] = None
    ping_role_id: Optional[int] = None


def _key(clone_id) -> int:
    return -1 if clone_id is None else int(clone_id)


async def _pool():
    from database import get_pool
    return await get_pool()


async def get_config(guild_id: int, clone_id=None) -> FeedConfig:
    try:
        pool = await _pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT enabled, channel_id, enabled_at, ping_role_id FROM anime_feed_config WHERE guild_id = $1 AND clone_key = $2",
                                      int(guild_id), _key(clone_id))
    except Exception:
        logger.exception("[anime-feed] couldn't read the setup for guild %s", guild_id)
        row = None
    return _cfg(guild_id, clone_id, row) if row else FeedConfig(guild_id, clone_id)


def _cfg(guild_id, clone_id, row) -> FeedConfig:
    return FeedConfig(int(guild_id), clone_id, bool(row["enabled"]), int(row["channel_id"]) if row["channel_id"] else None, row["enabled_at"],
                      int(row["ping_role_id"]) if row["ping_role_id"] else None)


async def enabled_guilds(clone_id=None) -> List[FeedConfig]:
    """Every server of this bot with the feed on and a channel picked. Raises on database errors (the worker backs off)."""
    pool = await _pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT guild_id, enabled, channel_id, enabled_at, ping_role_id FROM anime_feed_config "
                                "WHERE clone_key = $1 AND enabled AND channel_id IS NOT NULL", _key(clone_id))
    return [_cfg(r["guild_id"], clone_id, r) for r in rows]


async def claim(guild_id: int, clone_id, anime_id: int, episode: int) -> bool:
    """True for exactly one caller per (server, episode): the duplicate guard. Claim BEFORE sending."""
    pool = await _pool()
    async with pool.acquire() as conn:
        got = await conn.fetchval("INSERT INTO anime_feed_posted (guild_id, clone_key, anime_id, episode) VALUES ($1, $2, $3, $4) "
                                  "ON CONFLICT DO NOTHING RETURNING 1", int(guild_id), _key(clone_id), int(anime_id), int(episode))
    return got is not None


async def release(guild_id: int, clone_id, anime_id: int, episode: int) -> None:
    """Give a claim back when the send failed for a reason worth retrying."""
    pool = await _pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM anime_feed_posted WHERE guild_id = $1 AND clone_key = $2 AND anime_id = $3 AND episode = $4",
                           int(guild_id), _key(clone_id), int(anime_id), int(episode))


async def disable(guild_id: int, clone_id=None) -> None:
    pool = await _pool()
    async with pool.acquire() as conn:
        await conn.execute("UPDATE anime_feed_config SET enabled = FALSE, enabled_at = NULL, updated_at = NOW() WHERE guild_id = $1 AND clone_key = $2",
                           int(guild_id), _key(clone_id))


async def save_config(cfg: FeedConfig, updated_by: Optional[int] = None) -> None:
    """Saves the channel and switch. `enabled_at` is stamped when the feed turns on (and cleared when it turns off), so only
    episodes that air AFTER that moment are ever posted. Wakes the worker so a change takes effect without waiting."""
    on = bool(cfg.enabled and cfg.channel_id)
    pool = await _pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO anime_feed_config (guild_id, clone_key, enabled, channel_id, updated_by, updated_at, enabled_at, ping_role_id) "
            "VALUES ($1, $2, $3, $4, $5, NOW(), CASE WHEN $3 THEN NOW() END, $6) ON CONFLICT (guild_id, clone_key) DO UPDATE SET "
            "enabled = EXCLUDED.enabled, channel_id = EXCLUDED.channel_id, updated_by = EXCLUDED.updated_by, updated_at = NOW(), "
            "ping_role_id = EXCLUDED.ping_role_id, "
            "enabled_at = CASE WHEN NOT EXCLUDED.enabled THEN NULL WHEN anime_feed_config.enabled THEN anime_feed_config.enabled_at ELSE NOW() END",
            int(cfg.guild_id), _key(cfg.clone_id), on, cfg.channel_id, updated_by, cfg.ping_role_id)
    try:
        from modules import anime_feed_worker
        anime_feed_worker.wake_all()
    except Exception:
        logger.debug("[anime-feed] couldn't wake the worker", exc_info=True)


# ── personal follows ────────────────────────────────────────────────────

FREE_FOLLOW_LIMIT = 5               # followed anime per person on the free plan (premium limits come with the plans slice)
MAX_FOLLOWER_PINGS = 10             # people mentioned in one post; the rest are counted, not mentioned


async def toggle_follow(user_id: int, anime_id: int, title: str, limit: int = FREE_FOLLOW_LIMIT) -> Tuple[str, int]:
    """Follow or unfollow. Returns (state, count): 'followed' | 'unfollowed' | 'limit', and how many they follow now.
    One transaction under a per-person advisory lock, so two quick taps can't slip past the limit."""
    pool = await _pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock($1)", int(user_id))
            if await conn.fetchval("DELETE FROM anime_follows WHERE user_id = $1 AND anime_id = $2 RETURNING 1", int(user_id), int(anime_id)):
                n = int(await conn.fetchval("SELECT COUNT(*) FROM anime_follows WHERE user_id = $1", int(user_id)) or 0)
                return "unfollowed", n
            n = int(await conn.fetchval("SELECT COUNT(*) FROM anime_follows WHERE user_id = $1", int(user_id)) or 0)
            if n >= limit:
                return "limit", n
            await conn.execute("INSERT INTO anime_follows (user_id, anime_id, title) VALUES ($1, $2, $3)", int(user_id), int(anime_id), str(title)[:120])
            return "followed", n + 1


async def followers_of(anime_id: int, limit: int = 500) -> List[int]:
    """People following this anime, earliest first. Never raises (empty on error: the post still goes out)."""
    try:
        pool = await _pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT user_id FROM anime_follows WHERE anime_id = $1 ORDER BY followed_at LIMIT $2", int(anime_id), int(limit))
        return [int(r["user_id"]) for r in rows]
    except Exception:
        logger.debug("[anime-feed] couldn't read followers", exc_info=True)
        return []


def ping_line(role_id: Optional[int], user_ids: List[int]) -> Tuple[str, List[int]]:
    """The mention line for a post and the user ids actually mentioned (at most MAX_FOLLOWER_PINGS); '' when nobody."""
    shown = list(dict.fromkeys(int(u) for u in user_ids))[:MAX_FOLLOWER_PINGS]
    extra = len(set(user_ids)) - len(shown)
    parts = ([f"<@&{int(role_id)}>"] if role_id else []) + [f"<@{u}>" for u in shown]
    if extra > 0:
        parts.append(f"and {extra} more following")
    return " ".join(parts), shown
