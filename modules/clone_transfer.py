# path: modules/clone_transfer.py

"""
Moving a server's data from one clone to another (clone A is retired, clone B replaces it).

Per-server data is keyed by (guild_id, clone_id), so a replacement clone starts empty. This module moves the rows.

Safety rules (all tested):
  * BLOCKER: a source clone that is still LIVE (active and a heartbeat within 20 minutes) can never be migrated from. Two live bots
    in one server would be a real conflict, not a replacement. Shut it down first.
  * CONFIRMATION: when the source is down / inactive / removed, the owner must confirm the pair (old -> new) once with
    `/admin clonedata transfer`. Until that is done nothing is moved, not even automatically when the new clone joins a server.
    After the one-time confirmation, servers where the new clone newly joins are carried over automatically.
  * Tables are DISCOVERED from the database (every table with guild_id + clone_id) so nothing is forgotten, except an explicit
    deny-list: billing/entitlements, public listings/bump network, tokens, per-bot registry rows, caches, retired features.
  * A row whose move would collide with a row the new clone already has is skipped (the new clone's data wins), never overwritten.
  * Each server is moved in one transaction with a savepoint per table: one broken table cannot undo or block the rest.
  * Every run is recorded in clone_transfer_log (who, which clones, rows moved / skipped / failed).
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

LIVE_MAX_AGE_SECONDS = 20 * 60            # same window clone_health calls "delayed": might still be running, so treat as live
_IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")

# Never moved (reason shown to the owner). Matching is by exact name or prefix.
DENY_EXACT = {
    "discord_premium_groups": "billing / entitlements",
    "discord_guild_subscriptions": "billing / entitlements",
    "discord_pro_guilds": "billing / entitlements",
    "listing_boost_payments": "billing / entitlements",
    "verification_purchases": "billing / entitlements",
    "verification_qualifications": "billing / entitlements",
    "discord_guilds": "each bot keeps its own server registry row",
    "discord_new_guild_claims": "join bookkeeping of the old bot",
    "discord_dashboard_tokens": "login tokens",
    "discord_invite_cache": "cache, the new bot rebuilds it",
    "discord_quickstart_dm": "join bookkeeping",
    "auto_listing_offer": "join bookkeeping",
    "guild_terms_acceptance": "must be accepted again for the new bot",
    "market_listings": "public marketplace",
    "disguild_invites": "public listing",
    "server_reviews": "public listing",
    "report_notify_config": "owner report routing",
}
DENY_PREFIX = {
    "bump_": "bump network",
    "listing_": "public server listing",
    "server_listing": "public server listing",
    "heist_": "retired feature",
    "card_": "cards trades are per-bot and money-like",
}

# What to do by hand afterwards: Discord buttons/menus belong to the bot that posted them. Keyed by table.
PANEL_HINTS = {
    "discord_reaction_roles": "Reaction-role panels: run `/reactionrole repost` in the new bot (re-posts them for you).",
    "discord_ticket_config": "Ticket panel: post the panel again from the new bot (its ticket setup).",
    "discord_tickets": "Open tickets: their buttons are dead; close or re-open them from the new bot.",
    "discord_verification_config": "Verification panel: post it again from the new bot.",
    "discord_verification_passes": "Verification panel: post it again from the new bot.",
    "discord_giveaways": "Running giveaways: the Enter buttons are dead; end and re-run them from the new bot.",
    "discord_custom_role_settings": "Custom-role panel: post it again from the new bot.",
    "discord_music_panel": "Music panel: refreshes itself the next time music plays.",
    "discord_application_forms": "Application forms: re-post the apply button from the new bot.",
    "discord_suggestion_config": "Suggestion buttons on old suggestions are dead; new ones work.",
    "discord_autopost_config": "Auto-posts continue from the new bot.",
}


def deny_reason(table: str) -> Optional[str]:
    if table in DENY_EXACT:
        return DENY_EXACT[table]
    for p, why in DENY_PREFIX.items():
        if table.startswith(p):
            return why
    return None


def is_movable(table: str) -> bool:
    return bool(_IDENT.match(table)) and deny_reason(table) is None


# ---- liveness / the blocker (pure) ---------------------------------------------------------------------------------------

LIVE, DOWN, INACTIVE, REMOVED = "live", "down", "inactive", "removed"


def classify_source(row: Optional[dict], heartbeat: Optional[datetime], now: Optional[datetime] = None) -> str:
    now = now or datetime.now(timezone.utc)
    if row is None:
        return REMOVED
    if row.get("status") != "active":
        return INACTIVE
    if heartbeat is None:
        return DOWN
    if heartbeat.tzinfo is None:
        heartbeat = heartbeat.replace(tzinfo=timezone.utc)
    return LIVE if (now - heartbeat).total_seconds() <= LIVE_MAX_AGE_SECONDS else DOWN


def check_pair(old_clone: int, new_clone: int, source_state: str, target_row: Optional[dict]) -> Tuple[bool, str]:
    """(allowed to proceed to the confirmation step, reason when not)."""
    if old_clone == new_clone:
        return False, "Source and target are the same clone."
    if target_row is None or target_row.get("status") != "active":
        return False, f"Clone #{new_clone} is not an active clone."
    if source_state == LIVE:
        return False, (f"Clone #{old_clone} is still ONLINE. Moving its data while it is running would put two live bots in the same "
                       "servers. Shut it down first (or wait until it has been offline for 20 minutes), then run this again.")
    return True, ""


# ---- storage -------------------------------------------------------------------------------------------------------------

_ready = False


async def _ensure(conn) -> None:
    global _ready
    if _ready:
        return
    await conn.execute(
        "CREATE TABLE IF NOT EXISTS clone_transfer_auth (from_clone INTEGER NOT NULL, to_clone INTEGER NOT NULL, "
        "confirmed_by BIGINT NOT NULL, confirmed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), PRIMARY KEY (from_clone, to_clone))")
    await conn.execute(
        "CREATE TABLE IF NOT EXISTS clone_transfer_log (id BIGSERIAL PRIMARY KEY, guild_id BIGINT NOT NULL, from_clone INTEGER NOT NULL, "
        "to_clone INTEGER NOT NULL, status TEXT NOT NULL, moved JSONB, skipped JSONB, failed JSONB, note TEXT, by_user BIGINT, "
        "created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())")
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_clone_transfer_log_guild ON clone_transfer_log (guild_id, id DESC)")
    _ready = True


async def _pool():
    from database import get_pool
    return await get_pool()


async def discover_tables(conn) -> Tuple[List[str], Dict[str, str]]:
    """(tables to move, {skipped table: reason}) from the live schema: every base table with both guild_id and clone_id."""
    rows = await conn.fetch(
        "SELECT t.table_name FROM information_schema.tables t WHERE t.table_schema = current_schema() AND t.table_type = 'BASE TABLE' "
        "AND EXISTS (SELECT 1 FROM information_schema.columns c WHERE c.table_schema = t.table_schema AND c.table_name = t.table_name "
        "AND c.column_name = 'guild_id') "
        "AND EXISTS (SELECT 1 FROM information_schema.columns c WHERE c.table_schema = t.table_schema AND c.table_name = t.table_name "
        "AND c.column_name = 'clone_id') ORDER BY 1")
    move, skip = [], {}
    for r in rows:
        name = r["table_name"]
        if not _IDENT.match(name):
            continue
        why = deny_reason(name)
        if why:
            skip[name] = why
        elif not name.startswith("clone_transfer_"):
            move.append(name)
    return move, skip


async def plan_guild(conn, tables: Iterable[str], guild_id: int, old: int) -> Dict[str, int]:
    """Dry run: {table: rows the old clone has for this server}."""
    out = {}
    for t in tables:
        n = await conn.fetchval(f'SELECT COUNT(*) FROM "{t}" WHERE guild_id = $1 AND clone_id = $2', guild_id, old)
        if n:
            out[t] = int(n)
    return out


async def _move_table(conn, table: str, guild_id: int, old: int, new: int) -> Tuple[int, int]:
    import asyncpg
    try:
        async with conn.transaction():                       # savepoint inside the server's transaction
            res = await conn.execute(f'UPDATE "{table}" SET clone_id = $3 WHERE guild_id = $1 AND clone_id = $2', guild_id, old, new)
        return int((res or "UPDATE 0").split()[-1]), 0
    except asyncpg.UniqueViolationError:
        pass                                                 # the new clone already has some of these: go row by row, its rows win
    moved = skipped = 0
    rows = await conn.fetch(f'SELECT ctid FROM "{table}" WHERE guild_id = $1 AND clone_id = $2', guild_id, old)
    for r in rows:
        try:
            async with conn.transaction():
                await conn.execute(f'UPDATE "{table}" SET clone_id = $2 WHERE ctid = $1', r["ctid"], new)
            moved += 1
        except asyncpg.UniqueViolationError:
            skipped += 1
    return moved, skipped


async def transfer_guild(conn, tables: Iterable[str], guild_id: int, old: int, new: int) -> dict:
    """Moves one server's rows from `old` to `new` in one transaction (savepoint per table). Returns {moved, skipped, failed}."""
    moved, skipped, failed = {}, {}, {}
    async with conn.transaction():
        for t in tables:
            has = await conn.fetchval(f'SELECT 1 FROM "{t}" WHERE guild_id = $1 AND clone_id = $2 LIMIT 1', guild_id, old)
            if not has:
                continue
            try:
                m, s = await _move_table(conn, t, guild_id, old, new)
            except Exception as e:                           # one broken table must not stop the others
                logger.exception("[clone-transfer] %s failed for guild %s", t, guild_id)
                failed[t] = e.__class__.__name__
                continue
            if m:
                moved[t] = m
            if s:
                skipped[t] = s
    return {"moved": moved, "skipped": skipped, "failed": failed}


