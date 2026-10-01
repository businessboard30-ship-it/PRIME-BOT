"""
Owner-panel Batch 6 data helpers: health dashboard, server inspector and user
inspector. The screens live in discord_bot/cogs/_views_admin_panel_inspect.py.

Nothing here adds a command or a table. Reads use plain SELECTs on tables that
already exist; the only writes are `reset_xp` and the helpers the panel already
uses elsewhere (blacklist, premium revoke, economy adjust).

Health reads never raise: a broken probe is reported as "unknown" so the
dashboard still opens when the thing you're trying to diagnose is what's broken.
"""

from __future__ import annotations

import logging
import math
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

PROCESS_STARTED = time.time()      # close enough to process start: imported when the cog loads
HEARTBEAT_STALE_MIN = 15           # clones touch last_heartbeat every 5 minutes (bot.py)
ERROR_WINDOW_S = 3600
MAX_COINS = 10 ** 9


async def _pool():
    from database import get_pool  # lazy: keeps this module importable in tests
    return await get_pool()


# ── formatting ───────────────────────────────────────────────────────────

def format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def age_text(dt: Optional[datetime], now: Optional[datetime] = None) -> str:
    if dt is None:
        return "never"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    return format_duration((now - dt).total_seconds()) + " ago"


# ── health: process / bot ────────────────────────────────────────────────

def memory_mb() -> Optional[float]:
    """Resident memory of this process, or None when it can't be read."""
    try:
        with open("/proc/self/status", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024.0
    except Exception:
        pass
    try:
        import resource
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0   # peak, in MB on Linux
    except Exception:
        return None


def bot_snapshot(bot: Any) -> Dict[str, Any]:
    latency = None
    try:
        raw = bot.latency
        if raw is not None and not math.isnan(raw) and not math.isinf(raw):
            latency = round(raw * 1000)
    except Exception:
        pass
    try:
        servers = len(bot.guilds)
    except Exception:
        servers = None
    try:
        cogs = len(bot.cogs)
    except Exception:
        cogs = None
    return {
        "latency_ms": latency,
        "uptime": format_duration(time.time() - PROCESS_STARTED),
        "memory_mb": memory_mb(),
        "servers": servers,
        "cogs": cogs,
    }


def loop_report(bot: Any) -> Tuple[int, List[str]]:
    """(running_count, names of started-but-stopped loops). Only loops a cog has
    actually touched show up, which is exactly the set that was meant to run."""
    running, stopped = 0, []
    try:
        from discord.ext import tasks
    except Exception:
        return 0, []
    try:
        cogs = list(bot.cogs.items())
    except Exception:
        return 0, []
    for cog_name, cog in cogs:
        try:
            attrs = list(vars(cog).items())
        except Exception:
            continue
        for attr, val in attrs:
            if not isinstance(val, tasks.Loop):
                continue
            try:
                if val.is_running():
                    running += 1
                else:
                    stopped.append(f"{cog_name}.{attr}")
            except Exception:
                stopped.append(f"{cog_name}.{attr}")
    return running, sorted(stopped)


def error_counts(window_s: int = ERROR_WINDOW_S, now: Optional[float] = None) -> Tuple[int, int]:
    """(errors, warnings) in the last `window_s` seconds, from the in-memory log
    buffer (this process only, newest 200 WARNING+ records)."""
    try:
        from modules import admin_ops
        entries = admin_ops.recent_logs(limit=admin_ops.LOG_CAPACITY, mode="warnings")
    except Exception:
        return 0, 0
    cutoff = (now if now is not None else time.time()) - window_s
    errors = warnings = 0
    for e in entries:
        if e.ts < cutoff:
            continue
        if e.level >= logging.ERROR:
            errors += 1
        else:
            warnings += 1
    return errors, warnings


async def db_ping() -> Optional[float]:
    """Round-trip of `SELECT 1` in ms, or None when the database can't be reached."""
    try:
        pool = await _pool()
        started = time.perf_counter()
        async with pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        return (time.perf_counter() - started) * 1000.0
    except Exception:
        logger.debug("[admin-inspect] db ping failed", exc_info=True)
        return None


async def clone_heartbeats() -> Optional[List[dict]]:
    """Active clones with their last heartbeat, or None if it can't be read."""
    try:
        pool = await _pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT clone_id, bot_username, last_heartbeat FROM discord_cloned_bots "
                "WHERE status = 'active' ORDER BY clone_id ASC")
        return [dict(r) for r in rows]
    except Exception:
        logger.debug("[admin-inspect] clone heartbeats unavailable", exc_info=True)
        return None


def quiet_clones(rows: List[dict], now: Optional[datetime] = None) -> List[dict]:
    """Clones whose heartbeat is missing or older than HEARTBEAT_STALE_MIN."""
    now = now or datetime.now(timezone.utc)
    out = []
    for r in rows:
        hb = r.get("last_heartbeat")
        if hb is None:
            out.append(r)
            continue
        if hb.tzinfo is None:
            hb = hb.replace(tzinfo=timezone.utc)
        if (now - hb).total_seconds() > HEARTBEAT_STALE_MIN * 60:
            out.append(r)
    return out


# ── server inspector ─────────────────────────────────────────────────────

