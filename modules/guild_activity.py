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

SIZE MATTERS: a 10-member server that goes quiet has little to lose, a big server almost always has someone talking.
When the caller passes the server's member count, the quiet time that counts as dormant scales with it (SIZE_TIERS:
3 days up to 20 members, 7 days up to 200, 14 days above). Activity, not size, is still what decides: a small server
that keeps chatting never sleeps. GUILD_DORMANT_DAYS, when set, is one flat value for every size (the old behaviour).
Calls that don't pass a member count keep the flat default, so nothing changes for them.

DROWSY: before a server is dormant it goes DROWSY -- quiet for a third of its dormant time (1 day for a small server,
about 2 for a mid one, 4-5 for a big one). A drowsy server is not switched off; its NETWORK-heavy polling is only
slowed (one poll in DROWSY_EVERY, see quiet_poll_due), so nothing is ever lost, just fetched less often. Used by the
YouTube / Roblox feed polls, which fetch from outside once per feed. Any activity makes the server fully awake again
and the very next poll runs at the normal pace.
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


DROWSY_FRACTION = 1 / 3         # of the dormant time
DROWSY_EVERY = 4                # a drowsy server's polls run one cycle in this many
SIZE_TIERS = ((20, 3), (200, 7))      # (up to this many members, days of quiet) -- anything bigger uses the default
DEFAULT_DAYS = 14


def dormant_days_for(member_count: Optional[int] = None) -> int:
    """Days of quiet after which a server of this size is dormant. GUILD_DORMANT_DAYS (flat) wins when it is set."""
    if os.getenv("GUILD_DORMANT_DAYS"):
        return dormant_days()
    try:
        n = int(member_count) if member_count is not None else None
    except (TypeError, ValueError):
        n = None
    if not n or n < 0:
        return DEFAULT_DAYS
    for limit, days in SIZE_TIERS:
        if n <= limit:
            return days
    return DEFAULT_DAYS


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

    def is_dormant(self, guild_id: int, days: Optional[int] = None, member_count: Optional[int] = None) -> bool:
        try:
            if not enabled():
                return False
            last = self._last.get(guild_id)
            if last is None:
                return False
            limit = days or (dormant_days_for(member_count) if member_count else dormant_days())
            return self._clock() - last > limit * 86400
        except Exception:
            logger.debug("swallowed exception in ActivityTracker.is_dormant", exc_info=True)
            return False

    def is_quiet(self, guild_id: int, member_count: Optional[int] = None) -> bool:
        """Drowsy OR dormant: quiet for at least a third of the dormant time. Fails open (False) on any problem."""
        try:
            if not enabled():
                return False
            last = self._last.get(guild_id)
            if last is None:
                return False
            limit = dormant_days_for(member_count) if member_count else dormant_days()
            return self._clock() - last > limit * 86400 * DROWSY_FRACTION
        except Exception:
            logger.debug("swallowed exception in ActivityTracker.is_quiet", exc_info=True)
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


def is_dormant(guild_id: int, member_count: Optional[int] = None) -> bool:
    return tracker.is_dormant(guild_id, member_count=member_count)


class CycleThrottle:
    """Lets a quiet job through one cycle in `every` (the first one at once). Per key, in memory only."""

    def __init__(self, every: int = DROWSY_EVERY):
        self.every = max(1, every)
        self._n: dict = {}

    def should_run(self, key) -> bool:
        n = self._n.get(key, 0)
        self._n[key] = n + 1
        return n % self.every == 0

    def reset(self, key) -> None:
        self._n.pop(key, None)


_throttle = CycleThrottle()


def quiet_poll_due(key, quiet: bool) -> bool:
    """True when this poll should run. A job that isn't quiet always runs (and its counter restarts, so the first
    poll after the server wakes up is immediate); a quiet one runs one cycle in DROWSY_EVERY."""
    if not quiet:
        _throttle.reset(key)
        return True
    return _throttle.should_run(key)
