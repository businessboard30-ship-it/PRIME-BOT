"""
Owner-panel controls: persistent audit log, kill switches, global blacklist
and the premium-manager queries. See database/migrations/020_admin_controls.sql.

Two halves:

* Plain async DB helpers the panel screens call.
* `block_reason(interaction)`, called from the global slash-command check in
  discord_bot/cogs/_perm_guard.py. It reads a small in-memory snapshot
  (refreshed every CACHE_TTL seconds, and instantly after any panel change) so
  a busy bot doesn't hit Postgres on every command. It FAILS OPEN: if the
  database is unreachable the last good snapshot is used, and with no snapshot
  nothing is blocked. Owners (DISCORD_CLONE_ADMIN_IDS) are never blocked.

Only slash commands pass through the global check; buttons on panels that are
already open keep working until they time out.
"""

from __future__ import annotations

import logging
import time
from typing import Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

CACHE_TTL = 20  # seconds

MAINTENANCE = "maintenance"

# switch key -> (label, slash-command roots it turns off). Roots not listed
# here are never switched off by a feature switch.
FEATURES: Dict[str, Tuple[str, Set[str]]] = {
    "ai": ("AI chat & images", {"aichat", "aiimage", "aistatus", "aistore", "newchat", "endchat"}),
    "marketplace": ("Ads & marketplace", {"ad", "marketplace", "botstore"}),
    "media": ("Downloads & media", {"download", "convert", "movie", "imagesearch"}),
    "economy": ("Economy & heists", {"economy", "shop", "ecoconfig", "heist", "inventory", "loadout"}),
    "bump": ("Bump & discovery", {"bump", "bumpsetup", "discover"}),
    "leveling": ("Leveling & cards", {"rank", "leaderboard", "clan", "card", "levelrole"}),
}

_ROOT_TO_FEATURE: Dict[str, str] = {root: key for key, (_, roots) in FEATURES.items() for root in roots}

MSG_MAINTENANCE = "\N{HAMMER AND WRENCH}\N{VARIATION SELECTOR-16} The bot is in maintenance mode right now. Please try again in a little while."
MSG_FEATURE = "\N{DOUBLE VERTICAL BAR}\N{VARIATION SELECTOR-16} {label} is temporarily turned off by the bot owner. Please try again later."
MSG_USER = "\N{NO ENTRY SIGN} You've been blocked from using this bot."
MSG_GUILD = "\N{NO ENTRY SIGN} This server has been blocked from using this bot."

_snapshot: dict = {"ts": 0.0, "ok": False, "switches": set(), "users": set(), "guilds": set()}


async def _pool():
    from database import get_pool  # lazy: keeps this module importable in tests
    return await get_pool()


def invalidate() -> None:
    """Force the next check to reload from the database."""
    _snapshot["ts"] = 0.0


async def _refresh(force: bool = False) -> None:
    now = time.monotonic()
    if not force and _snapshot["ok"] and now - _snapshot["ts"] < CACHE_TTL:
        return
    # Even a failed refresh waits a full TTL before retrying so a down DB
    # isn't hammered on every command.
    _snapshot["ts"] = now
    try:
        pool = await _pool()
        async with pool.acquire() as conn:
            sw = await conn.fetch("SELECT switch FROM bot_kill_switches WHERE engaged")
            bl = await conn.fetch("SELECT kind, target_id FROM bot_blacklist")
        _snapshot["switches"] = {r["switch"] for r in sw}
        _snapshot["users"] = {r["target_id"] for r in bl if r["kind"] == "user"}
        _snapshot["guilds"] = {r["target_id"] for r in bl if r["kind"] == "guild"}
        _snapshot["ok"] = True
    except Exception:
        logger.debug("[admin-controls] snapshot refresh failed; keeping the last good one", exc_info=True)


async def block_reason(interaction) -> Optional[str]:
    """Message to show if this slash command must be refused, else None."""
    try:
        from config import DISCORD_CLONE_ADMIN_IDS
        user_id = interaction.user.id
        if user_id in DISCORD_CLONE_ADMIN_IDS:
            return None
        await _refresh()
        if user_id in _snapshot["users"]:
            return MSG_USER
        guild_id = getattr(interaction, "guild_id", None)
        if guild_id and guild_id in _snapshot["guilds"]:
            return MSG_GUILD
        if MAINTENANCE in _snapshot["switches"]:
            return MSG_MAINTENANCE
        cmd = getattr(interaction, "command", None)
        name = getattr(cmd, "qualified_name", None)
        if name:
            feature = _ROOT_TO_FEATURE.get(name.split()[0])
            if feature and feature in _snapshot["switches"]:
                return MSG_FEATURE.format(label=FEATURES[feature][0])
    except Exception:
        logger.debug("[admin-controls] block check failed; allowing", exc_info=True)
    return None