async def server_rows(guild_id: int) -> List[dict]:
    """One row per bot (main bot and/or clones) that has ever been in this server."""
    pool = await _pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT g.guild_id, g.clone_id, g.guild_name, g.member_count, g.owner_id,
                   g.joined_at, g.left_at, c.bot_username
            FROM discord_guilds g
            LEFT JOIN discord_cloned_bots c ON c.clone_id = g.clone_id
            WHERE g.guild_id = $1
            ORDER BY g.left_at IS NOT NULL, g.joined_at DESC
            """, guild_id)
    return [dict(r) for r in rows]


async def server_extras(guild_id: int) -> dict:
    """Block entry, premium subscriptions and report counts for one server."""
    pool = await _pool()
    async with pool.acquire() as conn:
        blocked = await conn.fetchrow(
            "SELECT reason, created_at FROM bot_blacklist WHERE kind = 'guild' AND target_id = $1", guild_id)
        premium = await conn.fetch(
            "SELECT clone_id, expires_at FROM discord_guild_subscriptions WHERE guild_id = $1", guild_id)
        reports = await conn.fetch(
            "SELECT status, COUNT(*) AS n FROM server_listing_reports WHERE guild_id = $1 GROUP BY status",
            guild_id)
    return {
        "blocked": dict(blocked) if blocked else None,
        "premium": [dict(r) for r in premium],
        "reports": {r["status"]: r["n"] for r in reports},
    }


def premium_active(expires_at: Optional[datetime], grace_days: int = 0,
                   now: Optional[datetime] = None) -> bool:
    if expires_at is None:
        return False
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    return (now - expires_at).total_seconds() < grace_days * 86400


# ── user inspector ───────────────────────────────────────────────────────

async def user_card(user_id: int) -> dict:
    """Everything the panel shows about one person, in one round of queries."""
    pool = await _pool()
    async with pool.acquire() as conn:
        name = await conn.fetchval("SELECT username FROM discord_username_cache WHERE user_id = $1", user_id)
        blocked = await conn.fetchrow(
            "SELECT reason, created_at FROM bot_blacklist WHERE kind = 'user' AND target_id = $1", user_id)
        owned = await conn.fetch(
            "SELECT guild_id, clone_id, guild_name FROM discord_guilds "
            "WHERE owner_id = $1 AND left_at IS NULL ORDER BY joined_at DESC LIMIT 10", user_id)
        clones = await conn.fetch(
            "SELECT clone_id, bot_username, status FROM discord_cloned_bots "
            "WHERE owner_id = $1 ORDER BY clone_id ASC LIMIT 10", user_id)
        xp = await conn.fetch(
            """
            SELECT x.guild_id, x.clone_id, x.total_xp, x.level, g.guild_name
            FROM discord_xp x
            LEFT JOIN discord_guilds g
                   ON g.guild_id = x.guild_id AND g.clone_id IS NOT DISTINCT FROM x.clone_id
            WHERE x.user_id = $1 ORDER BY x.total_xp DESC LIMIT 5
            """, user_id)
        xp_servers = await conn.fetchval("SELECT COUNT(*) FROM discord_xp WHERE user_id = $1", user_id)
        eco = await conn.fetchrow(
            "SELECT COALESCE(SUM(balance), 0) AS total, COUNT(*) AS n "
            "FROM discord_economy_balances WHERE user_id = $1", user_id)
        pays = await conn.fetch(
            "SELECT status, COUNT(*) AS n, COALESCE(SUM(amount_usd), 0) AS total "
            "FROM payments WHERE user_id = $1 GROUP BY status ORDER BY status", user_id)
    return {
        "user_id": user_id,
        "username": name,
        "blocked": dict(blocked) if blocked else None,
        "owned": [dict(r) for r in owned],
        "clones": [dict(r) for r in clones],
        "xp": [dict(r) for r in xp],
        "xp_servers": int(xp_servers or 0),
        "coins_total": int(eco["total"]) if eco else 0,
        "coins_servers": int(eco["n"]) if eco else 0,
        "payments": [dict(r) for r in pays],
    }


async def reset_xp(user_id: int, guild_id: int, clone_id: Optional[int]) -> bool:
    """Zero one person's XP and level in one server. The row stays."""
    pool = await _pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE discord_xp SET total_xp = 0, level = 0 "
            "WHERE user_id = $1 AND guild_id = $2 AND clone_id IS NOT DISTINCT FROM $3",
            user_id, guild_id, clone_id)
    return not result.endswith(" 0")


# ── input parsing shared by the modals ───────────────────────────────────

def parse_snowflake(raw: str) -> Optional[int]:
    raw = (raw or "").strip()
    if not raw.isdigit() or not (10 <= len(raw) <= 20):
        return None
    return int(raw)


def parse_clone(raw: str) -> Tuple[bool, Optional[int]]:
    """(ok, clone_id). Empty means the main bot (clone_id None)."""
    raw = (raw or "").strip()
    if not raw:
        return True, None
    if not raw.isdigit():
        return False, None
    return True, int(raw)


def parse_coins(raw: str) -> Optional[int]:
    """A signed whole number, non-zero, within +/- MAX_COINS."""
    raw = (raw or "").strip().replace(",", "")
    sign = 1
    if raw[:1] in ("+", "-"):
        sign = -1 if raw[0] == "-" else 1
        raw = raw[1:]
    if not raw.isdigit():
        return None
    value = sign * int(raw)
    if value == 0 or abs(value) > MAX_COINS:
        return None
    return value
