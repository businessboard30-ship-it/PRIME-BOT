"""One shared rate budget + pause for the AI scam scan, across the main bot and EVERY clone.

Why: all bot processes use the same Gemini key (one quota), but scam_vision.py counted its per-minute / per-day budget and its
"paused after a 429" flag inside each process. N processes therefore sent up to N x the allowed rate, and after the daily quota
was gone every process retried every 60 s forever (a constant stream of 429s). Now:
  * acquire(rpm, daily): one atomic slot counter in the database (per-minute window + per-day total).
  * block(seconds): after a 429/5xx the whole fleet pauses; repeated failures back off 1m -> 2m -> 5m -> 15m -> 1h (a Retry-After
    header is honoured); a success resets it.
Table is created on first use (no migration / schema bump). If the database can't be reached everything falls back to the old
per-process limits: the scan never breaks because of this layer. Pure helpers are separate so they can be tested without a database.
"""
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

BACKOFF = (60, 120, 300, 900, 3600)       # seconds, by number of failures in a row
_DDL = ("CREATE TABLE IF NOT EXISTS scam_vision_shared (id SMALLINT PRIMARY KEY DEFAULT 1 CHECK (id = 1), "
        "blocked_until TIMESTAMPTZ, fail_count INT NOT NULL DEFAULT 0, day TEXT NOT NULL DEFAULT '', day_count INT NOT NULL DEFAULT 0, "
        "win_start TIMESTAMPTZ, win_count INT NOT NULL DEFAULT 0)")
_BLOCK_CACHE = (0.0, 0.0)                 # (checked at monotonic, blocked_until as unix time)
CHECK_EVERY = 5.0


def backoff_seconds(fail_count: int, retry_after: Optional[float] = None) -> int:
    """Pause length after the `fail_count`-th failure in a row. A server-sent Retry-After wins when it is longer (capped at 1 h)."""
    base = BACKOFF[min(max(fail_count, 1), len(BACKOFF)) - 1]
    if retry_after and retry_after > 0:
        base = max(base, int(min(retry_after, 3600)))
    return base


def parse_retry_after(value: Optional[str]) -> Optional[float]:
    try:
        return float(value) if value is not None and str(value).strip() else None
    except ValueError:
        return None


def decide(state: dict, now: datetime, rpm: int, daily: int) -> Tuple[bool, dict, str]:
    """Pure core of acquire(): (allowed, new_state, reason). `state` has blocked_until, day, day_count, win_start, win_count."""
    s = dict(state)
    if s.get("blocked_until") and now < s["blocked_until"]:
        return False, s, "paused"
    if s.get("win_start") is None or now - s["win_start"] >= timedelta(seconds=60):
        s["win_start"], s["win_count"] = now, 0
    if s["win_count"] >= rpm:
        return False, s, "rpm"
    today = now.strftime("%Y-%m-%d")
    if s.get("day") != today:
        s["day"], s["day_count"] = today, 0
    if s["day_count"] >= daily:
        return False, s, "daily"
    s["win_count"] += 1
    s["day_count"] += 1
    return True, s, "ok"


async def _pool():
    from database import get_pool
    return await get_pool()


async def _load(conn) -> dict:
    await conn.execute(_DDL)
    await conn.execute("INSERT INTO scam_vision_shared (id) VALUES (1) ON CONFLICT (id) DO NOTHING")
    row = await conn.fetchrow("SELECT blocked_until, fail_count, day, day_count, win_start, win_count FROM scam_vision_shared WHERE id = 1 FOR UPDATE")
    return dict(row)


async def acquire(rpm: int, daily: int, now: Optional[datetime] = None) -> Tuple[bool, str]:
    """Take one call slot from the shared budget. (True, 'ok') or (False, why). Any database trouble -> (True, 'local'):
    the caller's own per-process limits still apply."""
    now = now or datetime.now(timezone.utc)
    try:
        pool = await _pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                state = await _load(conn)
                ok, new, why = decide(state, now, rpm, daily)
                if ok or new != state:
                    await conn.execute("UPDATE scam_vision_shared SET day = $1, day_count = $2, win_start = $3, win_count = $4 WHERE id = 1",
                                       new["day"], new["day_count"], new["win_start"], new["win_count"])
                return ok, why
    except Exception:
        return True, "local"


async def block(retry_after: Optional[float] = None, now: Optional[datetime] = None) -> int:
    """Pause the AI scan for every bot after a 429 / server error. Returns the pause in seconds."""
    global _BLOCK_CACHE
    now = now or datetime.now(timezone.utc)
    try:
        pool = await _pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                state = await _load(conn)
                still = state.get("blocked_until") and now < state["blocked_until"]
                fails = int(state.get("fail_count") or 0) + (0 if still else 1)     # many bots failing at once count as ONE failure
                secs = backoff_seconds(fails, retry_after)
                until = max(now + timedelta(seconds=secs), state["blocked_until"] or now) if still else now + timedelta(seconds=secs)
                await conn.execute("UPDATE scam_vision_shared SET blocked_until = $1, fail_count = $2 WHERE id = 1", until, fails)
        _BLOCK_CACHE = (time.monotonic(), until.timestamp())
        return secs
    except Exception:
        return backoff_seconds(1, retry_after)


