# path: modules/cat_pool.py

"""
The shared cat picture pool, used by the main bot AND every clone.

Each good picture (it passed modules/cat_quality.py) is saved once to the image-hosting channel, the same default
channel welcome backgrounds use, and a row in `cat_image_pool` remembers where. Any bot can then post it, so bot B
reuses a picture bot C already fetched and the third-party cat APIs are called far less.

Discord attachment links expire, so only (channel_id, message_id) is stored; a fresh link is read from the hosted
message when needed and remembered in memory for a few hours.
"""

from __future__ import annotations

import hashlib
import io
import logging
import os
import random
import time
from typing import Dict, List, Optional, Tuple

import discord

from modules.cat_quality import Quality

logger = logging.getLogger(__name__)

URL_TTL_SECONDS = 6 * 3600
# A server is not shown the same pooled picture again within this many days (while it has unseen ones left).
SEEN_DAYS = int(os.getenv("CAT_SEEN_DAYS", "14") or 14)
_url_cache: Dict[int, Tuple[float, str]] = {}


class PooledUrl(str):
    """A picture link that remembers which pool row it came from (needed to adopt that cat)."""
    pool_id: Optional[int] = None

    def __new__(cls, url: str, pool_id: Optional[int] = None):
        obj = super().__new__(cls, url)
        obj.pool_id = pool_id
        return obj
_EXT = {"JPEG": "jpg", "PNG": "png", "WEBP": "webp"}


async def _pool():
    from database import get_pool
    return await get_pool()


async def pool_count() -> int:
    pool = await _pool()
    async with pool.acquire() as conn:
        return int(await conn.fetchval("SELECT COUNT(*) FROM cat_image_pool") or 0)


async def known_source_ids(source: str, ids: List[str]) -> set:
    ids = [str(i) for i in ids if i]
    if not ids:
        return set()
    pool = await _pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT source_id FROM cat_image_pool WHERE source = $1 AND source_id = ANY($2::text[])", source, ids)
    return {r["source_id"] for r in rows}


async def _forget(row_id: int) -> None:
    _url_cache.pop(row_id, None)
    pool = await _pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM cat_image_pool WHERE id = $1", row_id)
        try:
            await conn.execute("DELETE FROM cat_pool_seen WHERE pool_id = $1", row_id)
        except Exception:
            logger.debug("[cat-pool] couldn't clear seen rows", exc_info=True)


async def _channel(bot, channel_id: int):
    return bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)


_PICK_SQL = (
    "SELECT p.id, p.channel_id, p.message_id FROM cat_image_pool p "
    "LEFT JOIN cat_pool_seen s ON s.guild_id = $1 AND s.pool_id = p.id "
    "CROSS JOIN LATERAL (SELECT (s.seen_at IS NOT NULL AND s.seen_at > NOW() - make_interval(days => $2)) AS recent) r "
    "{where}"
    "ORDER BY r.recent ASC, CASE WHEN r.recent THEN s.seen_at END ASC NULLS FIRST, "
    "EXISTS (SELECT 1 FROM cat_adoptions a WHERE a.pool_id = p.id) ASC, p.uses ASC, random() LIMIT 1")
_PICK_PLAIN_SQL = (
    "SELECT p.id, p.channel_id, p.message_id FROM cat_image_pool p "
    "ORDER BY EXISTS (SELECT 1 FROM cat_adoptions a WHERE a.pool_id = p.id) ASC, p.uses ASC, random() LIMIT 1")


async def _row(conn, guild_id: Optional[int], unseen_only: bool):
    """The next picture to show. With a server: pictures it hasn't seen in SEEN_DAYS come first (and with
    `unseen_only` ONLY those); if it has seen everything, the one it saw longest ago. Without a server (or if the
    seen-table isn't there yet) it is the old global least-used-first pick."""
    if guild_id is not None:
        try:
            where = "WHERE NOT r.recent " if unseen_only else ""
            return await conn.fetchrow(_PICK_SQL.format(where=where), int(guild_id), SEEN_DAYS)
        except Exception:
            logger.warning("[cat-pool] seen-tracking query failed, picking without it", exc_info=True)
            if unseen_only:
                return None
    return await conn.fetchrow(_PICK_PLAIN_SQL)


