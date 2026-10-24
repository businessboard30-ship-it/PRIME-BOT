"""
Scam Shield, cheap sender risk check. Decides who gets the EXPENSIVE free checks (link-preview downloads, the extra OCR
passes). Everybody still gets the fast checks (text rules, hash rules, QR, first OCR read).

A sender is "risky" when ANY of these is true: the account is under ACCOUNT_DAYS old, they joined this server under
JOIN_DAYS ago, they still have the default avatar, or the message pings @everyone / @here. Old, established members
(the vast majority of traffic) are not. Missing data counts as NOT risky, so an odd message costs nothing.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
import logging

logger = logging.getLogger(__name__)

ACCOUNT_DAYS = 30
JOIN_DAYS = 7


def _age_days(dt) -> "float | None":
    try:
        if dt is None:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds() / 86400
    except Exception:
        logger.debug("swallowed exception in _age_days", exc_info=True)
        return None


def risky(message) -> bool:
    try:
        if getattr(message, "mention_everyone", False) is True:
            return True
        a = message.author
        age = _age_days(getattr(a, "created_at", None))
        if age is not None and age < ACCOUNT_DAYS:
            return True
        joined = _age_days(getattr(a, "joined_at", None))
        if joined is not None and joined < JOIN_DAYS:
            return True
        if hasattr(a, "avatar") and a.avatar is None:
            return True
    except Exception:
        logger.debug("swallowed exception in risky", exc_info=True)
        return False
    return False


# ── throttle for the expensive checks ────────────────────────────────────
USER_GAP = 10.0                  # seconds between expensive checks for one user
GLOBAL_PER_MINUTE = 30           # expensive checks per minute across the whole bot
_user_last: dict = {}
_window: list = []


def allow(user_id: int) -> bool:
    """True if this user may have an expensive check now (and records it). Bounded memory."""
    now = time.monotonic()
    if len(_user_last) > 2000:
        for k in [k for k, v in _user_last.items() if now - v > USER_GAP]:
            _user_last.pop(k, None)
    if now - _user_last.get(user_id, -1e9) < USER_GAP:
        return False
    while _window and now - _window[0] > 60:
        _window.pop(0)
    if len(_window) >= GLOBAL_PER_MINUTE:
        return False
    _user_last[user_id] = now
    _window.append(now)
    return True
