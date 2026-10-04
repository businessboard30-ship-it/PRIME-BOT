"""
Join gate — pure decision logic (no Discord calls, no database), so it is easy to test.

A new member is caught when their Discord account is younger than the server's minimum age,
or (if switched on) they still have the default avatar. Bots are never checked.
Used by discord_bot/cogs/join_gate.py and shown in the Server Owners Panel.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional

ACTIONS = {
    "alert": "Alert staff only",
    "kick": "Kick and alert staff",
}
MIN_AGE_CHOICES = (1, 3, 7, 14, 30)   # days; the panel offers these in one select
MAX_AGE_DAYS = 365


def age_text(seconds: float) -> str:
    seconds = max(0, int(seconds))
    days, rem = divmod(seconds, 86400)
    if days >= 1:
        return f"{days} day{'s' if days != 1 else ''}"
    hours = rem // 3600
    if hours >= 1:
        return f"{hours} hour{'s' if hours != 1 else ''}"
    minutes = max(1, rem // 60)
    return f"{minutes} minute{'s' if minutes != 1 else ''}"


def reasons_for(created_at: datetime, has_custom_avatar: bool, cfg: dict,
                now: Optional[datetime] = None) -> List[str]:
    """Why this joiner should be caught (empty list = let them in)."""
    if not cfg.get("enabled"):
        return []
    now = now or datetime.now(timezone.utc)
    out: List[str] = []
    min_days = int(cfg.get("min_age_days") or 0)
    age = (now - created_at).total_seconds()
    if min_days > 0 and age < min_days * 86400:
        out.append(f"account is only {age_text(age)} old (minimum {min_days} day{'s' if min_days != 1 else ''})")
    if cfg.get("block_default_avatar") and not has_custom_avatar:
        out.append("no profile picture (default avatar)")
    return out


def validate_min_age(value) -> Optional[int]:
    """Whole days from 0 to MAX_AGE_DAYS, else None."""
    try:
        days = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return days if 0 <= days <= MAX_AGE_DAYS else None