async def mark_seen(guild_id: Optional[int], pool_id: Optional[int]) -> None:
    """Remember that this server was shown this pooled picture. Never raises (a cat still gets posted)."""
    if guild_id is None or not pool_id:
        return
    try:
        pool = await _pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO cat_pool_seen (guild_id, pool_id) VALUES ($1, $2) "
                "ON CONFLICT (guild_id, pool_id) DO UPDATE SET seen_at = NOW()", int(guild_id), int(pool_id))
            if random.randrange(200) == 0:                       # housekeeping: rows past the window mean nothing
                await conn.execute("DELETE FROM cat_pool_seen WHERE seen_at < NOW() - make_interval(days => $1)",
                                   SEEN_DAYS)
    except Exception:
        logger.debug("[cat-pool] couldn't record the seen picture", exc_info=True)


async def pick(bot, guild_id: Optional[int] = None, unseen_only: bool = False) -> Optional[str]:
    """A fresh link to one pooled picture, or None.

    Without `guild_id`: least used first, random among ties (the original behaviour).
    With `guild_id`: a picture that server hasn't seen lately comes first and is recorded as seen. With
    `unseen_only` it returns None when the server has seen them all, so the caller can fetch new ones instead."""
    for _ in range(3):
        pool = await _pool()
        async with pool.acquire() as conn:
            row = await _row(conn, guild_id, unseen_only)
            if row is None:
                return None
            cached = _url_cache.get(row["id"])
            url = cached[1] if cached and cached[0] > time.monotonic() else None
            if url is None:
                try:
                    ch = await _channel(bot, int(row["channel_id"]))
                    msg = await ch.fetch_message(int(row["message_id"]))
                    url = msg.attachments[0].url if msg.attachments else None
                except discord.NotFound:
                    url = None
                except (discord.Forbidden, discord.HTTPException, AttributeError):
                    logger.warning("[cat-pool] this bot can't read the hosting channel right now")
                    return None
                if url is None:
                    await _forget(row["id"])           # the hosted picture is gone: drop the row, try another
                    continue
                _url_cache[row["id"]] = (time.monotonic() + URL_TTL_SECONDS, url)
                if len(_url_cache) > 2000:
                    _url_cache.clear()
            await conn.execute("UPDATE cat_image_pool SET uses = uses + 1 WHERE id = $1", row["id"])
        await mark_seen(guild_id, int(row["id"]))
        return PooledUrl(url, int(row["id"]))
    return None


async def store(bot, data: bytes, q: Quality, source: str, source_id: Optional[str]) -> Optional[str]:
    """Save one good picture to the hosting channel + pool. Returns its link, or None (duplicate / no channel)."""
    from modules import image_host
    sha = hashlib.sha256(data).hexdigest()
    pool = await _pool()
    async with pool.acquire() as conn:
        if await conn.fetchval("SELECT 1 FROM cat_image_pool WHERE sha256 = $1", sha):
            return None
    host = await image_host.resolve_channel(bot)
    if host is None:
        return None
    try:
        msg = await host.send(
            content=f"🐱 cat pool · {q.width}x{q.height} · sharpness {q.sharpness:.0f} · {source}",
            file=discord.File(io.BytesIO(data), filename=f"cat_{sha[:16]}.{_EXT.get(q.fmt, 'jpg')}"),
            allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException:
        logger.warning("[cat-pool] couldn't save a cat to the hosting channel", exc_info=True)
        return None
    if not msg.attachments:
        return None
    async with pool.acquire() as conn:
        pool_id = await conn.fetchval(
            "INSERT INTO cat_image_pool (sha256, source, source_id, width, height, sharpness, bytes, channel_id, "
            "message_id, added_by_clone) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10) ON CONFLICT (sha256) DO NOTHING "
            "RETURNING id",
            sha, source, str(source_id) if source_id else None, q.width, q.height, float(q.sharpness), q.size,
            host.id, msg.id, getattr(bot, "clone_id", None))
    return PooledUrl(msg.attachments[0].url, int(pool_id) if pool_id else None)


async def url_for(bot, pool_id: int) -> Optional[str]:
    """A fresh link to one specific pooled picture (an adopted cat's photo), or None if it can't be read."""
    cached = _url_cache.get(pool_id)
    if cached and cached[0] > time.monotonic():
        return cached[1]
    pool = await _pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT channel_id, message_id FROM cat_image_pool WHERE id = $1", pool_id)
    if row is None:
        return None
    try:
        ch = await _channel(bot, int(row["channel_id"]))
        msg = await ch.fetch_message(int(row["message_id"]))
        url = msg.attachments[0].url if msg.attachments else None
    except (discord.HTTPException, AttributeError):
        return None
    if url:
        _url_cache[pool_id] = (time.monotonic() + URL_TTL_SECONDS, url)
    return url
