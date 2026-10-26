"""Groq key pool: loading, round-robin ordering and short cooldowns after failures.

Keys come from GROQ_API_KEY, GROQ_API_KEY_BACKUP, GROQ_API_KEY_BACKUP2 and an optional
GROQ_API_KEYS (comma, space or newline separated) for any number of extra keys.
De-duplicated, blanks ignored, order kept (primary first).

ordered_keys() spreads traffic across the keys instead of always hammering the first one,
and puts a key that just failed (429 / auth / 5xx) at the back for a short time so requests
do not waste a round trip on a key that is known to be limited. Cooled-down keys are still
tried last, so a request is never refused just because every key is cooling.
"""
from __future__ import annotations

import itertools
import os
import re
import time
from typing import Dict, List, Optional

_COOLDOWN_SECONDS = {401: 600, 403: 600, 429: 60}
_DEFAULT_COOLDOWN = 15            # 5xx, timeouts and network errors

_cooling: Dict[str, float] = {}   # key -> monotonic time until which it is deprioritised
_counter = itertools.count()


def load_keys() -> List[str]:
    extra = re.split(r"[,\s]+", os.getenv("GROQ_API_KEYS", "") or "")
    names = ("GROQ_API_KEY", "GROQ_API_KEY_BACKUP", "GROQ_API_KEY_BACKUP2")
    raw = [(os.getenv(n, "") or "").strip() for n in names] + [k.strip() for k in extra]
    return list(dict.fromkeys(k for k in raw if k))


def ordered_keys(keys: List[str]) -> List[str]:
    """Healthy keys first, rotated so each request starts on a different one; cooling keys last."""
    if len(keys) < 2:
        return list(keys)
    now = time.monotonic()
    healthy = [k for k in keys if _cooling.get(k, 0.0) <= now]
    cooling = [k for k in keys if _cooling.get(k, 0.0) > now]
    if healthy:
        shift = next(_counter) % len(healthy)
        healthy = healthy[shift:] + healthy[:shift]
    return healthy + cooling


def mark_failed(key: str, status: Optional[int]) -> None:
    _cooling[key] = time.monotonic() + _COOLDOWN_SECONDS.get(status or 0, _DEFAULT_COOLDOWN)


def mark_ok(key: str) -> None:
    _cooling.pop(key, None)


class LiveKeys(list):
    """A list of Groq keys that follows the environment. Every module that did `from ... import GROQ_API_KEYS` keeps
    working, but truthiness and iteration re-read the keys at call time, so a key saved in the owner form (or changed in
    Railway) is used without a restart. An empty pool also DMs the owner (modules/owner_env.py)."""

    def _sync(self) -> None:
        fresh = load_keys()
        if fresh != list(self):
            self[:] = fresh
        try:
            from modules import owner_env as oe
            oe.kick_refresh()
            if not fresh:
                oe.note_missing("GROQ_API_KEY", "AI chat")
        except Exception:
            pass

    def __bool__(self):
        self._sync()
        return list.__len__(self) > 0

    def __iter__(self):
        self._sync()
        return list.__iter__(self)

    def __len__(self):
        self._sync()
        return list.__len__(self)


def live_keys() -> LiveKeys:
    return LiveKeys(load_keys())
