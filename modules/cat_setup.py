# path: modules/cat_setup.py

"""
Per-server Cat setup: which channel(s) get automatic cats. Premium logic lives here.

  Free server:     1 spawn channel,   80 to 90 cats a day.
  Premium server:  up to 3 channels, 150 to 200 cats a day.

Each server's amount for a given UTC day is picked inside its tier's range (the same number all day, different
tomorrow, no storage needed) and the cats are spread evenly across the day. With several channels the cats rotate
between them, so the daily amount is for the whole server, not per channel.

A server's own setup is always respected: off means no cats, cats only appear in the channels the server picked
(and only where the bot can post), and the limits are enforced twice (the wizard stops a free server picking more
channels, and the spawner clamps whatever is stored to the plan the server has RIGHT NOW, so a lapsed Premium
server drops to the free amount by itself). `interval_minutes` is a legacy column from the first version and is
no longer used for pacing.
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Tier:
    name: str
    max_channels: int
    daily_min: int                      # cats per day, inclusive range
    daily_max: int


FREE = Tier("free", 1, 80, 90)
PREMIUM = Tier("premium", 3, 150, 200)
SECONDS_PER_DAY = 86400
GAP_JITTER = 0.15                       # +/-15% on each gap so servers don't all post on the same second


def tier_for(premium: bool) -> Tier:
    return PREMIUM if premium else FREE


def daily_target(guild_id: int, clone_id: Optional[int], tier: Tier, day: date) -> int:
    """How many cats this server gets on this UTC day: stable all day, inside the tier's range."""
    return random.Random(f"cat:{guild_id}:{-1 if clone_id is None else clone_id}:{day.isoformat()}").randint(
        tier.daily_min, tier.daily_max)


def gap_seconds(target: int) -> float:
    """Average seconds between two cats for a daily target."""
    return SECONDS_PER_DAY / max(1, int(target))


def clamp_channels(ids, tier: Tier) -> Tuple[int, ...]:
    seen: List[int] = []
    for i in ids:
        i = int(i)
        if i > 0 and i not in seen:
            seen.append(i)
    return tuple(seen[:tier.max_channels])


@dataclass(frozen=True)
class CatConfig:
    guild_id: int
    clone_id: Optional[int] = None
    enabled: bool = False
    channel_ids: Tuple[int, ...] = ()
    interval_minutes: int = 720         # legacy column, not used for pacing any more
    next_at: Optional[datetime] = None
    last_post_at: Optional[datetime] = None

    def effective(self, premium: bool, today: Optional[date] = None) -> Tuple[Tuple[int, ...], float]:
        """(channels, average seconds between cats) after applying the limits of the server's CURRENT plan."""
        tier = tier_for(premium)
        today = today or datetime.now(timezone.utc).date()
        return clamp_channels(self.channel_ids, tier), gap_seconds(daily_target(self.guild_id, self.clone_id, tier, today))

    @classmethod
    def from_row(cls, row) -> "CatConfig":
        return cls(
            guild_id=int(row["guild_id"]), clone_id=row["clone_id"], enabled=bool(row["enabled"]),
            channel_ids=tuple(int(c) for c in (row["channel_ids"] or ())),
            interval_minutes=int(row["interval_minutes"]),
            next_at=row["next_at"], last_post_at=row["last_post_at"])


def after_change(old: CatConfig, new: CatConfig, now: Optional[datetime] = None) -> CatConfig:
    """When the next cat is due after the owner changes something: turning it on (or giving a switched-on server
    its first channel) posts one on the next tick so they see it work."""
    now = now or datetime.now(timezone.utc)
    turned_on = new.enabled and not old.enabled
    first_channel = new.enabled and new.channel_ids and not old.channel_ids
    if turned_on or first_channel:
        return replace(new, next_at=now)
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
                (guild_id, clone_id, enabled, channel_ids, interval_minutes, next_at, updated_by, updated_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, NOW())
            ON CONFLICT (guild_id, (COALESCE(clone_id, -1))) DO UPDATE SET
                enabled = EXCLUDED.enabled, channel_ids = EXCLUDED.channel_ids,
                interval_minutes = EXCLUDED.interval_minutes, next_at = EXCLUDED.next_at,
                updated_by = EXCLUDED.updated_by, updated_at = NOW()
            """,
            cfg.guild_id, cfg.clone_id, cfg.enabled, list(cfg.channel_ids), cfg.interval_minutes,
            cfg.next_at, updated_by)


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


async def is_premium(guild_id: int, clone_id: Optional[int] = None) -> bool:
    try:
        from database import db
        return bool(await db.is_guild_premium_active(guild_id, clone_id))
    except Exception:
        return False
