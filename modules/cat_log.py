# path: modules/cat_log.py

"""
Cat events in the storage (image-hosting) channel.

Every pooled cat picture lives in that channel as one message. This module (a) turns a server name into a safe caption
for that message, and (b) writes a short log line, as a REPLY to the picture's message, whenever something happens to a
cat: adopted, released, gifted or traded (and later: ran away, came back). So the picture and its history sit together.

Rules:
- Logging never blocks or breaks the game: record() only queues; one background worker sends, spaced out so a busy
  moment can't hit Discord's rate limit; if the queue is full or the channel can't be used, the line is dropped.
- Nothing pings anyone (AllowedMentions.none) and every name is cleaned before it is written.
- Automatic spawns are NOT logged (free servers get up to ~90 a day each); they are counted per server by cat_spawns.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Dict, Optional

import discord

logger = logging.getLogger(__name__)

QUEUE_MAX = 300
SEND_GAP_SECONDS = 1.3
NAME_MAX = 60

_queue: Optional[asyncio.Queue] = None
_worker: Optional[asyncio.Task] = None
dropped = 0

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_UNSAFE = re.compile(r"[@#<>\[\]()`*_~|\\]")


def clean_name(raw, fallback: str = "unknown") -> str:
    """A name safe to put in a log line: no mentions, markdown, links or control characters, at most NAME_MAX long."""
    text = _CONTROL.sub(" ", str(raw or ""))
    text = _UNSAFE.sub("", text)
    text = re.sub(r"https?://\S+|discord\.gg\S*", "", text, flags=re.I)
    text = re.sub(r"\s+", " ", text).strip()
    return (text[:NAME_MAX].strip()) or fallback


def server_text(guild_id, guild_name) -> str:
    return f"{clean_name(guild_name, 'a server')} ({int(guild_id)})" if guild_id else clean_name(guild_name, "no server")


def pool_caption(width, height, sharpness, source, server: Optional[tuple]) -> str:
    """The text on the picture's message in the storage channel; names the server it was fetched for."""
    text = f"🐱 cat pool · {width}x{height} · sharpness {sharpness:.0f} · {source}"
    if server and server[0]:
        text += f" · fetched for server: {server_text(server[0], server[1])}"
    return text[:1900]


def _cat(f: Dict[str, Any], prefix: str = "") -> str:
    name, rarity, trait = f.get(prefix + "cat_name"), f.get(prefix + "rarity"), f.get(prefix + "trait")
    out = f"**{clean_name(name, 'a cat')}**"
    if rarity:
        out += f" ({clean_name(rarity)}{' · ' + clean_name(trait) if trait else ''})"
    return out


def _who(f: Dict[str, Any], prefix: str = "user") -> str:
    uid = f.get(f"{prefix}_id")
    return f"{clean_name(f.get(f'{prefix}_name'), 'someone')} ({int(uid)})" if uid else clean_name(f.get(f"{prefix}_name"), "someone")


def format_event(kind: str, f: Dict[str, Any]) -> str:
    """One compact line. `kind`: adopt | release | gift | trade | runaway | returned | history."""
    where = f" · server: {server_text(f.get('guild_id'), f.get('guild_name'))}" if f.get("guild_id") else ""
    if kind == "history":
        # Written later for cats adopted before logging existed. It only claims what the database knows: who owns the cat
        # now (not who first adopted it, which wasn't recorded), where and when it was adopted, how often it changed hands.
        out = f"📜 **Earlier record** · {_cat(f)} · owner now: {_who(f)}"
        if f.get("guild_id"):
            out += f" · adopted in server: {server_text(f.get('guild_id'), f.get('guild_name'))}"
        if f.get("adopted_on"):
            out += f" on {clean_name(f.get('adopted_on'))}"
        if f.get("traded"):
            out += f" · changed hands {int(f['traded'])}x"
        return out
    if kind == "adopt":
        return f"💖 **Adopted** · {_cat(f)} · by {_who(f)}{where}"
    if kind == "release":
        return f"👋 **Released** · {_cat(f)} · by {_who(f)}{where}"
    if kind == "gift":
        return f"🎁 **Gifted** · {_cat(f)} · {_who(f)} → {_who(f, 'other')}{where}"
    if kind == "trade":
        return f"🔁 **Traded** · {_cat(f)} ⇄ {_cat(f, 'other_')} · {_who(f)} ⇄ {_who(f, 'other')}{where}"
    if kind == "runaway":
        return f"🐾 **Left home** · {_cat(f)} · owner was {_who(f)}{where}"
    if kind == "returned":
        return f"🏠 **Back in the spawn** · {_cat(f)}{where}"
    return f"🐱 {clean_name(kind)} · {_cat(f)}{where}"


def record(bot, kind: str, **fields) -> None:
    """Queue one event. Never raises, never waits."""
    global _queue, _worker, dropped
    try:
        loop = asyncio.get_running_loop()
        if _queue is None:
            _queue = asyncio.Queue(maxsize=QUEUE_MAX)
        if _worker is None or _worker.done():
            _worker = loop.create_task(_run())
        _queue.put_nowait((bot, kind, fields))
    except asyncio.QueueFull:
        dropped += 1
    except Exception:
        logger.debug("[cat-log] couldn't queue an event", exc_info=True)


async def _run() -> None:
    while True:
        bot, kind, fields = await _queue.get()
        try:
            await _send(bot, kind, fields)
        except Exception:
            logger.debug("[cat-log] couldn't write an event", exc_info=True)
        await asyncio.sleep(SEND_GAP_SECONDS)


async def _send(bot, kind: str, fields: Dict[str, Any]) -> bool:
    from database import get_pool
    from modules import image_host
    host = await image_host.resolve_channel(bot)
    if host is None:
        return False
    reference = None
    pool_id = fields.get("pool_id")
    if pool_id:
        try:
            pool = await get_pool()
            async with pool.acquire() as conn:
                row = await conn.fetchrow("SELECT channel_id, message_id FROM cat_image_pool WHERE id = $1", int(pool_id))
            if row and int(row["channel_id"]) == host.id:
                reference = discord.MessageReference(message_id=int(row["message_id"]), channel_id=host.id,
                                                     fail_if_not_exists=False)
        except Exception:
            logger.debug("[cat-log] couldn't find the picture message", exc_info=True)
    text = format_event(kind, fields)
    if pool_id:
        text += f" · picture #{int(pool_id)}"
    try:
        await host.send(content=text[:1900], reference=reference, allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException:
        logger.debug("[cat-log] storage channel refused the log line", exc_info=True)
        return False
    return True
