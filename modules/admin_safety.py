"""
Owner-panel Batch 3 (moderation & safety) data helpers: abuse watchlist,
report queue, custom status rotation and the honeypot overview. The screens
live in discord_bot/cogs/_views_admin_panel_safety.py. See
database/migrations/022_admin_safety.sql.

Nothing here adds a command. The status helpers are also read by bot.py's
rotation loop, so `load_status_rotation()` never raises and falls back to
"no custom entries, online" (= the built-in rotation) when the DB is down.
"""

from __future__ import annotations

import logging
import time
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

WATCH_WINDOWS = (7, 30)          # days
WATCH_LIMIT = 10

REPORT_NEW, REPORT_REVIEWED, REPORT_DISMISSED = "new", "reviewed", "dismissed"

STATUS_KINDS: Dict[str, str] = {
    "playing": "Playing", "watching": "Watching", "listening": "Listening to", "competing": "Competing in",
}
PRESENCES: Dict[str, str] = {"online": "Online", "idle": "Idle", "dnd": "Do not disturb"}
STATUS_MAX_ENTRIES = 20
STATUS_TEXT_MAX = 128            # Discord's limit for an activity name
STATUS_CACHE_TTL = 60            # seconds


async def _pool():
    from database import get_pool  # lazy: keeps this module importable in tests
    return await get_pool()


# ── abuse watchlist ──────────────────────────────────────────────────────
# Rate-limit hits are not stored anywhere, so the watchlist counts the
# auto-mod actions automod.py already writes to moderation_logs
# (action_type automod_delete / automod_warn / automod_timeout / automod_kick).

async def watchlist(kind: str, days: int, limit: int = WATCH_LIMIT) -> List[dict]:
    if kind not in ("user", "guild"):
        raise ValueError(f"unknown watchlist kind {kind!r}")
    if days not in WATCH_WINDOWS:
        days = WATCH_WINDOWS[0]
    col = "target_user_id" if kind == "user" else "chat_id"
    pool = await _pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT {col} AS target_id, COUNT(*) AS hits, COUNT(DISTINCT chat_id) AS guilds,
                   MAX(created_at) AS last_at
            FROM moderation_logs
            WHERE left(action_type, 8) = 'automod_' AND {col} IS NOT NULL
              AND created_at > NOW() - make_interval(days => $1)
            GROUP BY {col} ORDER BY hits DESC, last_at DESC LIMIT $2
            """,
            days, limit,
        )
    return [dict(r) for r in rows]


# ── report queue ─────────────────────────────────────────────────────────

async def report_counts() -> Dict[str, int]:
    pool = await _pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT status, COUNT(*) AS n FROM server_listing_reports GROUP BY status")
    return {r["status"]: int(r["n"]) for r in rows}


async def list_new_reports(limit: int = 10) -> List[dict]:
    pool = await _pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, guild_id, reason, created_at FROM server_listing_reports
            WHERE status = 'new' ORDER BY created_at DESC, id DESC LIMIT $1
            """,
            limit,
        )
    return [dict(r) for r in rows]


async def resolve_report(report_id: int, status: str, by: int) -> bool:
    """Move a NEW report to reviewed/dismissed. Returns False if it was
    already handled (or doesn't exist), so a double press is a no-op."""
    if status not in (REPORT_REVIEWED, REPORT_DISMISSED):
        raise ValueError(f"bad report status {status!r}")
    pool = await _pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            """
            UPDATE server_listing_reports SET status = $2, reviewed_by = $3, reviewed_at = NOW()
            WHERE id = $1 AND status = 'new'
            """,
            report_id, status, by,
        )
    return result.endswith(" 1")


# ── status editor ────────────────────────────────────────────────────────

def clean_status_text(raw: Optional[str]) -> str:
    return " ".join((raw or "").split())[:STATUS_TEXT_MAX]


def render_status_text(text: str, servers: int, members: int) -> str:
    """{servers} and {members} placeholders, replaced literally (no str.format,
    so stray braces in an entry can never raise)."""
    out = (text or "").replace("{servers}", str(servers)).replace("{members}", f"{members:,}")
    return out[:STATUS_TEXT_MAX]


_status_cache: dict = {"ts": 0.0, "entries": [], "presence": "online"}


