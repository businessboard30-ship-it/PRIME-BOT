"""Movies hub storage (one row per server). Creates its own table on first use, like meme_store."""
from __future__ import annotations

from typing import Dict, Optional

from database import get_pool

_ready = False
SETTING_FIELDS = ("channel_id", "region", "role_id")


async def ensure_tables() -> None:
    global _ready
    if _ready:
        return
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS discord_movie_config (
                guild_id BIGINT NOT NULL,
                clone_id INTEGER,
                channel_id BIGINT,
                region TEXT NOT NULL DEFAULT 'US',
                role_id BIGINT,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS discord_movie_config_guild_clone_key
            ON discord_movie_config (guild_id, COALESCE(clone_id, -1))
        """)
    _ready = True


def defaults(guild_id: int, clone_id: Optional[int]) -> Dict:
    return {"guild_id": guild_id, "clone_id": clone_id, "channel_id": None, "region": "US", "role_id": None}


async def get_config(guild_id: int, clone_id: Optional[int] = None) -> Dict:
    await ensure_tables()
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM discord_movie_config WHERE guild_id = $1 AND clone_id IS NOT DISTINCT FROM $2", guild_id, clone_id)
    return dict(row) if row else defaults(guild_id, clone_id)


async def set_config(guild_id: int, clone_id: Optional[int] = None, **fields) -> Dict:
    unknown = set(fields) - set(SETTING_FIELDS)
    if unknown:
        raise ValueError(f"unknown movie setting(s): {sorted(unknown)}")
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
            INSERT INTO discord_movie_config (guild_id, clone_id, {", ".join(cols)}) VALUES ($1, $2, {marks})
            ON CONFLICT (guild_id, (COALESCE(clone_id, -1))) DO UPDATE SET {sets}, updated_at = NOW()
            RETURNING *
            """, guild_id, clone_id, *[fields[c] for c in cols])
    return dict(row)
