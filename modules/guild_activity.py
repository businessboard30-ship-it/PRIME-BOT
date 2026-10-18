# path: modules/guild_activity.py

"""
Per-server activity tracker, used to put optional, purely social features to sleep in servers nobody uses.

A server is DORMANT when this bot has seen no message, command or voice activity there for DORMANT_DAYS (default 14).
A server the tracker has never seen is NOT dormant: the clock starts the first time the bot sees it (seed), so a
restart or a fresh deploy never puts anything to sleep by mistake. Any activity wakes the server at once.

Only features that are pointless without people may ask is_dormant() (cat spawns, Ship). Anything with a promise to
keep (giveaways, scheduled messages, anti-raid, verification, bump, reminders) must never ask.

Memory: one float per server. Stored as ONE JSON value per bot in bot_global_settings (no schema change), written
at most every 10 minutes and only when something changed. Fails open: any problem means 'not dormant'.
Switch off with GUILD_DORMANCY=0; threshold with GUILD_DORMANT_DAYS (1-365).
"""

import json
import logging
import os
import time
from typing import Iterable, Optional

logger = logging.getLogger(__name__)

TOUCH_RESOLUTION = 300          # activity is only re-stamped every 5 minutes per server
MAX_SERVERS = 50_000            # sanity cap for the stored blob


def enabled() -> bool:
    return os.getenv("GUILD_DORMANCY", "1") != "0"


def dormant_days() -> int:
    try:
        return min(365, max(1, int(os.getenv("GUILD_DORMANT_DAYS", "14"))))
    except ValueError:
        return 14


def setting_key(clone_id: Optional[int]) -> str:
    return f"guild_activity:{'main' if clone_id is None else clone_id}"


class ActivityTracker:
    def __init__(self, clock=time.time):
        self._clock = clock
        self._last: dict[int, float] = {}
        self.dirty = False

    def touch(self, guild_id: Optional[int]) -> None:
        if not guild_id:
            return
        now = self._clock()
        prev = self._last.get(guild_id)
        if prev is None or now - prev >= TOUCH_RESOLUTION:
            self._last[guild_id] = now
            self.dirty = True

    def seed(self, guild_ids: Iterable[int]) -> None:
        """Servers seen for the first time start their quiet-clock now."""
        now = self._clock()
        for gid in guild_ids:
            if gid not in self._last:
                self._last[gid] = now
                self.dirty = True

    def forget_missing(self, guild_ids: Iterable[int]) -> None:
        keep = set(guild_ids)
        gone = [g for g in self._last if g not in keep]
        for g in gone:
            del self._last[g]
        if gone:
            self.dirty = True

    def is_dormant(self, guild_id: int, days: Optional[int] = None) -> bool:
        try:
            if not enabled():
                return False
            last = self._last.get(guild_id)
            if last is None:
                return False
            return self._clock() - last > (days or dormant_days()) * 86400
        except Exception:
            return False

    def quiet_seconds(self, guild_id: int) -> Optional[float]:
        last = self._last.get(guild_id)
        return None if last is None else max(0.0, self._clock() - last)

    def dump(self) -> str:
        return json.dumps({str(g): int(t) for g, t in list(self._last.items())[:MAX_SERVERS]}, separators=(",", ":"))

    def load(self, raw: Optional[str]) -> None:
        try:
            data = json.loads(raw) if raw else {}
            for g, t in data.items():
                self._last.setdefault(int(g), float(t))
        except Exception:
            logger.warning("[guild-activity] stored activity unreadable, starting fresh")


tracker = ActivityTracker()


def is_dormant(guild_id: int) -> bool:
    return tracker.is_dormant(guild_id)