async def blocked_for(now: Optional[datetime] = None) -> float:
    """Seconds left on the fleet-wide pause (0 = not paused). Cached 5 s so every image doesn't hit the database."""
    global _BLOCK_CACHE
    checked, until = _BLOCK_CACHE
    if time.monotonic() - checked < CHECK_EVERY:
        return max(0.0, until - time.time())
    try:
        pool = await _pool()
        async with pool.acquire() as conn:
            await conn.execute(_DDL)
            row = await conn.fetchrow("SELECT blocked_until FROM scam_vision_shared WHERE id = 1")
        until = row["blocked_until"].timestamp() if row and row["blocked_until"] else 0.0
        _BLOCK_CACHE = (time.monotonic(), until)
    except Exception:
        return max(0.0, until - time.time())
    return max(0.0, until - time.time())


async def success() -> None:
    """A call worked: clear the failure streak so the next hiccup starts from a short pause again."""
    global _BLOCK_CACHE
    try:
        pool = await _pool()
        async with pool.acquire() as conn:
            await conn.execute(_DDL)
            await conn.execute("UPDATE scam_vision_shared SET fail_count = 0 WHERE id = 1 AND fail_count <> 0")
    except Exception:
        pass


async def clear_block() -> None:
    """The owner's 'Test AI scan' worked: lift the fleet-wide pause and the failure streak."""
    global _BLOCK_CACHE
    _BLOCK_CACHE = (time.monotonic(), 0.0)
    try:
        pool = await _pool()
        async with pool.acquire() as conn:
            await conn.execute(_DDL)
            await conn.execute("UPDATE scam_vision_shared SET blocked_until = NULL, fail_count = 0 WHERE id = 1")
    except Exception:
        pass


# ── survive a restart ──────────────────────────────────────────────────────
# Per-server daily check counts and the dHashes of images already flagged used to live only in memory: every deploy reset every
# server's daily cap (more API calls, more 429s) and made the scanner forget scams it had already learned. They are saved now and
# loaded once per process. Same rules as above: tables created on first use, any database trouble is ignored (memory still works).
_USAGE_DDL = ("CREATE TABLE IF NOT EXISTS scam_vision_usage (day TEXT NOT NULL, guild_id BIGINT NOT NULL, clone_key BIGINT NOT NULL DEFAULT -1, "
              "gemini_used INT NOT NULL DEFAULT 0, openai_used INT NOT NULL DEFAULT 0, PRIMARY KEY (day, guild_id, clone_key))")
_LEARNED_DDL = ("CREATE TABLE IF NOT EXISTS scam_vision_learned (dhash BIGINT PRIMARY KEY, learned_at TIMESTAMPTZ NOT NULL DEFAULT NOW())")
LEARNED_KEEP = 300
_loaded = False


def to_db_hash(h: int) -> int:
    """dHashes are unsigned 64-bit; Postgres BIGINT is signed."""
    return h - (1 << 64) if h >= (1 << 63) else h


def from_db_hash(h: int) -> int:
    return h + (1 << 64) if h < 0 else h


def _ck(clone_id) -> int:
    return -1 if clone_id is None else int(clone_id)


async def save_usage(day: str, guild_id: int, clone_id, gemini_used: int, openai_used: int) -> None:
    try:
        pool = await _pool()
        async with pool.acquire() as conn:
            await conn.execute(_USAGE_DDL)
            await conn.execute(
                "INSERT INTO scam_vision_usage (day, guild_id, clone_key, gemini_used, openai_used) VALUES ($1, $2, $3, $4, $5) "
                "ON CONFLICT (day, guild_id, clone_key) DO UPDATE SET gemini_used = EXCLUDED.gemini_used, openai_used = EXCLUDED.openai_used",
                day, guild_id, _ck(clone_id), gemini_used, openai_used)
    except Exception:
        pass


async def save_learned(h: int) -> None:
    try:
        pool = await _pool()
        async with pool.acquire() as conn:
            await conn.execute(_LEARNED_DDL)
            await conn.execute("INSERT INTO scam_vision_learned (dhash) VALUES ($1) ON CONFLICT (dhash) DO NOTHING", to_db_hash(h))
            await conn.execute("DELETE FROM scam_vision_learned WHERE dhash NOT IN "
                               "(SELECT dhash FROM scam_vision_learned ORDER BY learned_at DESC LIMIT $1)", LEARNED_KEEP)
    except Exception:
        pass


async def load_state(day: str):
    """(learned hashes, {(guild, clone_or_None): (gemini_used, openai_used)}) for today. ([], {}) on any trouble."""
    try:
        pool = await _pool()
        async with pool.acquire() as conn:
            await conn.execute(_USAGE_DDL)
            await conn.execute(_LEARNED_DDL)
            await conn.execute("DELETE FROM scam_vision_usage WHERE day < $1", day)         # keep today only
            learned = await conn.fetch("SELECT dhash FROM scam_vision_learned ORDER BY learned_at ASC")
            usage = await conn.fetch("SELECT guild_id, clone_key, gemini_used, openai_used FROM scam_vision_usage WHERE day = $1", day)
        return ([from_db_hash(int(r["dhash"])) for r in learned],
                {(int(r["guild_id"]), None if int(r["clone_key"]) < 0 else int(r["clone_key"])): (int(r["gemini_used"]), int(r["openai_used"]))
                 for r in usage})
    except Exception:
        return [], {}
