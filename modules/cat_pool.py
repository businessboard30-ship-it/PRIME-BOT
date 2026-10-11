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
import time
from typing import Dict, List, Optional, Tuple

import discord

from modules.cat_quality import Quality

logger = logging.getLogger(__name__)

URL_TTL_SECONDS = 6 * 3600
_url_cache: Dict[int, Tuple[float, str]] = {}
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


async def _channel(bot, channel_id: int):
    return bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)


async def pick(bot) -> Optional[str]:
    """A fresh link to one pooled picture (least used first, random among ties), or None."""
    for _ in range(3):
        pool = await _pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, channel_id, message_id FROM cat_image_pool ORDER BY uses ASC, random() LIMIT 1")
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
            return url
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
        await conn.execute(
            "INSERT INTO cat_image_pool (sha256, source, source_id, width, height, sharpness, bytes, channel_id, "
            "message_id, added_by_clone) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10) ON CONFLICT (sha256) DO NOTHING",
            sha, source, str(source_id) if source_id else None, q.width, q.height, float(q.sharpness), q.size,
            host.id, msg.id, getattr(bot, "clone_id", None))
    return msg.attachments[0].url