async def servers_to_move(conn, tables: List[str], old: int, new: int) -> List[int]:
    """Servers where the NEW clone is present (not left) and the OLD clone still has data."""
    present = {r["guild_id"] for r in await conn.fetch(
        "SELECT guild_id FROM discord_guilds WHERE clone_id = $1 AND left_at IS NULL", new)}
    if not present or not tables:
        return []
    union = " UNION ".join(f'SELECT guild_id FROM "{t}" WHERE clone_id = $1 AND guild_id = ANY($2::bigint[])' for t in tables)
    return sorted(r["guild_id"] for r in await conn.fetch(union, old, list(present)))


async def log_run(conn, guild_id: int, old: int, new: int, status: str, result: Optional[dict] = None, note: str = "",
                  by_user: Optional[int] = None) -> None:
    result = result or {}
    await conn.execute(
        "INSERT INTO clone_transfer_log (guild_id, from_clone, to_clone, status, moved, skipped, failed, note, by_user) "
        "VALUES ($1, $2, $3, $4, $5::jsonb, $6::jsonb, $7::jsonb, $8, $9)", guild_id, old, new, status,
        json.dumps(result.get("moved", {})), json.dumps(result.get("skipped", {})), json.dumps(result.get("failed", {})), note[:300], by_user)


# ---- authorization (the one-time confirmation) -----------------------------------------------------------------------------

