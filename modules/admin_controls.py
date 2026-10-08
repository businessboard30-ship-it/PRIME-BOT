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
    # Also enforced inside clone_admin.register_clone_token, so the "Build Bot"
    # button wizard is stopped too (it isn't a slash command).
    "clone_registration": ("Clone registration", {"registerclone"}),
    # Website-only (no slash command): stops Developer-mode exports to the storage channel.
    "dev_export": ("Developer export", set()),
}

# Opt-in switches: engaged means ON (the opposite of FEATURES above, where engaged means turned off).
# Default is not engaged, so the feature stays owner-only until turned on from the owner panel.
OPT_IN: Dict[str, str] = {
    "build_bot_public": "Build Bot button open to everyone",
}

# Switch keys that guard website features only (no slash command to turn off).
WEBSITE_ONLY = frozenset({"dev_export"})

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


async def current_switches() -> Set[str]:
    """Engaged kill-switch keys from the cached snapshot (reloaded at most every
    CACHE_TTL seconds). Fails open: an empty set when nothing is known."""
    await _refresh()
    return set(_snapshot["switches"])


def build_bot_open_cached() -> bool:
    """Sync read for rendering buttons: True only when the owner turned the Build Bot button on for
    everyone. Fails closed (no snapshot yet or database down -> owner-only)."""
    return bool(_snapshot["ok"] and "build_bot_public" in _snapshot["switches"])


async def build_bot_open() -> bool:
    """True when anyone may use the Build Bot button; otherwise only owners (DISCORD_CLONE_ADMIN_IDS)."""
    try:
        await _refresh()
    except Exception:
        return False
    return build_bot_open_cached()


MSG_BUILD_BOT_LOCKED = "\N{LOCK} Build Bot is limited to the bot owner for now. Ask in the support server if you'd like access."


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
    if switch != MAINTENANCE and switch not in FEATURES and switch not in OPT_IN:
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


GRANT_MAX_DAYS = 3650


async def grant_premium(guild_id: int, by: int, days: int, clone_id: Optional[int]):
    """Add `days` on top of whatever time is left (or from now if lapsed). Returns the new expiry.
    Same call the Discord panel's grant / +30 / +90 buttons make, so web and Discord agree."""
    if not 1 <= int(days) <= GRANT_MAX_DAYS:
        raise ValueError("days out of range")
    from database import db
    return await db.activate_guild_premium(guild_id, by, int(days), clone_id)


async def list_bot_audit(before: Optional[int] = None, action: Optional[str] = None,
                         guild_id: Optional[int] = None, admin_id: Optional[int] = None,
                         limit: int = 50) -> List[dict]:
    """The bot's own admin_panel_audit trail (Discord panel actions), newest first, with filters."""
    pool = await _pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, admin_id, action, guild_id, details, created_at FROM admin_panel_audit "
            "WHERE ($1::bigint IS NULL OR id < $1) AND ($2::text IS NULL OR action = $2) "
            "AND ($3::bigint IS NULL OR guild_id = $3) AND ($4::bigint IS NULL OR admin_id = $4) "
            "ORDER BY id DESC LIMIT $5",
            before, action, guild_id, admin_id, max(1, min(int(limit), 200)))
    return [dict(r) for r in rows]


# ── helper accounts (per-section panel access) ───────────────────────────
#
# Real owners live in config.py and always get everything. A *helper* is an
# extra Discord user the owner lets into SOME panel sections. Only sections
# that the panel handles itself can be granted: screens that hand off to
# another cog (payments, broadcast, servers, bump, feedback, system) are still
# gated by that cog's own owner allowlist, so granting them here would only
# show a helper buttons that then refuse. Sensitive screens (access manager,
# config, database, kill switches) are owner-only on purpose.
#
# Unlike the command gate above, this FAILS CLOSED: if the helper list can't be
# read and nothing was ever loaded, helpers get no access (owners are
# unaffected). Changes made from the panel are written through to the
# in-memory map at once, so a revoke applies immediately in this process.

GRANTABLE: Dict[str, str] = {
    "audit": "Audit log",
    "blacklist": "Blacklist",
    "premium": "Premium",
    "logs": "Log tail",
}

_helpers: dict = {"ts": 0.0, "ok": False, "map": {}}


def _parse_sections(raw: Optional[str]) -> Set[str]:
    """Stored text -> set of valid grantable keys. Unknown keys (including a
    tampered 'access') are dropped here, so they can never grant anything."""
    return {s.strip() for s in (raw or "").split(",") if s.strip() in GRANTABLE}


def helper_sections(user_id: int) -> Set[str]:
    """Sections a helper may open. Sync (reads the in-memory map) so it can be
    used from allowed_sections()."""
    return set(_helpers["map"].get(user_id, ()))


def invalidate_helpers() -> None:
    _helpers["ts"] = 0.0


async def refresh_helpers(force: bool = False) -> None:
    """Reload the helper map from the database (at most every CACHE_TTL
    seconds unless forced). Never raises."""
    now = time.monotonic()
    if not force and _helpers["ok"] and now - _helpers["ts"] < CACHE_TTL:
        return
    _helpers["ts"] = now   # a failed refresh also waits a full TTL
    try:
        pool = await _pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT user_id, sections FROM admin_panel_helpers")
        _helpers["map"] = {r["user_id"]: _parse_sections(r["sections"]) for r in rows}
        _helpers["ok"] = True
    except Exception:
        logger.debug("[admin-controls] helper refresh failed; keeping the last good map", exc_info=True)


async def list_helpers(limit: int = 25) -> List[dict]:
    pool = await _pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT user_id, sections, added_by, created_at, updated_at FROM admin_panel_helpers "
            "ORDER BY created_at DESC LIMIT $1", limit)
    out = []
    for r in rows:
        d = dict(r)
        d["sections"] = _parse_sections(d.get("sections"))
        out.append(d)
    return out


async def set_helper(user_id: int, sections: Set[str], by: int) -> Set[str]:
    """Create or update a helper. Returns the sections actually stored (invalid
    ones are dropped). An empty set removes the helper."""
    clean = {s for s in sections if s in GRANTABLE}
    if not clean:
        await remove_helper(user_id)
        return set()
    pool = await _pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO admin_panel_helpers (user_id, sections, added_by)
            VALUES ($1, $2, $3)
            ON CONFLICT (user_id) DO UPDATE SET sections = $2, updated_at = NOW()
            """,
            user_id, ",".join(sorted(clean)), by)
    _helpers["map"][user_id] = set(clean)   # write-through
    invalidate_helpers()
    return clean


async def remove_helper(user_id: int) -> bool:
    pool = await _pool()
    async with pool.acquire() as conn:
        result = await conn.execute("DELETE FROM admin_panel_helpers WHERE user_id = $1", user_id)
    _helpers["map"].pop(user_id, None)       # write-through
    invalidate_helpers()
    return result.endswith(" 1")
