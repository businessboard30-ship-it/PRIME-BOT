"""Meme feed storage. Creates its own table on first use (CREATE TABLE IF NOT EXISTS), like counting_store."""
from __future__ import annotations

from typing import Dict, List, Optional

from database import get_pool

_ready = False
SETTING_FIELDS = ("enabled", "channel_id", "interval_hours", "subs", "ping_role_id")


async def ensure_tables() -> None:
    global _ready
    if _ready:
        return
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS discord_meme_config (
                guild_id BIGINT NOT NULL,
                clone_id INTEGER,
                enabled BOOLEAN NOT NULL DEFAULT FALSE,
                channel_id BIGINT,
                interval_hours INTEGER NOT NULL DEFAULT 6,
                subs TEXT NOT NULL DEFAULT 'memes,dankmemes,wholesomememes',
                ping_role_id BIGINT,
                last_post_at TIMESTAMPTZ,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS discord_meme_config_guild_clone_key
            ON discord_meme_config (guild_id, COALESCE(clone_id, -1))
        """)
    _ready = True


def defaults(guild_id: int, clone_id: Optional[int]) -> Dict:
    return {"guild_id": guild_id, "clone_id": clone_id, "enabled": False, "channel_id": None, "interval_hours": 6,
            "subs": "memes,dankmemes,wholesomememes", "ping_role_id": None, "last_post_at": None}


async def get_config(guild_id: int, clone_id: Optional[int] = None) -> Dict:
    await ensure_tables()
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM discord_meme_config WHERE guild_id = $1 AND clone_id IS NOT DISTINCT FROM $2", guild_id, clone_id)
    return dict(row) if row else defaults(guild_id, clone_id)


async def set_config(guild_id: int, clone_id: Optional[int] = None, **fields) -> Dict:
    unknown = set(fields) - set(SETTING_FIELDS)
    if unknown:
        raise ValueError(f"unknown meme setting(s): {sorted(unknown)}")
    if not fields:
        return await get_config(guild_id, clone_id)
    await ensure_tables()
    cols = list(fields)
    marks = ", ".join(f"${i + 3}" for i in range(len(cols)))
    sets = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols)
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"""
            INSERT INTO discord_meme_config (guild_id, clone_id, {", ".join(cols)}) VALUES ($1, $2, {marks})
            ON CONFLICT (guild_id, (COALESCE(clone_id, -1))) DO UPDATE SET {sets}, updated_at = NOW()
            RETURNING *
            """, guild_id, clone_id, *[fields[c] for c in cols])
    return dict(row)


async def enabled_configs(clone_id: Optional[int]) -> List[Dict]:
    await ensure_tables()
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM discord_meme_config WHERE enabled AND channel_id IS NOT NULL AND clone_id IS NOT DISTINCT FROM $1",
            clone_id)
    return [dict(r) for r in rows]


async def mark_posted(guild_id: int, clone_id: Optional[int]) -> None:
    await ensure_tables()
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE discord_meme_config SET last_post_at = NOW() WHERE guild_id = $1 AND clone_id IS NOT DISTINCT FROM $2",
            guild_id, clone_id)
