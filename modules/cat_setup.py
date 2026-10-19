# path: modules/cat_setup.py

"""
Per-server Cat setup: which channel(s) get automatic cats. Premium logic lives here.

  Free server:     1 spawn channel,   120 to 140 cats a day.
  Premium server:  up to 3 channels, 250 to 300 cats a day.

Each server's amount for a given UTC day is picked inside its tier's range (the same number all day, different
tomorrow, no storage needed) and the cats are spread evenly across the day. With several channels the cats rotate
between them, so the daily amount is for the whole server, not per channel.

A server's own setup is always respected: off means no cats, cats only appear in the channels the server picked
(and only where the bot can post), and the limits are enforced twice (the wizard stops a free server picking more
channels, and the spawner clamps whatever is stored to the plan the server has RIGHT NOW, so a lapsed Premium
server drops to the free amount by itself). `interval_minutes` is a legacy column from the first version and is
no longer used for pacing. A server may choose FEWER cats (`pace_percent`: 75 / 50 / 25 / 10 % of its plan amount);
never more than the plan.
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Tier:
    name: str
    max_channels: int
    daily_min: int                      # cats per day, inclusive range
    daily_max: int


FREE = Tier("free", 1, 120, 140)
PREMIUM = Tier("premium", 3, 250, 300)
SECONDS_PER_DAY = 86400
GAP_JITTER = 0.15                       # +/-15% on each gap so servers don't all post on the same second


def tier_for(premium: bool) -> Tier:
    return PREMIUM if premium else FREE


def daily_target(guild_id: int, clone_id: Optional[int], tier: Tier, day: date) -> int:
    """How many cats this server gets on this UTC day: stable all day, inside the tier's range."""
    return random.Random(f"cat:{guild_id}:{-1 if clone_id is None else clone_id}:{day.isoformat()}").randint(
        tier.daily_min, tier.daily_max)


PACES = (100, 75, 50, 25, 10)            # % of the plan's daily amount a server can choose (never more than 100)


def clamp_pace(value) -> int:
    """Nearest allowed pace that is not higher than asked; junk or None means the full amount."""
    try:
        v = int(value)
    except (TypeError, ValueError):
        return 100
    lower = [p for p in PACES if p <= v]
    return max(lower) if lower else min(PACES)


def pace_target(target: int, pace: int) -> int:
    return max(1, round(int(target) * clamp_pace(pace) / 100))


def gap_seconds(target: int) -> float:
    """Average seconds between two cats for a daily target."""
    return SECONDS_PER_DAY / max(1, int(target))


def clamp_hour(value, default: int) -> int:
    try:
        v = int(value)
    except (TypeError, ValueError):
        return default
    return v if 0 <= v <= 24 else default


def window_hours(start: int, end: int) -> int:
    """Length in hours of the daily window [start, end) in UTC. start == end (or 0/24) means the whole day."""
    start, end = clamp_hour(start, 0) % 24, clamp_hour(end, 24)
    if end == 24 and start == 0:
        return 24
    length = (end - start) % 24
    return length or 24


def in_window(start: int, end: int, now: Optional[datetime] = None) -> bool:
    now = now or datetime.now(timezone.utc)
    if window_hours(start, end) == 24:
        return True
    start = clamp_hour(start, 0) % 24
    return (now.hour - start) % 24 < window_hours(start, end)


def next_window_start(start: int, end: int, now: Optional[datetime] = None) -> datetime:
    """The next moment (UTC) the window opens; `now` itself if it is already open."""
    now = now or datetime.now(timezone.utc)
    if in_window(start, end, now):
        return now
    s = clamp_hour(start, 0) % 24
    opens = now.replace(hour=s, minute=0, second=0, microsecond=0)
    return opens if opens > now else opens + timedelta(days=1)


def clamp_channels(ids, tier: Tier) -> Tuple[int, ...]:
    seen: List[int] = []
    for i in ids:
        i = int(i)
        if i > 0 and i not in seen:
            seen.append(i)
    return tuple(seen[:tier.max_channels])


ANNOUNCE_MODES = ("all", "milestones", "off")      # public cat posts: everything / only big moments / nothing


def clamp_announce(value) -> str:
    return value if value in ANNOUNCE_MODES else "all"


