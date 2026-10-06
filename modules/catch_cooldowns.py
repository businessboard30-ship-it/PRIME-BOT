"""Shared cooldown helper (P1-04). One table, one API, every feature.

``try_start`` is atomic: two double-clicks race on the same row and only one
wins, because the upsert only overwrites a cooldown that has already expired.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from modules import catch_db

# Default durations in seconds (P0 balance placeholders, tuned in P11-06).
DEFAULTS: dict[str, int] = {
    "throw": 3,
    "encounter": 600,
    "fish": 300,
    "daily": 72_000,
    "swap": 60,
    "hunt_reroll": 86_400,
    "catchbot": 3_600,
    "vote": 43_200,
}


def remaining_seconds(ready_at: datetime | None, now: datetime | None = None) -> int:
    if ready_at is None:
        return 0
    now = now or datetime.now(timezone.utc)
    return max(0, int((ready_at - now).total_seconds() + 0.999))


def format_remaining(seconds: int) -> str:
    if seconds <= 0:
        return "ready"
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def _duration(kind: str, seconds: int | None) -> int:
    if seconds is not None:
        return max(0, int(seconds))
    if kind not in DEFAULTS:
        raise KeyError(f"unknown cooldown kind: {kind}")
    return DEFAULTS[kind]


async def get_ready_at(user_id: int, clone_id: int | None, kind: str, conn=None) -> datetime | None:
    async with catch_db.connection(conn) as c:
        return await c.fetchval(
            "SELECT ready_at FROM catch_cooldowns "
            "WHERE user_id = $1 AND clone_key = $2 AND kind = $3",
            user_id, catch_db.clone_key(clone_id), kind,
        )


async def try_start(
    user_id: int, clone_id: int | None, kind: str, seconds: int | None = None, conn=None
) -> tuple[bool, datetime]:
    """Start a cooldown if none is running. Returns (started, ready_at)."""
    ready_at = datetime.now(timezone.utc) + timedelta(seconds=_duration(kind, seconds))
    async with catch_db.transaction(conn) as c:
        started = await c.fetchval(
            """
            INSERT INTO catch_cooldowns (user_id, clone_id, kind, ready_at)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (user_id, clone_key, kind) DO UPDATE SET ready_at = EXCLUDED.ready_at
                WHERE catch_cooldowns.ready_at <= now()
            RETURNING ready_at
            """,
            user_id, clone_id, kind, ready_at,
        )
        if started is not None:
            return True, started
        current = await c.fetchval(
            "SELECT ready_at FROM catch_cooldowns "
            "WHERE user_id = $1 AND clone_key = $2 AND kind = $3",
            user_id, catch_db.clone_key(clone_id), kind,
        )
        return False, current


async def clear(user_id: int, clone_id: int | None, kind: str, conn=None) -> None:
    async with catch_db.transaction(conn) as c:
        await c.execute(
            "DELETE FROM catch_cooldowns WHERE user_id = $1 AND clone_key = $2 AND kind = $3",
            user_id, catch_db.clone_key(clone_id), kind,
        )
