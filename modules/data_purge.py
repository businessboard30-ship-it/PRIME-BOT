"""Automatic data deletion when the bot is removed from a server, or a member leaves one.

Discord's developer policy expects data to be deleted when it is no longer needed. Two triggers use this module:

* purge_guild(): the bot was removed from a server (kick, ban, or an admin removing it). Deletes everything stored for
  that server, for this bot or clone only.
* purge_member(): a person left (or was removed from) a server the bot is in. Deletes that person's records for that
  server only. Their other servers and their account-level data are untouched (/mydata covers account-level data).

The tables to wipe are found from the live schema (every base table with a guild_id column), so a table added later is
covered without touching this file. The KEEP sets below are the only exceptions, kept on purpose:

* Payments, plans and premium entitlements: accounting, and a paid server that re-adds the bot keeps what it paid for.
* Moderation and security records (quarantine, honeypot catches, scam shield hits, audit logs): deleting them would
  undo safety actions. Same policy as /mydata.
* Usage counters, which back the weekly limits.
* Invite join rows on member leave: they stop fake-invite abuse and feed the inviter's stats. They ARE deleted when the
  whole server is purged.

Clone safety: a table with a clone_id column is only touched for the clone that is leaving. A table without one belongs
to the main bot, so it is only touched when the main bot (clone_id None) is the one leaving.
"""
import logging
import os
from typing import Optional

from database import get_pool

logger = logging.getLogger(__name__)

# Kept when the whole server is purged.
GUILD_KEEP = frozenset({
    "discord_guild_subscriptions", "discord_guild_premium", "discord_pro_guilds", "discord_premium_groups",
    "listing_boost_payments", "verification_purchases", "premium_discount_codes",
    "admin_panel_audit",
})
# Kept when only one member leaves.
MEMBER_KEEP = frozenset({
    "premium_discount_codes", "discord_quarantined", "discord_honeypot_catches", "scam_shield_hits",
    "dash_audit", "ai_chat_usage", "discord_invite_joins",
})

_INT_TYPES = frozenset({"bigint", "integer", "smallint", "numeric"})
_plan_cache: Optional[dict] = None


def guild_purge_enabled() -> bool:
    return os.getenv("PURGE_GUILD_DATA_ON_REMOVE", "1").strip().lower() not in ("0", "false", "no", "off")


def member_purge_enabled() -> bool:
    return os.getenv("PURGE_MEMBER_DATA_ON_LEAVE", "1").strip().lower() not in ("0", "false", "no", "off")


def _q(ident: str) -> str:
    return '"' + ident.replace('"', '""') + '"'


def _bind(value: int, data_type: str):
    return int(value) if data_type in _INT_TYPES else str(value)


async def _plan(refresh: bool = False) -> dict:
    """{table: {"guild": type, "clone": bool, "user": type or None}} for every base table with a guild_id column."""
    global _plan_cache
    if _plan_cache is not None and not refresh:
        return _plan_cache
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT c.table_name, c.column_name, c.data_type
            FROM information_schema.columns c
            JOIN information_schema.tables t
              ON t.table_schema = c.table_schema AND t.table_name = c.table_name
            WHERE c.table_schema = current_schema() AND t.table_type = 'BASE TABLE'
              AND c.column_name IN ('guild_id', 'clone_id', 'user_id')
            """
        )
    plan: dict = {}
    for r in rows:
        entry = plan.setdefault(r["table_name"], {"guild": None, "clone": False, "user": None})
        if r["column_name"] == "guild_id":
            entry["guild"] = r["data_type"]
        elif r["column_name"] == "clone_id":
            entry["clone"] = True
        else:
            entry["user"] = r["data_type"]
    _plan_cache = {t: e for t, e in plan.items() if e["guild"]}
    return _plan_cache


async def _delete(conn, table: str, entry: dict, guild_id: int, clone_id: Optional[int],
                  user_id: Optional[int] = None) -> int:
    where = [f"{_q('guild_id')} = $1" if entry["guild"] in _INT_TYPES else f"{_q('guild_id')}::text = $1"]
    args = [_bind(guild_id, entry["guild"])]
    if entry["clone"]:
        args.append(clone_id)
        where.append(f"{_q('clone_id')} IS NOT DISTINCT FROM ${len(args)}")
    if user_id is not None:
        args.append(_bind(user_id, entry["user"]))
        where.append(f"{_q('user_id')} = ${len(args)}" if entry["user"] in _INT_TYPES
                     else f"{_q('user_id')}::text = ${len(args)}")
    result = await conn.execute(f"DELETE FROM {_q(table)} WHERE {' AND '.join(where)}", *args)
    try:
        return int(result.split()[-1])
    except Exception:
        return 0


async def purge_guild(guild_id: int, clone_id: Optional[int] = None) -> dict:
    """Delete everything stored for this server for this bot/clone. Idempotent, so safe to run again.
    Returns {"removed": {table: rows}, "failed": [table, ...]}."""
    plan = await _plan()
    removed: dict = {}
    failed: list = []
    pool = await get_pool()
    async with pool.acquire() as conn:
        for table, entry in sorted(plan.items()):
            if table in GUILD_KEEP:
                continue
            if not entry["clone"] and clone_id is not None:
                continue  # main-bot table: a clone leaving must not touch it
            try:
                n = await _delete(conn, table, entry, guild_id, clone_id)
            except Exception:
                logger.warning("[purge] guild %s: could not clear %s", guild_id, table, exc_info=True)
                failed.append(table)
                continue
            if n:
                removed[table] = n
    logger.info("[purge] guild %s clone %s: cleared %s rows in %s tables%s", guild_id, clone_id,
                sum(removed.values()), len(removed), f" (failed: {failed})" if failed else "")
    return {"removed": removed, "failed": failed}


async def purge_member(guild_id: int, user_id: int, clone_id: Optional[int] = None) -> dict:
    """Delete one person's records for one server. Account-level data is not touched."""
    plan = await _plan()
    removed: dict = {}
    failed: list = []
    pool = await get_pool()
    async with pool.acquire() as conn:
        for table, entry in sorted(plan.items()):
            if table in MEMBER_KEEP or not entry["user"]:
                continue
            if not entry["clone"] and clone_id is not None:
                continue
            try:
                n = await _delete(conn, table, entry, guild_id, clone_id, user_id)
            except Exception:
                logger.warning("[purge] member %s in guild %s: could not clear %s", user_id, guild_id, table,
                               exc_info=True)
                failed.append(table)
                continue
            if n:
                removed[table] = n
    if removed or failed:
        logger.info("[purge] member %s in guild %s clone %s: cleared %s rows%s", user_id, guild_id, clone_id,
                    sum(removed.values()), f" (failed: {failed})" if failed else "")
    return {"removed": removed, "failed": failed}