@dataclass(frozen=True)
class CatConfig:
    guild_id: int
    clone_id: Optional[int] = None
    enabled: bool = False
    channel_ids: Tuple[int, ...] = ()
    interval_minutes: int = 720         # legacy column, not used for pacing any more
    pace_percent: int = 100             # the server's own choice: 100 = full plan amount, less = fewer cats
    active_start: int = 0               # spawn timing: cats only appear from this UTC hour ...
    active_end: int = 24                # ... up to this one (0/24 = all day). The daily amount is unchanged.
    next_at: Optional[datetime] = None
    last_post_at: Optional[datetime] = None
    announce_mode: str = "all"          # see ANNOUNCE_MODES
    fights_cross_server: bool = False   # cat fights: this server's board posts may be answered from other servers (default OFF)
    fights_show_server: bool = False    # cat fights: show this server's name on the public board (default OFF)

    def effective(self, premium: bool, today: Optional[date] = None) -> Tuple[Tuple[int, ...], float]:
        """(channels, average seconds between cats) after applying the limits of the server's CURRENT plan."""
        tier = tier_for(premium)
        today = today or datetime.now(timezone.utc).date()
        target = pace_target(daily_target(self.guild_id, self.clone_id, tier, today), self.pace_percent)
        # the same daily amount is spread over the server's active window only (a shorter window = shorter gaps)
        window = SECONDS_PER_DAY * window_hours(self.active_start, self.active_end) // 24
        return clamp_channels(self.channel_ids, tier), max(1.0, window / max(1, target))

    def is_open(self, now: Optional[datetime] = None) -> bool:
        return in_window(self.active_start, self.active_end, now)

    @classmethod
    def from_row(cls, row) -> "CatConfig":
        return cls(
            guild_id=int(row["guild_id"]), clone_id=row["clone_id"], enabled=bool(row["enabled"]),
            channel_ids=tuple(int(c) for c in (row["channel_ids"] or ())),
            interval_minutes=int(row["interval_minutes"]),
            pace_percent=clamp_pace(row["pace_percent"] if "pace_percent" in row.keys() else 100),
            active_start=clamp_hour(row["active_start"] if "active_start" in row.keys() else 0, 0) % 24,
            active_end=clamp_hour(row["active_end"] if "active_end" in row.keys() else 24, 24),
            next_at=row["next_at"], last_post_at=row["last_post_at"],
            announce_mode=clamp_announce(row["announce_mode"] if "announce_mode" in row.keys() else "all"),
            fights_cross_server=bool(row["fights_cross_server"]) if "fights_cross_server" in row.keys() else False,
            fights_show_server=bool(row["fights_show_server"]) if "fights_show_server" in row.keys() else False)


def after_change(old: CatConfig, new: CatConfig, now: Optional[datetime] = None) -> CatConfig:
    """When the next cat is due after the owner changes something: turning it on (or giving a switched-on server
    its first channel) posts one on the next tick so they see it work."""
    now = now or datetime.now(timezone.utc)
    turned_on = new.enabled and not old.enabled
    first_channel = new.enabled and new.channel_ids and not old.channel_ids
    if turned_on or first_channel:
        return replace(new, next_at=now)
    if (new.active_start, new.active_end) != (old.active_start, old.active_end):
        return replace(new, next_at=None)       # re-evaluated on the next tick against the new hours
    return new


async def _pool():
    from database import get_pool        # lazy: keeps this module importable in tests
    return await get_pool()


def _key(clone_id) -> int:
    return -1 if clone_id is None else int(clone_id)


async def get_config(guild_id: int, clone_id: Optional[int] = None) -> CatConfig:
    try:
        pool = await _pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM cat_guild_config WHERE guild_id = $1 AND COALESCE(clone_id, -1) = $2",
                guild_id, _key(clone_id))
    except Exception:
        logger.exception("[cat] couldn't read the setup for guild %s", guild_id)
        row = None
    return CatConfig.from_row(row) if row else CatConfig(guild_id=guild_id, clone_id=clone_id)


