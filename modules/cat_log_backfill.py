# path: modules/cat_log_backfill.py

"""
Writes up OLD cats in the storage channel: the ones adopted, and the pictures stored, before the event log existed
(#238). Two jobs, both slow and safe:

  adoptions  one "Earlier record" line per cat adopted before logging started, as a reply under its picture
  pictures   pictures stored before the server name went into the caption get "first spawned in server: X (id)"
             (the server of the picture's earliest spawn; the fetching server wasn't recorded for old pictures)

Rules: a small ledger table (cat_log_backfill) means nothing is written twice, across restarts or ticks; a small batch per
tick with a pause between Discord calls; a failed item is retried up to MAX_ATTEMPTS then skipped; nothing here can touch
the game (no cat, wallet or spawn is changed); main bot only (the pool and storage channel are shared by every bot).
Adopted cats after the cutoff were logged live, so they are never backfilled. Set CAT_LOG_BACKFILL=0 to switch it off and
CAT_LOG_BACKFILL_BEFORE (ISO time) to move the cutoff.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import discord

from modules import cat_log

logger = logging.getLogger(__name__)

# #238 (storage-channel log) was merged 2026-10-11 12:04:30 UTC; ten minutes of slack for the deploy. A cat adopted in that
# window may get a second line; a cat adopted before it never goes unrecorded.
DEFAULT_CUTOFF = datetime(2026, 10, 11, 12, 14, 30, tzinfo=timezone.utc)
BATCH = 15
MAX_ATTEMPTS = 3
CONTENT_MAX = 2000
_MARKERS = ("fetched for server", "first spawned in server")


def enabled() -> bool:
    return os.getenv("CAT_LOG_BACKFILL", "1").strip() != "0"


def cutoff() -> datetime:
    raw = os.getenv("CAT_LOG_BACKFILL_BEFORE", "").strip()
    if raw:
        try:
            when = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return when if when.tzinfo else when.replace(tzinfo=timezone.utc)
        except ValueError:
            logger.warning("[cat-backfill] ignoring unreadable CAT_LOG_BACKFILL_BEFORE")
    return DEFAULT_CUTOFF


def history_fields(row: Dict[str, Any], bot) -> Dict[str, Any]:
    """The log fields for one adoption row. Names come from the cache only (no API calls); unknown ones fall back."""
    user = bot.get_user(int(row["owner_id"])) if row.get("owner_id") else None
    guild = bot.get_guild(int(row["adopted_guild_id"])) if row.get("adopted_guild_id") else None
    adopted = row.get("adopted_at")
    return {"cat_name": row.get("name"), "rarity": row.get("rarity"), "trait": row.get("trait"),
            "user_id": row.get("owner_id"), "user_name": getattr(user, "display_name", None) or getattr(user, "name", None),
            "guild_id": row.get("adopted_guild_id"), "guild_name": getattr(guild, "name", None),
            "adopted_on": adopted.strftime("%Y-%m-%d") if hasattr(adopted, "strftime") else None,
            "traded": row.get("traded_count") or 0, "pool_id": row.get("pool_id")}


def with_server_note(content: str, guild_id, guild_name) -> Optional[str]:
    """The picture message text with the server note added, or None when there is nothing to do (already named, no server,
    or it would not fit in a Discord message). Never changes the existing text, only appends."""
    content = content or ""
    if not guild_id or any(m in content for m in _MARKERS):
        return None
    note = f" · first spawned in server: {cat_log.server_text(guild_id, guild_name)}"
    return content + note if len(content) + len(note) <= CONTENT_MAX else None


# ---- ledger ----
async def _pool():
    from database import get_pool
    return await get_pool()


async def pending_adoptions(limit: int, before: datetime) -> List[Dict[str, Any]]:
    async with (await _pool()).acquire() as conn:
        rows = await conn.fetch(
            """SELECT a.* FROM cat_adoptions a
               LEFT JOIN cat_log_backfill b ON b.kind = 'adoption' AND b.ref_id = a.id
               WHERE a.adopted_at < $1 AND (b.ref_id IS NULL OR b.status = 'retry')
               ORDER BY a.id LIMIT $2""", before, int(limit))
    return [dict(r) for r in rows]


async def pending_pictures(limit: int, before: datetime) -> List[Dict[str, Any]]:
    async with (await _pool()).acquire() as conn:
        rows = await conn.fetch(
            """SELECT p.id, p.channel_id, p.message_id,
                      (SELECT s.guild_id FROM cat_spawns s WHERE s.pool_id = p.id ORDER BY s.created_at, s.id LIMIT 1) AS first_guild
               FROM cat_image_pool p
               LEFT JOIN cat_log_backfill b ON b.kind = 'picture' AND b.ref_id = p.id
               WHERE p.created_at < $1 AND (b.ref_id IS NULL OR b.status = 'retry')
               ORDER BY p.id LIMIT $2""", before, int(limit))
    return [dict(r) for r in rows]


async def mark(kind: str, ref_id: int, status: str) -> str:
    """Record the outcome. 'retry' becomes 'skipped' after MAX_ATTEMPTS. Returns the stored status."""
    async with (await _pool()).acquire() as conn:
        return await conn.fetchval(
            """INSERT INTO cat_log_backfill (kind, ref_id, status, attempts) VALUES ($1, $2, $3, CASE WHEN $3 = 'retry' THEN 1 ELSE 0 END)
               ON CONFLICT (kind, ref_id) DO UPDATE SET
                   attempts = cat_log_backfill.attempts + CASE WHEN $3 = 'retry' THEN 1 ELSE 0 END,
                   status = CASE WHEN $3 = 'retry' AND cat_log_backfill.attempts + 1 >= $4 THEN 'skipped' ELSE $3 END,
                   updated_at = NOW()
               RETURNING status""", kind, int(ref_id), status, MAX_ATTEMPTS)


async def run_once(bot, limit: int = BATCH, gap: float = cat_log.SEND_GAP_SECONDS) -> Dict[str, Any]:
    """One tick: up to `limit` items, adoptions first. Never raises past a single item."""
    from modules import image_host
    out = {"logged": 0, "captions": 0, "skipped": 0, "retry": 0}
    host = await image_host.resolve_cat_channel(bot)
    if host is None:
        return {**out, "error": "no_storage_channel"}               # nothing marked: tried again next tick
    before, left = cutoff(), int(limit)
    for row in await pending_adoptions(left, before):
        left -= 1
        try:
            ok = await cat_log._send(bot, "history", history_fields(row, bot))
        except Exception:
            logger.debug("[cat-backfill] adoption line failed", exc_info=True)
            ok = False
        status = await mark("adoption", row["id"], "done" if ok else "retry")
        out["logged" if ok else ("retry" if status == "retry" else "skipped")] += 1
        await asyncio.sleep(gap)
    for row in await pending_pictures(max(left, 0), before):
        status = await _caption_one(bot, host, row)
        out[{"done": "captions", "skipped": "skipped", "retry": "retry"}[status]] += 1
        await asyncio.sleep(gap)
    return out


async def _caption_one(bot, host, row: Dict[str, Any]) -> str:
    """-> 'done' | 'skipped' | 'retry' (already stored in the ledger)."""
    pid = row["id"]
    if not row.get("first_guild") or int(row["channel_id"]) != host.id:
        return await mark("picture", pid, "skipped")                # never spawned anywhere / lives in another channel
    try:
        msg = await host.fetch_message(int(row["message_id"]))
    except (discord.NotFound, discord.Forbidden):
        return await mark("picture", pid, "skipped")
    except Exception:
        logger.warning("swallowed exception in _caption_one", exc_info=True)
        return await mark("picture", pid, "retry")
    guild = bot.get_guild(int(row["first_guild"]))
    new = with_server_note(msg.content, row["first_guild"], getattr(guild, "name", None))
    if new is None or getattr(msg.author, "id", None) != getattr(bot.user, "id", None):
        return await mark("picture", pid, "skipped")                # already named, or not our message to edit
    try:
        await msg.edit(content=new, allowed_mentions=discord.AllowedMentions.none())
    except (discord.Forbidden, discord.NotFound):
        return await mark("picture", pid, "skipped")
    except Exception:
        logger.warning("swallowed exception in _caption_one", exc_info=True)
        return await mark("picture", pid, "retry")
    return await mark("picture", pid, "done")
