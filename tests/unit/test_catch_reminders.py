from datetime import datetime, timezone

import pytest

from modules.catch_reminders import Reminder, dispatch_due_reminders, in_quiet_hours, next_delivery_at


def at(hour, minute=0):
    return datetime(2026, 1, 10, hour, minute, tzinfo=timezone.utc)


def test_cross_midnight_quiet_hours():
    quiet = {"start": "22:00", "end": "07:00"}
    assert in_quiet_hours(at(23), quiet)
    assert in_quiet_hours(at(6, 59), quiet)
    assert not in_quiet_hours(at(7), quiet)
    assert not in_quiet_hours(at(12), quiet)


def test_next_delivery_moves_to_quiet_end():
    result = next_delivery_at(at(23, 15), {"start": "22:00", "end": "07:00"})
    assert result == at(0, 0).replace(day=11, hour=7)


def test_invalid_quiet_hours_are_rejected():
    with pytest.raises(ValueError):
        in_quiet_hours(at(12), {"start": "bad", "end": "07:00"})


def test_failed_delivery_does_not_block_later_reminders(monkeypatch):
    reminders = (
        Reminder(1, 10, None, "encounter", "dm", None, {}, 1),
        Reminder(2, 11, None, "encounter", "dm", None, {}, 1),
    )
    delivered = []

    async def claim(**kwargs):
        return reminders

    async def mark(reminder_id, **kwargs):
        delivered.append(reminder_id)

    async def send(reminder):
        if reminder.id == 1:
            raise RuntimeError("temporary delivery failure")
        return True

    monkeypatch.setattr("modules.catch_reminders.claim_due_reminders", claim)
    monkeypatch.setattr("modules.catch_reminders.mark_delivered", mark)

    async def run():
        return await dispatch_due_reminders(send, now=at(12))

    import asyncio

    assert asyncio.run(run()) == (2,)
    assert delivered == [2]
