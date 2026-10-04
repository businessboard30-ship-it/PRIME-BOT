"""Durable catch reminder scheduling and dispatch primitives (P1-10).

Reminders are claimed from the database before delivery so retries and multiple
workers cannot deliver the same row. Quiet hours are stored in the reminder
payload as ``quiet_hours: {start: HH:MM, end: HH:MM}`` and interpreted in UTC.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Any, Awaitable, Callable

from modules.catch_db import transaction


@dataclass(frozen=True)
class Reminder:
    id: int
    user_id: int
    clone_id: int | None
    kind: str
    delivery: str
    channel_id: int | None
    payload: dict[str, Any]
    attempts: int


def _parse_clock(value: str) -> time:
    try:
        hour, minute = (int(part) for part in value.split(":", 1))
        return time(hour=hour, minute=minute)
    except (TypeError, ValueError) as exc:
        raise ValueError("quiet-hour times must use HH:MM") from exc


def in_quiet_hours(moment: datetime, quiet_hours: dict[str, str] | None) -> bool:
    if not quiet_hours:
        return False
    start = _parse_clock(quiet_hours.get("start", "22:00"))
    end = _parse_clock(quiet_hours.get("end", "07:00"))
    current = moment.astimezone(timezone.utc).time().replace(second=0, microsecond=0)
    if start == end:
        return True
    if start < end:
        return start <= current < end
    return current >= start or current < end


def next_delivery_at(now: datetime, quiet_hours: dict[str, str] | None) -> datetime:
    if not quiet_hours or not in_quiet_hours(now, quiet_hours):
        return now
    end = _parse_clock(quiet_hours.get("end", "07:00"))
    current = now.astimezone(timezone.utc)
    candidate = current.replace(hour=end.hour, minute=end.minute, second=0, microsecond=0)
    if candidate <= current:
        candidate += timedelta(days=1)
    return candidate


async def schedule_reminder(
    *, user_id: int, kind: str, due_at: datetime, dedupe_key: str,
    payload: dict[str, Any] | None = None, clone_id: int | None = None,
    delivery: str = "dm", channel_id: int | None = None, conn=None,
) -> bool:
    if delivery not in {"dm", "channel"}:
        raise ValueError("delivery must be dm or channel")
    async with transaction(conn) as db:
        result = await db.fetchval(
            """INSERT INTO catch_reminders
                (user_id, clone_id, kind, due_at, delivery, channel_id, payload, dedupe_key)
               VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8)
               ON CONFLICT (user_id, clone_key, dedupe_key) DO NOTHING
               RETURNING id""",
            user_id, clone_id, kind, due_at, delivery, channel_id,
            payload or {}, dedupe_key,
        )
        return result is not None


async def claim_due_reminders(*, now: datetime | None = None, limit: int = 100, conn=None) -> tuple[Reminder, ...]:
    if limit <= 0:
        return ()
    now = now or datetime.now(timezone.utc)
    async with transaction(conn) as db:
        rows = await db.fetch(
            """WITH due AS (
                SELECT id FROM catch_reminders
                WHERE NOT delivered AND due_at <= $1
                ORDER BY due_at, id FOR UPDATE SKIP LOCKED LIMIT $2
            )
            UPDATE catch_reminders AS r
               SET attempts = r.attempts + 1
              FROM due WHERE r.id = due.id
            RETURNING r.id, r.user_id, r.clone_id, r.kind, r.delivery,
                      r.channel_id, r.payload, r.attempts""",
            now, limit,
        )
        return tuple(Reminder(**dict(row)) for row in rows)


async def mark_delivered(reminder_id: int, *, delivered_at: datetime | None = None, conn=None) -> None:
    async with transaction(conn) as db:
        await db.execute(
            """UPDATE catch_reminders
               SET delivered = TRUE, delivered_at = COALESCE($2, now())
             WHERE id = $1 AND NOT delivered""",
            reminder_id, delivered_at,
        )


async def dispatch_due_reminders(
    deliver: Callable[[Reminder], Awaitable[bool]], *, now: datetime | None = None,
    limit: int = 100, conn=None,
) -> tuple[int, ...]:
    moment = now or datetime.now(timezone.utc)
    claimed = await claim_due_reminders(now=moment, limit=limit, conn=conn)
    delivered: list[int] = []
    for reminder in claimed:
        quiet = reminder.payload.get("quiet_hours")
        if in_quiet_hours(moment, quiet):
            async with transaction(conn) as db:
                await db.execute("UPDATE catch_reminders SET due_at = $2 WHERE id = $1", reminder.id, next_delivery_at(moment, quiet))
            continue
        if await deliver(reminder):
            await mark_delivered(reminder.id, delivered_at=moment, conn=conn)
            delivered.append(reminder.id)
    return tuple(delivered)


__all__ = ["Reminder", "claim_due_reminders", "dispatch_due_reminders", "in_quiet_hours", "mark_delivered", "next_delivery_at", "schedule_reminder"]
