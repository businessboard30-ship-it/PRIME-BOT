"""Welcome Card Studio Phase 7: scheduled designs (pure helpers, no Discord, no database, never raise).

A saved design can be scheduled for a date range (Halloween look from 25 Oct to 1 Nov). While today (UTC) is inside the
range, the join card uses that design's look; outside it, the live look is used again. Nothing polls: the choice is made
at render time from today's date, so there is no loop to run and nothing to miss if the bot was down on the day.

Two kinds of range:
- once:   both ends have a year (2026-10-25 to 2026-11-01).
- yearly: both ends are a day and month only (25 Oct to 1 Nov); repeats every year, may wrap over New Year.
"""
from __future__ import annotations

import datetime as _dt
import re
from typing import Optional
import logging

logger = logging.getLogger(__name__)

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
_MONTH_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
YEARLY_YEAR = 2000        # a leap year, so 29 Feb is a valid yearly day; the year of a yearly range is never used


def _mk(y: int, m: int, d: int) -> Optional[_dt.date]:
    try:
        return _dt.date(y, m, d)
    except (ValueError, OverflowError):
        return None


def parse_when(raw) -> Optional[tuple]:
    """(date, yearly) from what an admin typed, or None when it is not understood.

    Understood: `2026-10-25` (once), `25 Oct`, `Oct 25`, `25 October`, `10-25` or `10/25` as month-day (yearly).
    Month-day numbers are always MONTH first, written out in the wizard text. Never raises."""
    try:
        text = re.sub(r"\s+", " ", str(raw or "").strip().lower().replace(",", " ")).strip()
        if not text:
            return None
        m = re.fullmatch(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", text)
        if m:
            d = _mk(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            return (d, False) if d else None
        m = re.fullmatch(r"(\d{1,2})[-/.](\d{1,2})", text)
        if m:
            d = _mk(YEARLY_YEAR, int(m.group(1)), int(m.group(2)))
            return (d, True) if d else None
        m = re.fullmatch(r"(\d{1,2})(?:st|nd|rd|th)? ([a-z]+)", text)
        if m and m.group(2)[:3] in _MONTHS:
            d = _mk(YEARLY_YEAR, _MONTHS[m.group(2)[:3]], int(m.group(1)))
            return (d, True) if d else None
        m = re.fullmatch(r"([a-z]+) (\d{1,2})(?:st|nd|rd|th)?", text)
        if m and m.group(1)[:3] in _MONTHS:
            d = _mk(YEARLY_YEAR, _MONTHS[m.group(1)[:3]], int(m.group(2)))
            return (d, True) if d else None
    except Exception:
        logger.debug("swallowed exception in parse_when", exc_info=True)
        pass
    return None


def build_range(from_raw, to_raw) -> tuple:
    """(status, from_date, to_date, yearly). status is 'ok', 'bad_from', 'bad_to', 'mixed' (one end has a year and the
    other does not) or 'backwards' (a one-off range that ends before it starts). A blank `to` means a single day."""
    a = parse_when(from_raw)
    if a is None:
        return ("bad_from", None, None, False)
    if not str(to_raw or "").strip():
        return ("ok", a[0], a[0], a[1])
    b = parse_when(to_raw)
    if b is None:
        return ("bad_to", None, None, False)
    if a[1] != b[1]:
        return ("mixed", None, None, False)
    if not a[1] and b[0] < a[0]:
        return ("backwards", None, None, False)
    return ("ok", a[0], b[0], a[1])


def _as_date(v) -> Optional[_dt.date]:
    if isinstance(v, _dt.datetime):
        return v.date()
    return v if isinstance(v, _dt.date) else None


def is_active(design: dict, today: _dt.date) -> bool:
    """True when this design's schedule covers `today`. A design without a (valid) schedule is never active."""
    try:
        a, b = _as_date(design.get("sched_from")), _as_date(design.get("sched_to"))
        if a is None or b is None:
            return False
        if design.get("sched_yearly"):
            t, s, e = (today.month, today.day), (a.month, a.day), (b.month, b.day)
            return (s <= t <= e) if s <= e else (t >= s or t <= e)
        return a <= today <= b
    except Exception:
        logger.debug("swallowed exception in is_active", exc_info=True)
        return False


def pick_scheduled(designs, today: _dt.date) -> Optional[dict]:
    """The design that should be live today, or None. If several cover today: a one-off beats a yearly one, then the one
    that started latest (the narrower, more recent window), then the lowest id. Never raises."""
    try:
        live = [d for d in (designs or []) if isinstance(d, dict) and is_active(d, today)]
        if not live:
            return None

        def start_key(d):
            a = _as_date(d.get("sched_from"))
            return (a.month, a.day) if d.get("sched_yearly") else (a.year, a.month, a.day)

        live.sort(key=lambda d: (0 if not d.get("sched_yearly") else 1, tuple(-x for x in start_key(d)), int(d.get("id") or 0)))
        return live[0]
    except Exception:
        logger.debug("swallowed exception in pick_scheduled", exc_info=True)
        return None


def _fmt(d: _dt.date, yearly: bool) -> str:
    return f"{d.day} {_MONTH_NAMES[d.month - 1]}" + ("" if yearly else f" {d.year}")


def describe(design: dict) -> Optional[str]:
    """`25 Oct to 1 Nov every year` / `25 Oct 2026 to 1 Nov 2026` / `1 Nov every year`; None when not scheduled."""
    try:
        a, b = _as_date(design.get("sched_from")), _as_date(design.get("sched_to"))
        if a is None or b is None:
            return None
        yearly = bool(design.get("sched_yearly"))
        text = _fmt(a, yearly) if a == b else f"{_fmt(a, yearly)} to {_fmt(b, yearly)}"
        return text + (" every year" if yearly else "")
    except Exception:
        logger.debug("swallowed exception in describe", exc_info=True)
        return None
