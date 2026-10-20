# path: modules/anime_alerts.py

"""
Anime feed, slice D2: personal alerts. Per followed anime a person picks ONE mode: mention (in the server post, the default),
dm (a private message when the episode airs) or mute (still followed, no alert). DM alerts are opt-in only, never sent to anyone
who didn't choose them, claimed in the database BEFORE sending (no double sends), skipped during quiet hours, and after 3 closed-DM
failures in a row the person falls back to mention mode. Pure rules first, then the small database layer.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Optional, Tuple

from modules import anime_feed as af

logger = logging.getLogger(__name__)

MENTION, DM, MUTE = "mention", "dm", "mute"
MODES = (MENTION, DM, MUTE)
MAX_DM_FAILURES = 3
MAX_DM_TARGETS = 500                     # people read per episode in one pass; the worker's per-cycle cap sends fewer

# UTC offsets people can pick (Discord selects hold at most 25 options): label -> minutes
TZ_CHOICES: List[Tuple[str, int]] = [
    ("UTC−10", -600), ("UTC−9", -540), ("UTC−8", -480), ("UTC−7", -420), ("UTC−6", -360), ("UTC−5", -300), ("UTC−4", -240),
    ("UTC−3", -180), ("UTC+0", 0), ("UTC+1", 60), ("UTC+2", 120), ("UTC+3", 180), ("UTC+3:30", 210), ("UTC+4", 240),
    ("UTC+5", 300), ("UTC+5:30", 330), ("UTC+6", 360), ("UTC+7", 420), ("UTC+8", 480), ("UTC+9", 540), ("UTC+9:30", 570),
    ("UTC+10", 600), ("UTC+11", 660), ("UTC+12", 720), ("UTC+13", 780)]
# quiet-hour windows (local start hour, local end hour)
QUIET_CHOICES: List[Tuple[str, int, int]] = [("22:00 to 07:00", 22, 7), ("23:00 to 07:00", 23, 7), ("00:00 to 08:00", 0, 8),
                                             ("21:00 to 06:00", 21, 6), ("01:00 to 09:00", 1, 9), ("13:00 to 15:00", 13, 15)]


@dataclass
class Target:
    user_id: int
    tz_minutes: int = 0
    quiet_start: Optional[int] = None
    quiet_end: Optional[int] = None


@dataclass
class Prefs:
    tz_minutes: int = 0
    quiet_start: Optional[int] = None
    quiet_end: Optional[int] = None


# ── pure rules ──────────────────────────────────────────────────────────

def local_hour(now_ts: float, tz_minutes: int) -> int:
    return int(((int(now_ts) + int(tz_minutes) * 60) % 86400) // 3600)


def in_quiet(now_ts: float, tz_minutes: int, start: Optional[int], end: Optional[int]) -> bool:
    """True when it is currently quiet for this person (their local hour inside [start, end), wrapping past midnight)."""
    if start is None or end is None or start == end:
        return False
    h = local_hour(now_ts, tz_minutes)
    return start <= h < end if start < end else (h >= start or h < end)


def valid_mode(mode: str) -> bool:
    return mode in MODES


def mode_label(mode: Optional[str]) -> str:
    return {MENTION: "🔔 Mention me in the server", DM: "📩 DM me", MUTE: "🔕 Mute (still following)"}.get(mode or "", "Not following")


# ── database ────────────────────────────────────────────────────────────

async def get_mode(user_id: int, anime_id: int) -> Optional[str]:
    pool = await af._pool()
    async with pool.acquire() as conn:
        return await conn.fetchval("SELECT alert_mode FROM anime_follows WHERE user_id = $1 AND anime_id = $2", int(user_id), int(anime_id))


async def set_mode(user_id: int, anime_id: int, title: str, mode: str, limit: int) -> Tuple[str, int]:
    """Choose the alert mode for an anime; picking one also follows it when they weren't. Returns ('set' | 'limit', follows now).
    One transaction under the same per-person lock as follow toggling, so two quick taps can't slip past the limit."""
    if not valid_mode(mode):
        raise ValueError(mode)
    pool = await af._pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock($1)", int(user_id))
            n = int(await conn.fetchval("SELECT COUNT(*) FROM anime_follows WHERE user_id = $1", int(user_id)) or 0)
            changed = await conn.fetchval("UPDATE anime_follows SET alert_mode = $3, alert_changed_at = NOW() WHERE user_id = $1 AND anime_id = $2 RETURNING 1",
                                          int(user_id), int(anime_id), mode)
            if changed:
                result = ("set", n)
            elif n >= limit:
                return "limit", n
            else:
                await conn.execute("INSERT INTO anime_follows (user_id, anime_id, title, alert_mode) VALUES ($1, $2, $3, $4)",
                                   int(user_id), int(anime_id), str(title)[:120], mode)
                result = ("set", n + 1)
    if mode == DM:
        _wake()
    return result


