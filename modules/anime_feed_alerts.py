# path: modules/anime_feed_alerts.py

"""
Anime feed D2: personal alerts. Each follow has a mode: 'mention' (mentioned in the server post, the default), 'dm' (a private
message, opt-in only) or 'off' (still followed, no alert). Quiet hours hold DMs and drop a person from the mention line while
it is night for them. DMs are deduped in the database (one per person per episode) and a person whose DMs are closed is
switched back to 'mention' after DM_FAIL_LIMIT failures in a row. Pure rules are at the top (no database, no network).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

MODES = ("mention", "dm", "off")
DEFAULT_MODE = "mention"
DM_FAIL_LIMIT = 3                   # closed DMs in a row before DM alerts fall back to mention
MAX_DMS_PER_CYCLE = 60              # the DM queue is bounded per worker pass
DM_GAP = 1.0                        # seconds between two DMs
MAX_DM_RECIPIENTS = 2000            # per episode

# Quiet-hour presets offered in the panel: key -> (start hour, end hour), local time. None = off.
QUIET_PRESETS: Dict[str, Optional[Tuple[int, int]]] = {
    "off": None, "22-7": (22, 7), "23-8": (23, 8), "0-8": (0, 8), "1-9": (1, 9), "21-6": (21, 6),
}
TZ_CHOICES = tuple(range(-11, 14))  # 25 choices: a select menu holds at most 25 options


@dataclass(frozen=True)
class Prefs:
    tz: str = "UTC+0"
    quiet_start: Optional[int] = None
    quiet_end: Optional[int] = None
    dm_failures: int = 0
    fell_back: bool = False


def tz_label(hours: int) -> str:
    return f"UTC{'+' if hours >= 0 else '-'}{abs(hours)}"


def parse_tz(text: Optional[str]) -> Optional[int]:
    """'UTC+1' / 'UTC-5' -> whole hours; None for anything else (never guessed)."""
    m = re.fullmatch(r"UTC([+-])(\d{1,2})", str(text or "").strip().upper())
    if not m:
        return None
    hours = int(m.group(2)) * (1 if m.group(1) == "+" else -1)
    return hours if -12 <= hours <= 14 else None


def local_hour(tz: Optional[str], now_ts: int) -> int:
    off = parse_tz(tz) or 0
    return ((int(now_ts) // 3600) + off) % 24


def in_window(hour: int, start: Optional[int], end: Optional[int]) -> bool:
    """True when `hour` is inside [start, end) and the window may wrap midnight (22 -> 7). No window or an empty one = never."""
    if start is None or end is None or start == end:
        return False
    return start <= hour < end if start < end else (hour >= start or hour < end)


def is_quiet(prefs: Prefs, now_ts: int) -> bool:
    return in_window(local_hour(prefs.tz, now_ts), prefs.quiet_start, prefs.quiet_end)


def preset_key(prefs: Prefs) -> str:
    for k, v in QUIET_PRESETS.items():
        if v == ((prefs.quiet_start, prefs.quiet_end) if prefs.quiet_start is not None and prefs.quiet_end is not None else None):
            return k
    return "off"


def split_mentions(user_ids: List[int], prefs_by_user: Dict[int, Prefs], now_ts: int) -> List[int]:
    """People to mention in a server post: everyone who is not in their quiet hours (they still see the post)."""
    return [u for u in user_ids if not is_quiet(prefs_by_user.get(u, Prefs()), now_ts)]


# ── database ────────────────────────────────────────────────────────────

async def _pool():
    from database import get_pool
    return await get_pool()


def _wake() -> None:
    try:
        from modules import anime_feed_worker
        anime_feed_worker.wake_all()
    except Exception:
        logger.debug("[anime-feed] couldn't wake the worker", exc_info=True)


async def get_mode(user_id: int, anime_id: int) -> Optional[str]:
    """The follow's alert mode, or None when the person does not follow it."""
    pool = await _pool()
    async with pool.acquire() as conn:
        return await conn.fetchval("SELECT alert_mode FROM anime_follows WHERE user_id = $1 AND anime_id = $2", int(user_id), int(anime_id))


async def set_mode(user_id: int, anime_id: int, title: str, mode: str, limit: int) -> Tuple[str, int]:
    """Sets the alert mode, following the anime first when needed. ('ok' | 'limit', how many they follow).
    Same per-person advisory lock as toggle_follow, so quick taps can't slip past the follow limit."""
    if mode not in MODES:
        raise ValueError("unknown alert mode")
    pool = await _pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock($1)", int(user_id))
            n = int(await conn.fetchval("SELECT COUNT(*) FROM anime_follows WHERE user_id = $1", int(user_id)) or 0)
            if await conn.fetchval("UPDATE anime_follows SET alert_mode = $3 WHERE user_id = $1 AND anime_id = $2 RETURNING 1", int(user_id), int(anime_id), mode):
                state = "ok"
            elif n >= limit:
                return "limit", n
            else:
                await conn.execute("INSERT INTO anime_follows (user_id, anime_id, title, alert_mode) VALUES ($1, $2, $3, $4)", int(user_id), int(anime_id), str(title)[:120], mode)
                n, state = n + 1, "ok"
    if mode == "dm":
        _wake()                     # a DM alert is work for the worker: wake it so an idle worker starts
    return state, n


