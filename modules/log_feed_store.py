"""Where the shared log feed goes, kept in the database so the main bot AND every clone use the same channel.

One row: the channel id (any process that can see the channel may post there) and a webhook URL for it (works for clones that are
NOT in that server). The webhook URL is a secret: never log it, never show it. Table is created on first use (same convention as
ai_prefs.py), so no migration or schema bump. Reads are cached for a minute so the 10-second flush loop doesn't hit the database.
"""
import re
import time
from typing import Optional, Tuple

CACHE_SECONDS = 60
_cache: Tuple[float, Optional[int], Optional[str]] = (0.0, None, None)

_DDL = ("CREATE TABLE IF NOT EXISTS bot_log_feed (id SMALLINT PRIMARY KEY DEFAULT 1 CHECK (id = 1), channel_id BIGINT, "
        "webhook_url TEXT, updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())")

_ID = re.compile(r"^\s*(?:<#)?(\d{15,21})>?\s*$")


def parse_channel_id(text: str) -> Optional[int]:
    """'123456789012345678' or '<#123456789012345678>' -> the id. Anything else -> None."""
    m = _ID.match(text or "")
    return int(m.group(1)) if m else None


def valid_webhook_url(url: Optional[str]) -> bool:
    return bool(url) and bool(re.match(r"^https://(?:canary\.|ptb\.)?discord(?:app)?\.com/api/webhooks/\d+/[\w-]+$", url))


async def _pool():
    from database import get_pool
    return await get_pool()


async def get(force: bool = False) -> Tuple[Optional[int], Optional[str]]:
    """(channel_id, webhook_url), either may be None. Never raises (a database blip just means 'not configured yet')."""
    global _cache
    now = time.time()
    if not force and now - _cache[0] < CACHE_SECONDS:
        return _cache[1], _cache[2]
    try:
        pool = await _pool()
        async with pool.acquire() as conn:
            await conn.execute(_DDL)
            row = await conn.fetchrow("SELECT channel_id, webhook_url FROM bot_log_feed WHERE id = 1")
        cid = int(row["channel_id"]) if row and row["channel_id"] else None
        url = row["webhook_url"] if row and valid_webhook_url(row["webhook_url"]) else None
    except Exception:
        return _cache[1], _cache[2]
    _cache = (now, cid, url)
    return cid, url


async def save(channel_id: Optional[int], webhook_url: Optional[str]) -> None:
    """Store the shared destination. A webhook_url that isn't a real Discord webhook is dropped, not stored."""
    global _cache
    url = webhook_url if valid_webhook_url(webhook_url) else None
    pool = await _pool()
    async with pool.acquire() as conn:
        await conn.execute(_DDL)
        await conn.execute(
            "INSERT INTO bot_log_feed (id, channel_id, webhook_url) VALUES (1, $1, $2) "
            "ON CONFLICT (id) DO UPDATE SET channel_id = EXCLUDED.channel_id, webhook_url = EXCLUDED.webhook_url, updated_at = NOW()",
            channel_id, url)
    _cache = (time.time(), channel_id, url)


async def forget_webhook() -> None:
    """The stored webhook was deleted on Discord: drop it so the main bot makes a fresh one."""
    global _cache
    cid, _ = await get()
    try:
        await save(cid, None)
    except Exception:
        _cache = (time.time(), cid, None)