def _wake() -> None:
    try:
        from modules import anime_feed_worker
        anime_feed_worker.wake_all()
    except Exception:
        logger.debug("[anime-alerts] couldn't wake the worker", exc_info=True)


async def get_prefs(user_id: int) -> Prefs:
    try:
        pool = await af._pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT tz_minutes, quiet_start, quiet_end FROM anime_user_prefs WHERE user_id = $1", int(user_id))
    except Exception:
        logger.debug("[anime-alerts] couldn't read prefs", exc_info=True)
        row = None
    return Prefs(int(row["tz_minutes"]), row["quiet_start"], row["quiet_end"]) if row else Prefs()


async def save_prefs(user_id: int, prefs: Prefs) -> None:
    pool = await af._pool()
    async with pool.acquire() as conn:
        await conn.execute("INSERT INTO anime_user_prefs (user_id, tz_minutes, quiet_start, quiet_end) VALUES ($1, $2, $3, $4) "
                           "ON CONFLICT (user_id) DO UPDATE SET tz_minutes = EXCLUDED.tz_minutes, quiet_start = EXCLUDED.quiet_start, "
                           "quiet_end = EXCLUDED.quiet_end, updated_at = NOW()",
                           int(user_id), int(prefs.tz_minutes), prefs.quiet_start, prefs.quiet_end)


async def any_dm_alerts() -> bool:
    """Does anybody have a DM alert? The worker stays awake for them even when no server has the feed on. Raises on database errors."""
    pool = await af._pool()
    async with pool.acquire() as conn:
        return bool(await conn.fetchval("SELECT EXISTS (SELECT 1 FROM anime_follows WHERE alert_mode = 'dm')"))


async def dm_targets(anime_id: int, airing_at: int) -> List[Target]:
    """People who chose DM for this anime BEFORE the episode aired (never a backlog), with their quiet-hour settings."""
    pool = await af._pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT f.user_id, COALESCE(p.tz_minutes, 0) AS tz, p.quiet_start, p.quiet_end FROM anime_follows f "
            "LEFT JOIN anime_user_prefs p ON p.user_id = f.user_id WHERE f.anime_id = $1 AND f.alert_mode = 'dm' "
            "AND f.alert_changed_at <= to_timestamp($2) ORDER BY f.alert_changed_at LIMIT $3", int(anime_id), float(airing_at), MAX_DM_TARGETS)
    return [Target(int(r["user_id"]), int(r["tz"]), r["quiet_start"], r["quiet_end"]) for r in rows]


async def claim_dm(user_id: int, anime_id: int, episode: int) -> bool:
    pool = await af._pool()
    async with pool.acquire() as conn:
        got = await conn.fetchval("INSERT INTO anime_alerts_sent (user_id, anime_id, episode) VALUES ($1, $2, $3) ON CONFLICT DO NOTHING RETURNING 1",
                                  int(user_id), int(anime_id), int(episode))
    return got is not None


async def release_dm(user_id: int, anime_id: int, episode: int) -> None:
    pool = await af._pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM anime_alerts_sent WHERE user_id = $1 AND anime_id = $2 AND episode = $3", int(user_id), int(anime_id), int(episode))


async def note_dm_result(user_id: int, ok: bool) -> bool:
    """Record a DM outcome. A success resets the counter; the 3rd failure in a row moves their DM alerts back to mention mode and
    resets the counter. Returns True when that fallback just happened."""
    pool = await af._pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            if ok:
                await conn.execute("UPDATE anime_user_prefs SET dm_failures = 0 WHERE user_id = $1 AND dm_failures <> 0", int(user_id))
                return False
            n = int(await conn.fetchval("INSERT INTO anime_user_prefs (user_id, dm_failures) VALUES ($1, 1) ON CONFLICT (user_id) DO UPDATE SET "
                                        "dm_failures = anime_user_prefs.dm_failures + 1 RETURNING dm_failures", int(user_id)) or 0)
            if n < MAX_DM_FAILURES:
                return False
            await conn.execute("UPDATE anime_follows SET alert_mode = 'mention', alert_changed_at = NOW() WHERE user_id = $1 AND alert_mode = 'dm'", int(user_id))
            await conn.execute("UPDATE anime_user_prefs SET dm_failures = 0 WHERE user_id = $1", int(user_id))
            return True