async def is_authorized(old: int, new: int) -> bool:
    try:
        pool = await _pool()
        async with pool.acquire() as conn:
            await _ensure(conn)
            return bool(await conn.fetchval("SELECT 1 FROM clone_transfer_auth WHERE from_clone = $1 AND to_clone = $2", old, new))
    except Exception:
        logger.exception("[clone-transfer] couldn't read the authorization; treating as NOT authorized")
        return False


async def authorize(old: int, new: int, by_user: int) -> None:
    pool = await _pool()
    async with pool.acquire() as conn:
        await _ensure(conn)
        await conn.execute(
            "INSERT INTO clone_transfer_auth (from_clone, to_clone, confirmed_by) VALUES ($1, $2, $3) "
            "ON CONFLICT (from_clone, to_clone) DO UPDATE SET confirmed_by = EXCLUDED.confirmed_by, confirmed_at = NOW()", old, new, by_user)


async def revoke(old: int, new: int) -> bool:
    pool = await _pool()
    async with pool.acquire() as conn:
        await _ensure(conn)
        return "DELETE 0" not in await conn.execute("DELETE FROM clone_transfer_auth WHERE from_clone = $1 AND to_clone = $2", old, new)


async def list_authorizations() -> List[dict]:
    pool = await _pool()
    async with pool.acquire() as conn:
        await _ensure(conn)
        return [dict(r) for r in await conn.fetch("SELECT from_clone, to_clone, confirmed_by, confirmed_at FROM clone_transfer_auth ORDER BY confirmed_at DESC")]


