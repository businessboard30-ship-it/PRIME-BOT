"""Durable catch-spawn scheduler primitives (P1-09).

The scheduler is deliberately database-led: every tick claims due rows with
``FOR UPDATE SKIP LOCKED`` so multiple bot workers cannot process the same
spawn, and restart recovery comes from querying the persisted rows again.
"""
from __future__ import annotations

from dataclasses import dataclass

from modules.catch_db import transaction


@dataclass(frozen=True)
class SchedulerBatch:
    expired_ids: tuple[int, ...] = ()
    purged_ids: tuple[int, ...] = ()


async def claim_expired_spawns(*, limit: int = 100, conn=None) -> tuple[dict, ...]:
    """Claim live expired spawns and return them for message cleanup."""
    if limit <= 0:
        return ()
    async with transaction(conn) as db:
        rows = await db.fetch(
            """WITH due AS (
                SELECT id FROM catch_spawns
                WHERE caught_by IS NULL AND NOT fled AND expires_at <= now()
                ORDER BY expires_at, id
                FOR UPDATE SKIP LOCKED LIMIT $1
            )
            UPDATE catch_spawns AS s SET fled = TRUE
            FROM due WHERE s.id = due.id
            RETURNING s.id, s.guild_id, s.clone_id, s.channel_id, s.message_id,
                      s.species_id, s.expires_at""",
            limit,
        )
        return tuple(dict(row) for row in rows)


async def purge_spawn_history(*, retention_days: int = 30, limit: int = 1000, conn=None) -> tuple[int, ...]:
    """Delete old resolved rows, never live rows."""
    if retention_days < 1:
        raise ValueError("retention_days must be positive")
    if limit <= 0:
        return ()
    async with transaction(conn) as db:
        rows = await db.fetch(
            """DELETE FROM catch_spawns
            WHERE id IN (
                SELECT id FROM catch_spawns
                WHERE expires_at < now() - ($1 * INTERVAL '1 day')
                  AND (caught_by IS NOT NULL OR fled = TRUE)
                ORDER BY expires_at, id
                FOR UPDATE SKIP LOCKED LIMIT $2
            ) RETURNING id""",
            retention_days,
            limit,
        )
        return tuple(int(row["id"]) for row in rows)


async def run_scheduler_batch(*, expire_limit: int = 100, purge_limit: int = 1000, retention_days: int = 30, conn=None) -> SchedulerBatch:
    expired = await claim_expired_spawns(limit=expire_limit, conn=conn)
    purged = await purge_spawn_history(retention_days=retention_days, limit=purge_limit, conn=conn)
    return SchedulerBatch(
        expired_ids=tuple(int(row["id"]) for row in expired),
        purged_ids=purged,
    )


__all__ = ["SchedulerBatch", "claim_expired_spawns", "purge_spawn_history", "run_scheduler_batch"]
