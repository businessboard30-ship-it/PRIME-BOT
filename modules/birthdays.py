"""
Birthdays — pure logic (no Discord calls, no database), so it is easy to test.

A birthday is just a month + day (no year is ever stored). Feb 29 birthdays are
celebrated on Feb 28 in years that are not leap years.
Used by discord_bot/cogs/birthdays.py.
"""

from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta, timezone
from typing import Optional, Tuple

MONTH_NAMES = [calendar.month_name[i] for i in range(1, 13)]
DEFAULT_MESSAGE = "🎂 Happy birthday {user}! Everyone in **{server}**, wish them a great day! 🎉"
MAX_MESSAGE_LEN = 500
MIN_OFFSET, MAX_OFFSET = -12, 14      # UTC offset in whole hours
MAX_MENTIONS = 20                      # people mentioned in one announcement


def validate_birthday(month, day) -> Optional[Tuple[int, int]]:
    """(month, day) if it is a real calendar day (Feb 29 allowed), else None."""
    try:
        m, d = int(str(month).strip()), int(str(day).strip())
    except (TypeError, ValueError):
        return None
    if not 1 <= m <= 12:
        return None
    if not 1 <= d <= calendar.monthrange(2024, m)[1]:   # 2024 = leap year
        return None
    return m, d


def celebration_day(month: int, day: int, year: int) -> Tuple[int, int]:
    """The day this birthday is celebrated in `year` (Feb 29 -> Feb 28 off leap years)."""
    if month == 2 and day == 29 and not calendar.isleap(year):
        return 2, 28
    return month, day


def is_birthday_today(month: int, day: int, today: date) -> bool:
    return celebration_day(month, day, today.year) == (today.month, today.day)


def days_until(month: int, day: int, today: date) -> int:
    """Days from `today` to the next celebration (0 = today)."""
    for year in (today.year, today.year + 1):
        m, d = celebration_day(month, day, year)
        target = date(year, m, d)
        if target >= today:
            return (target - today).days
    return 0  # unreachable


def pretty_date(month: int, day: int) -> str:
    return f"{day} {MONTH_NAMES[month - 1]}"


def days_until_text(n: int) -> str:
    return "today 🎉" if n == 0 else "tomorrow" if n == 1 else f"in {n} days"


def validate_offset(value) -> Optional[int]:
    try:
        n = int(str(value).strip().replace("UTC", "").replace("utc", "").replace("+", ""))
    except (TypeError, ValueError):
        return None
    return n if MIN_OFFSET <= n <= MAX_OFFSET else None


def validate_hour(value) -> Optional[int]:
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return n if 0 <= n <= 23 else None


def local_now(offset_hours: int, now: Optional[datetime] = None) -> datetime:
    """Current time in a server's chosen UTC offset (naive datetime)."""
    now = now or datetime.now(timezone.utc)
    return (now + timedelta(hours=int(offset_hours))).replace(tzinfo=None)


def due_for_announcement(local: datetime, announce_hour: int, last_announced: Optional[date]) -> bool:
    """True once per local day, from announce_hour onward (so a late restart still posts)."""
    return local.hour >= announce_hour and last_announced != local.date()


def format_message(template: str, mentions: str, names: str, server: str) -> str:
    """Fill {user} / {name} / {server}. Plain replace on purpose, so stray braces never crash it."""
    text = (template or DEFAULT_MESSAGE)
    return (text.replace("{user}", mentions).replace("{name}", names).replace("{server}", server))[:1900]
