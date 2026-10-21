"""
Birthday storage. Self-contained: creates its own two tables the first time it is used
(CREATE TABLE IF NOT EXISTS), so nothing in database.py has to change.

  discord_birthdays        one row per user (global: set once, works in every server)
  discord_birthday_config  one row per server (+ clone, like the other discord_* config tables)
"""

from __future__ import annotations

from datetime import date
from typing import Dict, List, Optional

from database import get_pool
from modules import birthdays as bd

_ready = False

_CONFIG_FIELDS = ("enabled", "channel_id", "ping_role_id", "message", "utc_offset_hours", "announce_hour",
                  # v2 (Premium perks; saved for everyone, only applied while Premium is active)
                  "embed_title", "embed_color", "embed_image_url", "embed_footer", "show_avatar",
                  "birthday_role_id", "extra_messages")


async def ensure_tables() -> None:
    global _ready
    if _ready:
        return
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS discord_birthdays (
                user_id BIGINT PRIMARY KEY,
                month SMALLINT NOT NULL,
                day SMALLINT NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        await conn.execute("CREATE INDEX IF NOT EXISTS discord_birthdays_month_day ON discord_birthdays (month, day)")
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS discord_birthday_config (
                guild_id BIGINT NOT NULL,
                clone_id INTEGER,
                enabled BOOLEAN NOT NULL DEFAULT FALSE,
                channel_id BIGINT,
                ping_role_id BIGINT,
                message TEXT,
                utc_offset_hours SMALLINT NOT NULL DEFAULT 0,
                announce_hour SMALLINT NOT NULL DEFAULT 9,
                last_announced DATE,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS discord_birthday_config_guild_clone_key
            ON discord_birthday_config (guild_id, COALESCE(clone_id, -1))
        """)
        # v2: Premium perks. ADD COLUMN IF NOT EXISTS keeps v1 servers working untouched.
        for ddl in (
            "embed_title TEXT", "embed_color INTEGER", "embed_image_url TEXT", "embed_footer TEXT",
            "show_avatar BOOLEAN", "birthday_role_id BIGINT", "extra_messages TEXT[]",
        ):
            await conn.execute(f"ALTER TABLE discord_birthday_config ADD COLUMN IF NOT EXISTS {ddl}")
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS discord_birthday_role_grants (
                guild_id BIGINT NOT NULL,
                clone_id INTEGER,
                user_id BIGINT NOT NULL,
                role_id BIGINT NOT NULL,
                remove_after DATE NOT NULL
            )
        """)
        await conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS discord_birthday_role_grants_key
            ON discord_birthday_role_grants (guild_id, COALESCE(clone_id, -1), user_id, role_id)
        """)
    _ready = True


# ---- members -------------------------------------------------------------------------

async def set_birthday(user_id: int, month: int, day: int) -> None:
    await ensure_tables()
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO discord_birthdays (user_id, month, day) VALUES ($1, $2, $3)
            ON CONFLICT (user_id) DO UPDATE SET month = $2, day = $3, updated_at = NOW()
            """,
            user_id, month, day,
        )


async def get_birthday(user_id: int) -> Optional[Dict]:
    await ensure_tables()
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT user_id, month, day FROM discord_birthdays WHERE user_id = $1", user_id)
        return dict(row) if row else None


async def remove_birthday(user_id: int) -> bool:
    await ensure_tables()
    pool = await get_pool()
    async with pool.acquire() as conn:
        return (await conn.execute("DELETE FROM discord_birthdays WHERE user_id = $1", user_id)) != "DELETE 0"


async def birthdays_on(today: date) -> List[int]:
    """User ids whose birthday is celebrated on `today` (handles Feb 29 on Feb 28)."""
    await ensure_tables()
    days = [today.day]
    if today.month == 2 and today.day == 28 and bd.celebration_day(2, 29, today.year) == (2, 28):
        days.append(29)
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT user_id FROM discord_birthdays WHERE month = $1 AND day = ANY($2::int[])",
            today.month, days,
        )
    return [r["user_id"] for r in rows]


