# path: modules/counting_store.py

"""
Counting storage. Self-contained: creates its own two tables the first time it is used
(CREATE TABLE IF NOT EXISTS), so nothing in database.py has to change.

  discord_counting_config  one row per server (+ clone, like the other discord_* config tables): settings + the live run
  discord_counting_users   one row per server member who has counted: correct counts and mistakes
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from database import get_pool

_ready = False

SETTING_FIELDS = ("enabled", "channel_id", "math_on", "saves_on", "milestone_every")
STATE_FIELDS = ("current", "high_score", "last_user_id", "saves", "total_correct", "total_ruined")


async def ensure_tables() -> None:
    global _ready
    if _ready:
        return
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS discord_counting_config (
                guild_id BIGINT NOT NULL,
                clone_id INTEGER,
                enabled BOOLEAN NOT NULL DEFAULT FALSE,
                channel_id BIGINT,
                math_on BOOLEAN NOT NULL DEFAULT FALSE,
                saves_on BOOLEAN NOT NULL DEFAULT FALSE,
                milestone_every INTEGER NOT NULL DEFAULT 0,
                current BIGINT NOT NULL DEFAULT 0,
                high_score BIGINT NOT NULL DEFAULT 0,
                last_user_id BIGINT,
                saves SMALLINT NOT NULL DEFAULT 1,
                total_correct BIGINT NOT NULL DEFAULT 0,
                total_ruined BIGINT NOT NULL DEFAULT 0,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS discord_counting_config_guild_clone_key
            ON discord_counting_config (guild_id, COALESCE(clone_id, -1))
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS discord_counting_users (
                guild_id BIGINT NOT NULL,
                clone_id INTEGER,
                user_id BIGINT NOT NULL,
                correct BIGINT NOT NULL DEFAULT 0,
                wrong BIGINT NOT NULL DEFAULT 0
            )
        """)
        await conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS discord_counting_users_key
            ON discord_counting_users (guild_id, COALESCE(clone_id, -1), user_id)
        """)
    _ready = True


def defaults(guild_id: int, clone_id: Optional[int]) -> Dict:
    return {"guild_id": guild_id, "clone_id": clone_id, "enabled": False, "channel_id": None, "math_on": False,
            "saves_on": False, "milestone_every": 0, "current": 0, "high_score": 0, "last_user_id": None,
            "saves": 1, "total_correct": 0, "total_ruined": 0}


async def get_config(guild_id: int, clone_id: Optional[int] = None) -> Dict:
    await ensure_tables()
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM discord_counting_config WHERE guild_id = $1 AND clone_id IS NOT DISTINCT FROM $2",
            guild_id, clone_id)
    return dict(row) if row else defaults(guild_id, clone_id)


async def set_config(guild_id: int, clone_id: Optional[int] = None, **fields) -> Dict:
    """Upsert only the settings passed (the live run is never touched here). Pass None explicitly to clear channel_id."""
    unknown = set(fields) - set(SETTING_FIELDS)
    if unknown:
        raise ValueError(f"unknown counting setting(s): {sorted(unknown)}")
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
            INSERT INTO discord_counting_config (guild_id, clone_id, {", ".join(cols)}) VALUES ($1, $2, {marks})
            ON CONFLICT (guild_id, (COALESCE(clone_id, -1))) DO UPDATE SET {sets}, updated_at = NOW()
            RETURNING *
            """,
            guild_id, clone_id, *[fields[c] for c in cols])
    return dict(row)


async def record(guild_id: int, clone_id: Optional[int], state: Dict, user_id: int, correct: bool) -> None:
    """Persist the live run after one attempt and bump that member's tally. One connection, one transaction."""
    await ensure_tables()
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                UPDATE discord_counting_config
                SET current = $3, high_score = $4, last_user_id = $5, saves = $6,
                    total_correct = $7, total_ruined = $8, updated_at = NOW()
                WHERE guild_id = $1 AND clone_id IS NOT DISTINCT FROM $2
                """,
                guild_id, clone_id, int(state["current"]), int(state["high_score"]), state.get("last_user_id"),
                int(state["saves"]), int(state["total_correct"]), int(state["total_ruined"]))
            await conn.execute(
                f"""
                INSERT INTO discord_counting_users (guild_id, clone_id, user_id, {"correct" if correct else "wrong"})
                VALUES ($1, $2, $3, 1)
                ON CONFLICT (guild_id, (COALESCE(clone_id, -1)), user_id)
                DO UPDATE SET {"correct = discord_counting_users.correct + 1" if correct
                               else "wrong = discord_counting_users.wrong + 1"}
                """,
                guild_id, clone_id, user_id)


async def reset_run(guild_id: int, clone_id: Optional[int] = None) -> None:
    """Back to 0 (best score and tallies are kept)."""
    await ensure_tables()
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE discord_counting_config SET current = 0, last_user_id = NULL, updated_at = NOW() "
            "WHERE guild_id = $1 AND clone_id IS NOT DISTINCT FROM $2", guild_id, clone_id)


async def top_users(guild_id: int, clone_id: Optional[int], limit: int = 15) -> List[Tuple[int, int, int]]:
    """(user_id, correct, wrong), most correct first."""
    await ensure_tables()
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT user_id, correct, wrong FROM discord_counting_users "
            "WHERE guild_id = $1 AND clone_id IS NOT DISTINCT FROM $2 AND correct > 0 "
            "ORDER BY correct DESC, user_id LIMIT $3", guild_id, clone_id, int(limit))
    return [(r["user_id"], r["correct"], r["wrong"]) for r in rows]


async def enabled_configs(clone_id: Optional[int] = None) -> List[Dict]:
    """Servers with counting on. If the table doesn't exist nobody ever used the feature: [] without creating anything."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if await conn.fetchval("SELECT to_regclass('discord_counting_config')") is None:
            return []
        rows = await conn.fetch(
            "SELECT * FROM discord_counting_config WHERE enabled AND channel_id IS NOT NULL "
            "AND clone_id IS NOT DISTINCT FROM $1", clone_id)
    return [dict(r) for r in rows]