def invalidate_status() -> None:
    _status_cache["ts"] = 0.0


async def list_status_entries() -> List[dict]:
    pool = await _pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, kind, text, created_by, created_at FROM bot_status_entries ORDER BY id LIMIT $1",
            STATUS_MAX_ENTRIES,
        )
    return [dict(r) for r in rows]


async def get_presence() -> str:
    pool = await _pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT key, value FROM bot_status_config WHERE key = 'presence'")
    value = rows[0]["value"] if rows else "online"
    return value if value in PRESENCES else "online"


async def add_status_entry(kind: str, text: str, by: int) -> bool:
    """False when the list is already full. Raises on DB errors."""
    if kind not in STATUS_KINDS:
        raise ValueError(f"unknown status kind {kind!r}")
    text = clean_status_text(text)
    if not text:
        raise ValueError("empty status text")
    pool = await _pool()
    async with pool.acquire() as conn:
        n = await conn.fetchval("SELECT COUNT(*) FROM bot_status_entries")
        if int(n or 0) >= STATUS_MAX_ENTRIES:
            return False
        await conn.execute(
            "INSERT INTO bot_status_entries (kind, text, created_by) VALUES ($1, $2, $3)", kind, text, by)
    invalidate_status()
    return True


async def remove_status_entry(entry_id: int) -> bool:
    pool = await _pool()
    async with pool.acquire() as conn:
        result = await conn.execute("DELETE FROM bot_status_entries WHERE id = $1", entry_id)
    invalidate_status()
    return result.endswith(" 1")


async def set_presence(value: str, by: int) -> None:
    if value not in PRESENCES:
        raise ValueError(f"unknown presence {value!r}")
    pool = await _pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO bot_status_config (key, value, updated_by) VALUES ('presence', $1, $2)
            ON CONFLICT (key) DO UPDATE SET value = $1, updated_by = $2, updated_at = NOW()
            """,
            value, by,
        )
    invalidate_status()


async def reset_status(by: int) -> None:
    """Back to the built-in rotation: drop every custom entry and the presence override."""
    pool = await _pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM bot_status_entries")
        await conn.execute("DELETE FROM bot_status_config WHERE key = 'presence'")
    invalidate_status()


async def load_status_rotation() -> Tuple[List[Tuple[str, str]], str]:
    """For bot.py's rotation loop: ([(kind, text), ...], presence). Cached for
    STATUS_CACHE_TTL seconds. NEVER raises: on any DB problem it keeps the last
    good value, or ([], "online") if there is none (= built-in rotation)."""
    now = time.monotonic()
    if now - _status_cache["ts"] < STATUS_CACHE_TTL and _status_cache["ts"] > 0:
        return list(_status_cache["entries"]), _status_cache["presence"]
    try:
        entries = [(r["kind"], r["text"]) for r in await list_status_entries()]
        presence = await get_presence()
        _status_cache.update(ts=now, entries=entries, presence=presence)
    except Exception:
        logger.debug("[status] custom rotation unavailable, using last good/default", exc_info=True)
        _status_cache["ts"] = now - STATUS_CACHE_TTL + 10   # retry in ~10s, don't hammer a sick DB
    return list(_status_cache["entries"]), _status_cache["presence"]


# ── honeypot overview (read-only) ────────────────────────────────────────

async def honeypot_overview(limit: int = 15) -> Tuple[dict, List[dict]]:
    """(totals, rows). Only config + counters exist; individual catches are not recorded."""
    pool = await _pool()
    async with pool.acquire() as conn:
        t = await conn.fetch(
            """
            SELECT COUNT(*) AS configured, COUNT(*) FILTER (WHERE enabled) AS enabled,
                   COALESCE(SUM(triggered_count), 0) AS triggers
            FROM discord_honeypot_config
            """
        )
        rows = await conn.fetch(
            """
            SELECT guild_id, clone_id, enabled, action, channel_id, triggered_count, last_triggered_at
            FROM discord_honeypot_config
            ORDER BY enabled DESC, last_triggered_at DESC NULLS LAST, triggered_count DESC
            LIMIT $1
            """,
            limit,
        )
    totals = dict(t[0]) if t else {"configured": 0, "enabled": 0, "triggers": 0}
    return {k: int(v or 0) for k, v in totals.items()}, [dict(r) for r in rows]