async def sweep_departed_guilds(tracked_rows: list, current_guild_ids: set, clone_id: Optional[int] = None) -> list:
    """Startup catch-up for removals that happened while the bot was offline (no event reaches us then).

    tracked_rows: db.list_discord_guilds(clone_id, include_left=True). Purges every tracked server the bot is no longer
    in. Safety: servers still marked as joined (left_at is None) are only purged if that is a small minority of what
    we track, so a bad gateway start that reports few guilds can never wipe real servers. Returns the purged ids."""
    if not guild_purge_enabled() or not current_guild_ids:
        return []
    gone_confirmed = [r["guild_id"] for r in tracked_rows
                      if r.get("left_at") is not None and r["guild_id"] not in current_guild_ids]
    gone_unmarked = [r["guild_id"] for r in tracked_rows
                     if r.get("left_at") is None and r["guild_id"] not in current_guild_ids]
    still_joined = [r for r in tracked_rows if r.get("left_at") is None]
    if gone_unmarked and len(gone_unmarked) > max(5, len(still_joined) // 4):
        logger.error("[purge] sweep: %s of %s joined servers look missing; skipping those as a safety check",
                     len(gone_unmarked), len(still_joined))
        gone_unmarked = []
    purged = []
    for gid in gone_confirmed + gone_unmarked:
        try:
            res = await purge_guild(int(gid), clone_id)
            if not res["failed"]:
                purged.append(gid)
        except Exception:
            logger.warning("[purge] sweep failed for guild %s", gid, exc_info=True)
    return purged


# ── /mydata: one person, every server and every bot ───────────────────────

_LABELS = {
    "discord_xp": "XP and levels",
    "discord_economy_balances": "coins",
    "discord_economy_inventory": "inventory",
    "discord_economy_transactions": "coin history",
    "leveling_ping_optout": "level-up ping setting",
}


def _label(table: str) -> str:
    return _LABELS.get(table) or table.removeprefix("discord_").replace("_", " ")


def _user_tables(plan: dict) -> list:
    """Per-member server tables /mydata may touch: every guild table with a user_id column except the kept ones."""
    return sorted(t for t, e in plan.items() if e["user"] and t not in MEMBER_KEEP)


async def user_summary(user_id: int) -> dict:
    """{label: row count} of this person's records across every server and bot (non-zero only)."""
    plan = await _plan()
    out: dict = {}
    pool = await get_pool()
    async with pool.acquire() as conn:
        for table in _user_tables(plan):
            entry = plan[table]
            col = _q("user_id") if entry["user"] in _INT_TYPES else _q("user_id") + "::text"
            try:
                n = await conn.fetchval(f"SELECT COUNT(*) FROM {_q(table)} WHERE {col} = $1",
                                        _bind(user_id, entry["user"]))
            except Exception:
                logger.warning("[mydata] could not count %s", table, exc_info=True)
                continue
            if n:
                out[_label(table)] = out.get(_label(table), 0) + int(n)
    return out


async def purge_user_everywhere(user_id: int) -> dict:
    """Delete this person's records in every server and for every bot/clone. Idempotent.
    Returns {"removed": {label: rows}, "failed": [table, ...]}."""
    plan = await _plan()
    removed: dict = {}
    failed: list = []
    pool = await get_pool()
    async with pool.acquire() as conn:
        for table in _user_tables(plan):
            entry = plan[table]
            col = _q("user_id") if entry["user"] in _INT_TYPES else _q("user_id") + "::text"
            try:
                res = await conn.execute(f"DELETE FROM {_q(table)} WHERE {col} = $1", _bind(user_id, entry["user"]))
                n = int(res.split()[-1])
            except Exception:
                logger.warning("[mydata] could not clear %s", table, exc_info=True)
                failed.append(table)
                continue
            if n:
                removed[_label(table)] = removed.get(_label(table), 0) + n
    return {"removed": removed, "failed": failed}
