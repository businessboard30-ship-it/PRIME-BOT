"""/mydata: show and delete the data PRIME BOT holds about one person.

Reuses the dashboard delete (api.dash_member.member_data_delete), then removes the person's records in every server and
for every bot, found from the live schema (modules.data_purge) so new per-member tables are covered automatically.
Kept on purpose: payments and plans, weekly usage counters, moderation and security records (warns, bans, quarantine,
honeypot and scam shield records, audit logs) and invite join records, because deleting those would undo safety
actions, break other people's invite stats or break accounting.

The same server-scoped deletion also runs by itself when someone leaves a server and when the bot is removed from one."""
import logging

from database import get_pool
from modules import data_purge

logger = logging.getLogger(__name__)

PHRASE = "DELETE MY DATA"

# Dashboard tables (no guild column): counted here, deleted by dash_member.member_data_delete.
DASH_TABLES = (
    ("dashboard card design", "user_level_cards", "user_id"),
    ("saved AI keys", "dev_connections", "user_id"),
    ("scheduled jobs", "dev_jobs", "user_id"),
    ("member messages sent or received", "dash_member_messages", "sender_id"),
)
KEPT = ["payments and plans", "weekly usage counters", "warns, bans and other moderation and security records",
        "invite join records"]


async def summary(user_id: int) -> dict:
    pool = await get_pool()
    out = {}
    try:
        out.update(await data_purge.user_summary(user_id))
    except Exception:
        logger.warning("swallowed exception in summary", exc_info=True)
    async with pool.acquire() as conn:
        for label, table, col in DASH_TABLES:
            try:
                n = await conn.fetchval(f"SELECT COUNT(*) FROM {table} WHERE {col}::text = $1", str(user_id))
            except Exception:
                logger.warning("swallowed exception in summary", exc_info=True)
                n = None
            if n:
                out[label] = int(n)
    return out


async def delete_all(user_id: int, db) -> dict:
    """Returns {"ok": bool, "message": str, "removed": dict}."""
    from api import dash_member
    res = await dash_member.member_data_delete(str(user_id), {"confirm": dash_member.DELETE_PHRASE}, db)
    if res.get("_status"):
        return {"ok": False, "message": res.get("message", "Couldn't delete right now."), "removed": {}}
    removed = dict(res.get("removed") or {})
    purge = await data_purge.purge_user_everywhere(user_id)
    for label, n in purge["removed"].items():
        removed[label] = removed.get(label, 0) + n
    if purge["failed"]:
        logger.warning("[mydata] user %s: could not clear %s", user_id, purge["failed"])
        return {"ok": False, "message": "Some of your records couldn't be removed just now. Nothing is lost by trying "
                                        "again in a moment.", "removed": {k: v for k, v in removed.items() if v}}
    return {"ok": True, "message": "Done.", "removed": {k: v for k, v in removed.items() if v}}
