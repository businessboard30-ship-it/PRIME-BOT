"""/mydata: show and delete the data PRIME BOT holds about one person.

Reuses the dashboard delete (api.dash_member.member_data_delete), then also removes the in-server records below.
Kept on purpose: payments and plans, weekly usage counters, and moderation records (warns, bans, mod log), because
deleting those would undo safety actions or break accounting."""
import logging

from database import get_pool

logger = logging.getLogger(__name__)

PHRASE = "DELETE MY DATA"

# (label, table, user column). Each has a user_id column and is safe to wipe for one person across all servers/bots.
SERVER_TABLES = (
    ("XP and levels", "discord_xp", "user_id"),
    ("coins", "discord_economy_balances", "user_id"),
    ("inventory", "discord_economy_inventory", "user_id"),
    ("coin history", "discord_economy_transactions", "user_id"),
    ("level-up ping setting", "leveling_ping_optout", "user_id"),
)
DASH_TABLES = (
    ("dashboard card design", "user_level_cards", "user_id"),
    ("saved AI keys", "dev_connections", "user_id"),
    ("scheduled jobs", "dev_jobs", "user_id"),
    ("member messages sent or received", "dash_member_messages", "sender_id"),
)
KEPT = ["payments and plans", "weekly usage counters", "warns, bans and other moderation records"]


async def summary(user_id: int) -> dict:
    pool = await get_pool()
    out = {}
    async with pool.acquire() as conn:
        for label, table, col in SERVER_TABLES + DASH_TABLES:
            try:
                n = await conn.fetchval(f"SELECT COUNT(*) FROM {table} WHERE {col}::text = $1", str(user_id))
            except Exception:
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
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            for label, table, _col in SERVER_TABLES:
                r = await conn.execute(f"DELETE FROM {table} WHERE user_id = $1", int(user_id))
                try:
                    removed[label] = int(r.split()[-1])
                except Exception:
                    removed[label] = 0
    return {"ok": True, "message": "Done.", "removed": {k: v for k, v in removed.items() if v}}