async def save_config(cfg: CatConfig, updated_by: Optional[int] = None) -> None:
    pool = await _pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO cat_guild_config
                (guild_id, clone_id, enabled, channel_ids, interval_minutes, next_at, updated_by, pace_percent,
                 active_start, active_end, announce_mode, fights_cross_server, fights_show_server, updated_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, NOW())
            ON CONFLICT (guild_id, (COALESCE(clone_id, -1))) DO UPDATE SET
                enabled = EXCLUDED.enabled, channel_ids = EXCLUDED.channel_ids,
                interval_minutes = EXCLUDED.interval_minutes, next_at = EXCLUDED.next_at,
                updated_by = EXCLUDED.updated_by, pace_percent = EXCLUDED.pace_percent,
                active_start = EXCLUDED.active_start, active_end = EXCLUDED.active_end,
                announce_mode = EXCLUDED.announce_mode, fights_cross_server = EXCLUDED.fights_cross_server,
                fights_show_server = EXCLUDED.fights_show_server, updated_at = NOW()
            """,
            cfg.guild_id, cfg.clone_id, cfg.enabled, list(cfg.channel_ids), cfg.interval_minutes,
            cfg.next_at, updated_by, clamp_pace(cfg.pace_percent),
            clamp_hour(cfg.active_start, 0) % 24, clamp_hour(cfg.active_end, 24), clamp_announce(cfg.announce_mode),
            bool(cfg.fights_cross_server), bool(cfg.fights_show_server))


async def due(clone_id: Optional[int], limit: int = 10) -> List[CatConfig]:
    """Servers (of THIS bot: main or one clone) whose next cat is due."""
    pool = await _pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM cat_guild_config WHERE enabled AND COALESCE(clone_id, -1) = $1 "
            "AND (next_at IS NULL OR next_at <= NOW()) ORDER BY next_at NULLS FIRST LIMIT $2",
            _key(clone_id), limit)
    return [CatConfig.from_row(r) for r in rows]


async def claim(guild_id: int, clone_id: Optional[int], gap: float) -> bool:
    """Atomically take this server's turn and schedule the next one. False = someone else already did.
    The next time is counted from when this cat was DUE (not from now), so a late tick doesn't make a server
    drift below its daily amount; a server that fell far behind (bot was down) restarts from now instead of
    bursting to catch up."""
    pool = await _pool()
    async with pool.acquire() as conn:
        got = await conn.fetchval(
            "UPDATE cat_guild_config SET "
            "next_at = CASE WHEN next_at IS NULL OR next_at < NOW() - make_interval(secs => $3) "
            "THEN NOW() + make_interval(secs => $3) ELSE next_at + make_interval(secs => $3) END, "
            "last_post_at = NOW() "
            "WHERE guild_id = $1 AND COALESCE(clone_id, -1) = $2 AND enabled "
            "AND (next_at IS NULL OR next_at <= NOW()) RETURNING guild_id",
            guild_id, _key(clone_id), float(gap))
    return got is not None


def upcoming(cfg: "CatConfig", next_at: Optional[datetime], now: Optional[datetime] = None) -> Optional[datetime]:
    """When the server's next cat will actually appear: the scheduled time, pushed to the next opening of the
    server's spawn hours if that time falls outside them. None when nothing is scheduled."""
    if next_at is None:
        return None
    if next_at.tzinfo is None:
        next_at = next_at.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    when = max(next_at, now)
    return next_window_start(cfg.active_start, cfg.active_end, when)


async def get_next_at(guild_id: int, clone_id: Optional[int]) -> Optional[datetime]:
    """The next scheduled cat time of a server (set by claim), or None. Never raises."""
    try:
        pool = await _pool()
        async with pool.acquire() as conn:
            return await conn.fetchval(
                "SELECT next_at FROM cat_guild_config WHERE guild_id = $1 AND COALESCE(clone_id, -1) = $2",
                guild_id, _key(clone_id))
    except Exception:
        return None


async def defer(guild_id: int, clone_id: Optional[int], until: datetime) -> None:
    """Outside the server's active hours: nothing is posted, just try again when the window opens."""
    pool = await _pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE cat_guild_config SET next_at = $3 WHERE guild_id = $1 AND COALESCE(clone_id, -1) = $2",
            guild_id, _key(clone_id), until)


async def is_premium(guild_id: int, clone_id: Optional[int] = None) -> bool:
    try:
        from database import db
        return bool(await db.is_guild_premium_active(guild_id, clone_id))
    except Exception:
        return False