# ── audit log ────────────────────────────────────────────────────────────

async def record_audit(admin_id: int, action: str, guild_id: Optional[int], details: str) -> None:
    try:
        pool = await _pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO admin_panel_audit (admin_id, action, guild_id, details) VALUES ($1, $2, $3, $4)",
                admin_id, action[:100], guild_id, (details or "")[:500],
            )
    except Exception:
        logger.debug("[admin-controls] couldn't persist audit row", exc_info=True)


async def recent_audit(limit: int = 15) -> List[dict]:
    pool = await _pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, admin_id, action, guild_id, details, created_at "
            "FROM admin_panel_audit ORDER BY created_at DESC, id DESC LIMIT $1", limit)
    return [dict(r) for r in rows]


# ── kill switches ────────────────────────────────────────────────────────

async def get_engaged_switches() -> Set[str]:
    pool = await _pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT switch FROM bot_kill_switches WHERE engaged")
    return {r["switch"] for r in rows}


async def set_switch(switch: str, engaged: bool, by: int) -> None:
    if switch != MAINTENANCE and switch not in FEATURES:
        raise ValueError(f"unknown switch {switch!r}")
    pool = await _pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO bot_kill_switches (switch, engaged, updated_by, updated_at)
            VALUES ($1, $2, $3, NOW())
            ON CONFLICT (switch) DO UPDATE SET engaged = $2, updated_by = $3, updated_at = NOW()
            """,
            switch, engaged, by,
        )
    invalidate()


# ── blacklist ────────────────────────────────────────────────────────────

async def add_blacklist(kind: str, target_id: int, reason: str, by: int) -> None:
    if kind not in ("user", "guild"):
        raise ValueError(f"unknown blacklist kind {kind!r}")
    pool = await _pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO bot_blacklist (kind, target_id, reason, added_by)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (kind, target_id) DO UPDATE SET reason = $3, added_by = $4, created_at = NOW()
            """,
            kind, target_id, (reason or "")[:300], by,
        )
    invalidate()


async def remove_blacklist(kind: str, target_id: int) -> bool:
    pool = await _pool()
    async with pool.acquire() as conn:
        result = await conn.execute("DELETE FROM bot_blacklist WHERE kind = $1 AND target_id = $2", kind, target_id)
    invalidate()
    return result.endswith(" 1")


async def list_blacklist(limit: int = 25) -> List[dict]:
    pool = await _pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT kind, target_id, reason, added_by, created_at FROM bot_blacklist "
            "ORDER BY created_at DESC LIMIT $1", limit)
    return [dict(r) for r in rows]


# ── premium manager ──────────────────────────────────────────────────────

async def list_premium(limit: int = 25) -> List[dict]:
    """Premium servers (including ones still inside the grace window),
    soonest-to-expire first, so the ones needing attention are on top."""
    from config import PREMIUM_GRACE_DAYS
    pool = await _pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT s.guild_id, s.clone_id, s.expires_at, g.guild_name
            FROM discord_guild_subscriptions s
            LEFT JOIN discord_guilds g
                   ON g.guild_id = s.guild_id AND g.clone_id IS NOT DISTINCT FROM s.clone_id
            WHERE s.expires_at + make_interval(days => $1) > NOW()
            ORDER BY s.expires_at ASC
            LIMIT $2
            """,
            PREMIUM_GRACE_DAYS, limit)
    return [dict(r) for r in rows]


async def revoke_premium(guild_id: int, clone_id: Optional[int]) -> bool:
    """End premium immediately: expiry moves past the grace window so every
    feature gate sees it as lapsed. The row stays (subscription ids and
    history are kept)."""
    from config import PREMIUM_GRACE_DAYS
    pool = await _pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE discord_guild_subscriptions "
            "SET expires_at = NOW() - make_interval(days => $3), reminded_for = NULL, updated_at = NOW() "
            "WHERE guild_id = $1 AND clone_id IS NOT DISTINCT FROM $2",
            guild_id, clone_id, PREMIUM_GRACE_DAYS + 1)
    return not result.endswith(" 0")