async def source_state(old: int) -> str:
    from database import db
    return classify_source(await db.get_discord_clone(old), await db.get_discord_clone_heartbeat(old))


# ---- the automatic step when the new clone joins a server (replaces the old unconditional migration) ------------------------

async def auto_migrate_on_join(guild_id: int, new_clone: int) -> List[int]:
    """Called when a clone newly joins a server. Moves data from an earlier clone ONLY if the owner confirmed that pair and that
    clone is not live. Otherwise records a 'blocked' line (visible to the owner) and moves nothing. Returns the clone ids migrated from."""
    done: List[int] = []
    try:
        pool = await _pool()
        async with pool.acquire() as conn:
            await _ensure(conn)
            tables, _ = await discover_tables(conn)
            if not tables:
                return done
            union = " UNION ".join(f'SELECT clone_id FROM "{t}" WHERE guild_id = $1 AND clone_id IS NOT NULL AND clone_id <> $2' for t in tables)
            olds = sorted(r["clone_id"] for r in await conn.fetch(union, guild_id, new_clone))
        for old in olds:
            state = await source_state(old)
            if state == LIVE:
                continue                                      # a live bot's data is never touched (it is probably in that server too)
            if not await is_authorized(old, new_clone):
                async with pool.acquire() as conn:
                    await log_run(conn, guild_id, old, new_clone, "blocked", note=f"source {state}; owner has not confirmed {old} -> {new_clone}")
                logger.warning("[clone-transfer] guild %s has data under clone #%s (%s) but %s -> %s is not confirmed: nothing moved",
                               guild_id, old, state, old, new_clone)
                continue
            async with pool.acquire() as conn:
                result = await transfer_guild(conn, tables, guild_id, old, new_clone)
                await log_run(conn, guild_id, old, new_clone, "moved" if not result["failed"] else "partial", result, "automatic on join")
            done.append(old)
    except Exception:
        logger.exception("[clone-transfer] automatic step failed for guild %s; nothing further moved", guild_id)
    return done


# ---- text ------------------------------------------------------------------------------------------------------------------

def panel_checklist(moved_tables: Iterable[str]) -> List[str]:
    seen, out = set(), []
    for t in sorted(moved_tables):
        h = PANEL_HINTS.get(t)
        if h and h not in seen:
            seen.add(h)
            out.append(h)
    return out


def summarize(results: Dict[int, dict]) -> Tuple[int, int, int, set]:
    moved = skipped = failed = 0
    tables = set()
    for r in results.values():
        moved += sum(r["moved"].values())
        skipped += sum(r["skipped"].values())
        failed += len(r["failed"])
        tables |= set(r["moved"])
    return moved, skipped, failed, tables
