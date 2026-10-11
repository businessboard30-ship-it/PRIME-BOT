# path: modules/cat_setup.py

"""
Per-server Cat setup: which channel(s) get an automatic cat, and how often. Premium logic lives here.

  Free server:     1 spawn channel,  a cat every 6, 12 or 24 hours.
  Premium server:  up to 3 spawn channels, a cat every 15 / 30 minutes, 1 / 2 / 3 / 6 / 12 / 24 hours.

The limits are enforced twice: the setup wizard stops a free server from picking more (and says why), and the
spawner clamps whatever is stored to the tier the server has RIGHT NOW, so a server whose Premium ended slows
down by itself without anyone having to edit anything.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Tier:
    name: str
    max_channels: int
    intervals: Tuple[int, ...]          # minutes
    default_interval: int


FREE = Tier("free", 1, (360, 720, 1440), 720)
PREMIUM = Tier("premium", 3, (15, 30, 60, 120, 180, 360, 720, 1440), 60)
ALL_INTERVALS = PREMIUM.intervals


def tier_for(premium: bool) -> Tier:
    return PREMIUM if premium else FREE


def interval_label(minutes: int) -> str:
    if minutes < 60:
        return f"{minutes} minutes"
    hours = minutes // 60
    return "1 hour" if hours == 1 else f"{hours} hours"


def clamp_interval(minutes: int, tier: Tier) -> int:
    """The tier's own allowed value closest to (and not faster than) what was asked for."""
    if minutes in tier.intervals:
        return minutes
    slower = [m for m in tier.intervals if m >= minutes]
    return min(slower) if slower else max(tier.intervals)


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
    interval_minutes: int = FREE.default_interval
    next_at: Optional[datetime] = None
    last_post_at: Optional[datetime] = None

    def effective(self, premium: bool) -> Tuple[Tuple[int, ...], int]:
        """(channels, minutes between cats) after applying the limits of the server's CURRENT plan."""
        tier = tier_for(premium)
        return clamp_channels(self.channel_ids, tier), clamp_interval(self.interval_minutes, tier)

    @classmethod
    def from_row(cls, row) -> "CatConfig":
        return cls(
            guild_id=int(row["guild_id"]), clone_id=row["clone_id"], enabled=bool(row["enabled"]),
            channel_ids=tuple(int(c) for c in (row["channel_ids"] or ())),
            interval_minutes=int(row["interval_minutes"]),
            next_at=row["next_at"], last_post_at=row["last_post_at"])


def after_change(old: CatConfig, new: CatConfig, now: Optional[datetime] = None) -> CatConfig:
    """When the next cat is due after the owner changes something: turning it on (or giving a switched-on server
    its first channel) posts one on the next tick so they see it work; a new interval never makes a cat wait
    longer than that interval."""
    now = now or datetime.now(timezone.utc)
    turned_on = new.enabled and not old.enabled
    first_channel = new.enabled and new.channel_ids and not old.channel_ids
    if turned_on or first_channel:
        return replace(new, next_at=now)
    if new.interval_minutes != old.interval_minutes:
        cap = now + timedelta(minutes=new.interval_minutes)
        return replace(new, next_at=min(new.next_at or cap, cap))
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


async def claim(guild_id: int, clone_id: Optional[int], interval_minutes: int) -> bool:
    """Atomically take this server's turn and schedule the next one. False = someone else already did."""
    pool = await _pool()
    async with pool.acquire() as conn:
        got = await conn.fetchval(
            "UPDATE cat_guild_config SET next_at = NOW() + make_interval(mins => $3), last_post_at = NOW() "
            "WHERE guild_id = $1 AND COALESCE(clone_id, -1) = $2 AND enabled "
            "AND (next_at IS NULL OR next_at <= NOW()) RETURNING guild_id",
            guild_id, _key(clone_id), int(interval_minutes))
    return got is not None


async def is_premium(guild_id: int, clone_id: Optional[int] = None) -> bool:
    try:
        from database import db
        return bool(await db.is_guild_premium_active(guild_id, clone_id))
    except Exception:
        return False
