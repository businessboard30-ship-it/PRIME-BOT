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


# ---------------------------------------------------------------------------- v2: Premium perks
#
# Free servers get a plain-text announcement with one message. Premium servers can add:
#   1. a styled embed (title, colour, banner, footer, avatar)
#   2. a birthday role for the day (given on the day, removed the next day)
#   3. up to MAX_EXTRA_MESSAGES more messages (one is picked at random each time)
# Settings are saved for everyone, but only APPLIED while Premium is active (same idea as the
# goodbye style), so if Premium lapses the server quietly goes back to the plain look.

import random as _random
from modules import goodbye_style as _gs

MAX_EXTRA_MESSAGES = 5
EXTRA_MESSAGE_LEN = 300
TITLE_MAX = 80
FOOTER_MAX = 100
DEFAULT_COLOR = 0xEE79C2          # pink, same as the Birthday Pings role
PREMIUM_PERKS = (
    "🎨 Styled announcement: your own title, colour, banner and footer\n"
    "🎭 Birthday role: a special role for the birthday person for the day\n"
    f"🔁 Up to {MAX_EXTRA_MESSAGES} extra messages, picked at random"
)


def parse_extra_messages(text: Optional[str]):
    """One message per line -> (list, error). Blank text clears them. Max 5 lines of 300 chars."""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    if len(lines) > MAX_EXTRA_MESSAGES:
        return None, f"Up to {MAX_EXTRA_MESSAGES} messages, one per line (you sent {len(lines)})."
    if any(len(ln) > EXTRA_MESSAGE_LEN for ln in lines):
        return None, f"Each message can be at most {EXTRA_MESSAGE_LEN} characters."
    return lines, None


def parse_yes_no(text: Optional[str], default: bool = True) -> bool:
    t = (text or "").strip().lower()
    if t in ("n", "no", "off", "false", "0"):
        return False
    if t in ("y", "yes", "on", "true", "1"):
        return True
    return default


def effective_style(cfg: dict, premium: bool) -> dict:
    """The look to post. Non-premium (or nothing customised) -> {'styled': False}."""
    if not premium:
        return {"styled": False}
    title = (cfg.get("embed_title") or "").strip()[:TITLE_MAX]
    image = cfg.get("embed_image_url") or None
    footer = (cfg.get("embed_footer") or "").strip()[:FOOTER_MAX]
    color = cfg.get("embed_color")
    show_avatar = cfg.get("show_avatar")
    styled = bool(title or image or footer or color is not None or show_avatar is False)
    if not styled:
        return {"styled": False}
    return {"styled": True, "title": title or "🎂 Happy Birthday!", "image_url": image, "footer": footer or None,
            "color": int(color) if color is not None else DEFAULT_COLOR,
            "show_avatar": True if show_avatar is None else bool(show_avatar)}


def pick_message(cfg: dict, premium: bool, rng=None) -> str:
    """The main message, or (Premium) a random one of main + extras."""
    main = cfg.get("message") or DEFAULT_MESSAGE
    extras = [m for m in (cfg.get("extra_messages") or []) if m and m.strip()] if premium else []
    return (rng or _random).choice([main] + extras[:MAX_EXTRA_MESSAGES])


def role_remove_after(local_date) -> date:
    """The role is taken back at the start of the next local day."""
    return local_date + timedelta(days=1)


def role_grant_expired(remove_after: date, local_date: date) -> bool:
    return local_date >= remove_after


def style_summary(cfg: dict) -> str:
    bits = []
    if cfg.get("embed_title"):
        bits.append("title")
    if cfg.get("embed_color") is not None:
        bits.append(f"colour {_gs.color_hex(cfg['embed_color'])}")
    if cfg.get("embed_image_url"):
        bits.append("banner")
    if cfg.get("embed_footer"):
        bits.append("footer")
    return ", ".join(bits) if bits else "default pink"
