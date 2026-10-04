"""Feature flags and the shared player gate (P1-04 / P1-13).

Every catch action calls ``check_player_allowed`` first. Phase 1 checks the
master switch and per-feature flags: the global row (guild_id 0, covers the
main bot and every clone) wins over the guild's own row for its clone. Sanctions and disclaimer acceptance plug in here in
P9-05, so callers never change.

Flags are cached for ``CACHE_TTL`` seconds so a turned-off feature stops
within seconds without a DB hit per button press.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from modules import catch_db

CACHE_TTL = 10.0
GLOBAL_GUILD = 0

FEATURES = (
    "game", "spawn", "catch", "encounter", "fishing", "trading", "market", "shop",
    "lootbox", "eggs", "swap", "lottery", "catchbot", "crews", "contests", "giveaways",
)
# Which flag guards each action. Every action is also guarded by "game".
ACTION_FEATURE: dict[str, str] = {
    "view": "game",
    "catch": "catch",
    "spawn": "spawn",
    "encounter": "encounter",
    "fish": "fishing",
    "trade": "trading",
    "market": "market",
    "shop": "shop",
    "lootbox": "lootbox",
    "egg": "eggs",
    "swap": "swap",
    "lottery": "lottery",
    "catchbot": "catchbot",
    "crew": "crews",
    "contest": "contests",
    "giveaway": "giveaways",
}


SUPPORT_ONLY_REASON = "the game is only open in the support server for now"


def support_server_only() -> bool:
    """True while the game is limited to the support server (config.CATCH_SUPPORT_SERVER_ONLY)."""
    import config
    return bool(config.CATCH_SUPPORT_SERVER_ONLY)


def guild_allowed(guild_id: int | None) -> bool:
    """May the game be used in this server? DMs and unknown servers are refused while restricted.

    Fails closed: if the support server ID is not configured, nobody is allowed.
    """
    if not support_server_only():
        return True
    import config
    support_id = int(config.DISCORD_SUPPORT_SERVER_ID or 0)
    return bool(support_id) and guild_id == support_id


@dataclass(frozen=True)
class Gate:
    allowed: bool
    reason: str | None = None


_cache: dict[tuple[int, int], tuple[float, dict[str, tuple[bool, str | None]]]] = {}


def invalidate(guild_id: int | None = None, clone_id: int | None = None) -> None:
    if guild_id is None or guild_id == GLOBAL_GUILD:
        _cache.clear()
    else:
        _cache.pop((guild_id, catch_db.clone_key(clone_id)), None)


async def _flags(guild_id: int, clone_id: int | None, conn=None) -> dict[str, tuple[bool, str | None]]:
    key = (guild_id, catch_db.clone_key(clone_id))
    hit = _cache.get(key)
    now = time.monotonic()
    if hit and hit[0] > now:
        return hit[1]
    async with catch_db.connection(conn) as c:
        rows = await c.fetch(
            "SELECT guild_id, feature, enabled, reason FROM catch_feature_flags "
            "WHERE (guild_id = 0 AND clone_key = -1) OR (guild_id = $1 AND clone_key = $2)",
            guild_id, catch_db.clone_key(clone_id),
        )
    # Global rows first so a guild row cannot re-enable a globally killed feature.
    merged: dict[str, tuple[bool, str | None]] = {}
    for r in sorted(rows, key=lambda r: r["guild_id"] != GLOBAL_GUILD):
        prev = merged.get(r["feature"])
        if prev is not None and not prev[0]:
            continue
        merged[r["feature"]] = (r["enabled"], r["reason"])
    _cache[key] = (now + CACHE_TTL, merged)
    return merged


def decide(flags: dict[str, tuple[bool, str | None]], action: str) -> Gate:
    """Pure decision so it can be unit-tested without a database."""
    feature = ACTION_FEATURE.get(action)
    if feature is None:
        raise KeyError(f"unknown catch action: {action}")
    for name in ("game", feature):
        enabled, reason = flags.get(name, (True, None))
        if not enabled:
            return Gate(False, reason or f"{name}_disabled")
    return Gate(True)


async def check_player_allowed(
    user_id: int, guild_id: int | None, action: str, clone_id: int | None = None, conn=None
) -> Gate:
    """Gate for every catch action. ``user_id`` is used by sanctions (P9-05)."""
    if not guild_allowed(guild_id):
        return Gate(False, SUPPORT_ONLY_REASON)
    flags = await _flags(guild_id or GLOBAL_GUILD, clone_id, conn)
    return decide(flags, action)


async def set_feature_flag(
    guild_id: int | None,
    clone_id: int | None,
    feature: str,
    enabled: bool,
    *,
    updated_by: int,
    reason: str | None = None,
    conn=None,
) -> None:
    if feature not in FEATURES:
        raise KeyError(f"unknown feature: {feature}")
    guild = guild_id or GLOBAL_GUILD
    if guild == GLOBAL_GUILD:
        clone_id = None  # the global kill switch covers the main bot and every clone
    async with catch_db.transaction(conn) as c:
        await c.execute(
            """
            INSERT INTO catch_feature_flags
                (guild_id, clone_id, feature, enabled, reason, updated_by, updated_at)
            VALUES ($1, $2, $3, $4, $5, $6, now())
            ON CONFLICT (guild_id, clone_key, feature) DO UPDATE SET
                enabled = EXCLUDED.enabled, reason = EXCLUDED.reason,
                updated_by = EXCLUDED.updated_by, updated_at = now()
            """,
            guild, clone_id, feature, enabled, reason, updated_by,
        )
        await c.execute(
            "INSERT INTO catch_audit (actor_id, clone_id, guild_id, action, detail) "
            "VALUES ($1, $2, $3, 'feature_flag', jsonb_build_object('feature', $4::text, 'enabled', $5::boolean, 'reason', $6::text))",
            updated_by, clone_id, guild, feature, enabled, reason,
        )
    invalidate(guild, clone_id)