async def all_birthdays() -> List[Dict]:
    await ensure_tables()
    pool = await get_pool()
    async with pool.acquire() as conn:
        return [dict(r) for r in await conn.fetch("SELECT user_id, month, day FROM discord_birthdays")]


# ---- servers -------------------------------------------------------------------------

def _defaults(guild_id: int, clone_id: Optional[int]) -> Dict:
    return {"guild_id": guild_id, "clone_id": clone_id, "enabled": False, "channel_id": None,
            "ping_role_id": None, "message": None, "utc_offset_hours": 0, "announce_hour": 9,
            "last_announced": None, "embed_title": None, "embed_color": None, "embed_image_url": None,
            "embed_footer": None, "show_avatar": None, "birthday_role_id": None, "extra_messages": None}


async def get_config(guild_id: int, clone_id: Optional[int] = None) -> Dict:
    await ensure_tables()
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM discord_birthday_config WHERE guild_id = $1 AND clone_id IS NOT DISTINCT FROM $2",
            guild_id, clone_id,
        )
    return dict(row) if row else _defaults(guild_id, clone_id)


async def set_config(guild_id: int, clone_id: Optional[int] = None, **fields) -> Dict:
    """Upsert-merge: omitted fields keep their value. Pass None explicitly to clear a field."""
    unknown = set(fields) - set(_CONFIG_FIELDS)
    if unknown:
        raise ValueError(f"unknown birthday config field(s): {sorted(unknown)}")
    current = await get_config(guild_id, clone_id)
    merged = {k: (fields[k] if k in fields else current.get(k)) for k in _CONFIG_FIELDS}
    cols = ", ".join(_CONFIG_FIELDS)
    marks = ", ".join(f"${i + 3}" for i in range(len(_CONFIG_FIELDS)))
    sets = ", ".join(f"{k} = ${i + 3}" for i, k in enumerate(_CONFIG_FIELDS))
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"""
            INSERT INTO discord_birthday_config (guild_id, clone_id, {cols}) VALUES ($1, $2, {marks})
            ON CONFLICT (guild_id, (COALESCE(clone_id, -1))) DO UPDATE SET {sets}, updated_at = NOW()
            RETURNING *
            """,
            guild_id, clone_id, *[merged[k] for k in _CONFIG_FIELDS],
        )
    return dict(row)


async def enabled_configs(clone_id: Optional[int] = None) -> List[Dict]:
    await ensure_tables()
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM discord_birthday_config WHERE enabled AND channel_id IS NOT NULL "
            "AND clone_id IS NOT DISTINCT FROM $1",
            clone_id,
        )
    return [dict(r) for r in rows]


async def claim_day(guild_id: int, clone_id: Optional[int], local_date: date) -> bool:
    """Atomically mark today as announced. True only for the one caller that wins (no double posts)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE discord_birthday_config SET last_announced = $3
            WHERE guild_id = $1 AND clone_id IS NOT DISTINCT FROM $2
              AND (last_announced IS NULL OR last_announced < $3)
            RETURNING 1
            """,
            guild_id, clone_id, local_date,
        )
    return row is not None


# ---- v2: birthday role grants -----------------------------------------------------------

async def add_role_grant(guild_id: int, clone_id: Optional[int], user_id: int, role_id: int, remove_after: date) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO discord_birthday_role_grants (guild_id, clone_id, user_id, role_id, remove_after)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (guild_id, (COALESCE(clone_id, -1)), user_id, role_id) DO UPDATE SET remove_after = $5
            """,
            guild_id, clone_id, user_id, role_id, remove_after,
        )


async def role_grants(clone_id: Optional[int] = None) -> List[Dict]:
    await ensure_tables()
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT guild_id, user_id, role_id, remove_after FROM discord_birthday_role_grants "
            "WHERE clone_id IS NOT DISTINCT FROM $1", clone_id)
    return [dict(r) for r in rows]


async def delete_role_grant(guild_id: int, clone_id: Optional[int], user_id: int, role_id: int) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM discord_birthday_role_grants WHERE guild_id = $1 AND clone_id IS NOT DISTINCT FROM $2 "
            "AND user_id = $3 AND role_id = $4", guild_id, clone_id, user_id, role_id)