def _prefs(row) -> Prefs:
    if not row:
        return Prefs()
    return Prefs(str(row["tz"] or "UTC+0"), row["quiet_start"], row["quiet_end"], int(row["dm_failures"] or 0), bool(row["fell_back"]))


async def get_prefs(user_id: int) -> Prefs:
    pool = await _pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT tz, quiet_start, quiet_end, dm_failures, fell_back FROM anime_user_prefs WHERE user_id = $1", int(user_id))
    return _prefs(row)


async def get_prefs_many(user_ids: List[int]) -> Dict[int, Prefs]:
    if not user_ids:
        return {}
    pool = await _pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT user_id, tz, quiet_start, quiet_end, dm_failures, fell_back FROM anime_user_prefs WHERE user_id = ANY($1::bigint[])",
                                [int(u) for u in user_ids])
    return {int(r["user_id"]): _prefs(r) for r in rows}


async def set_tz(user_id: int, hours: int) -> None:
    pool = await _pool()
    async with pool.acquire() as conn:
        await conn.execute("INSERT INTO anime_user_prefs (user_id, tz) VALUES ($1, $2) ON CONFLICT (user_id) DO UPDATE SET tz = EXCLUDED.tz, updated_at = NOW()",
                           int(user_id), tz_label(int(hours)))


async def set_quiet(user_id: int, key: str) -> None:
    window = QUIET_PRESETS.get(key)
    start, end = window if window else (None, None)
    pool = await _pool()
    async with pool.acquire() as conn:
        await conn.execute("INSERT INTO anime_user_prefs (user_id, quiet_start, quiet_end) VALUES ($1, $2, $3) ON CONFLICT (user_id) DO UPDATE SET "
                           "quiet_start = EXCLUDED.quiet_start, quiet_end = EXCLUDED.quiet_end, updated_at = NOW()", int(user_id), start, end)


async def clear_fell_back(user_id: int) -> None:
    pool = await _pool()
    async with pool.acquire() as conn:
        await conn.execute("UPDATE anime_user_prefs SET fell_back = FALSE WHERE user_id = $1", int(user_id))


async def has_dm_followers() -> bool:
    """True when anybody has a DM alert. The worker counts this as work (so DM-only followers are served even when no server has the feed on).
    Raises on database errors (the worker backs off)."""
    pool = await _pool()
    async with pool.acquire() as conn:
        return bool(await conn.fetchval("SELECT EXISTS (SELECT 1 FROM anime_follows WHERE alert_mode = 'dm')"))


async def dm_followers(anime_id: int, limit: int = MAX_DM_RECIPIENTS) -> List[int]:
    pool = await _pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT user_id FROM anime_follows WHERE anime_id = $1 AND alert_mode = 'dm' ORDER BY followed_at LIMIT $2", int(anime_id), int(limit))
    return [int(r["user_id"]) for r in rows]


async def claim_alert(user_id: int, anime_id: int, episode: int) -> bool:
    """True for exactly one caller per (person, episode): the DM duplicate guard. Claim BEFORE sending."""
    pool = await _pool()
    async with pool.acquire() as conn:
        got = await conn.fetchval("INSERT INTO anime_alerts_sent (user_id, anime_id, episode) VALUES ($1, $2, $3) ON CONFLICT DO NOTHING RETURNING 1",
                                  int(user_id), int(anime_id), int(episode))
    return got is not None


async def release_alert(user_id: int, anime_id: int, episode: int) -> None:
    pool = await _pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM anime_alerts_sent WHERE user_id = $1 AND anime_id = $2 AND episode = $3", int(user_id), int(anime_id), int(episode))


async def note_dm_result(user_id: int, ok: bool) -> bool:
    """Counts a DM result. Returns True when this failure was the last straw and the person's DM alerts were switched back to 'mention'."""
    pool = await _pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            if ok:
                await conn.execute("UPDATE anime_user_prefs SET dm_failures = 0 WHERE user_id = $1 AND dm_failures <> 0", int(user_id))
                return False
            n = int(await conn.fetchval("INSERT INTO anime_user_prefs (user_id, dm_failures) VALUES ($1, 1) ON CONFLICT (user_id) DO UPDATE SET "
                                        "dm_failures = anime_user_prefs.dm_failures + 1, updated_at = NOW() RETURNING dm_failures", int(user_id)) or 0)
            if n < DM_FAIL_LIMIT:
                return False
            await conn.execute("UPDATE anime_follows SET alert_mode = 'mention' WHERE user_id = $1 AND alert_mode = 'dm'", int(user_id))
            await conn.execute("UPDATE anime_user_prefs SET dm_failures = 0, fell_back = TRUE WHERE user_id = $1", int(user_id))
            return True
