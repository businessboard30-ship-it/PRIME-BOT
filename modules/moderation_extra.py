"""
Moderation Extras Adapter
Welcome messages, Postgres-backed anti-flood tracking, and moderation logging —
ported from SUPER-BOT's get_chat_settings/set_chat_setting, check_flood, and
log_action/get_logs, adapted to this repo's per-row Postgres model.
"""

from datetime import datetime, timedelta
from typing import List, Dict, Optional
from database import get_pool

FLOOD_WINDOW_SECONDS = 10
FLOOD_MAX_MESSAGES = 6
FLOOD_MUTE_MINUTES = 10
AUTOBAN_MINUTES = 60


async def get_welcome_message(chat_id: int) -> Optional[str]:
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            return await conn.fetchval(
                "SELECT welcome_message FROM chat_memberships WHERE chat_id = $1 ORDER BY id DESC LIMIT 1",
                chat_id
            )
    except Exception as e:
        print(f"[v0] Error fetching welcome message: {e}")
        return None


async def set_welcome_message(chat_id: int, message: str) -> bool:
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            existing = await conn.fetchval("SELECT id FROM chat_memberships WHERE chat_id = $1", chat_id)
            if existing:
                await conn.execute("UPDATE chat_memberships SET welcome_message = $2 WHERE chat_id = $1", chat_id, message)
            else:
                await conn.execute(
                    "INSERT INTO chat_memberships (chat_id, welcome_message) VALUES ($1, $2)", chat_id, message
                )
        return True
    except Exception as e:
        print(f"[v0] Error setting welcome message: {e}")
        return False


async def get_pay_button(chat_id: int) -> Optional[Dict]:
    """Returns {'label': str, 'amount_ghs': int} if this chat has a Pay Now
    button configured for its welcome message, else None."""
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT pay_button_label, pay_button_amount_ghs FROM chat_memberships "
                "WHERE chat_id = $1 ORDER BY id DESC LIMIT 1",
                chat_id
            )
            if row and row["pay_button_label"] and row["pay_button_amount_ghs"]:
                return {"label": row["pay_button_label"], "amount_ghs": row["pay_button_amount_ghs"]}
            return None
    except Exception as e:
        print(f"[v0] Error fetching pay button: {e}")
        return None


async def set_pay_button(chat_id: int, label: str, amount_ghs: int) -> bool:
    """Admin-configured label + amount for a generic 'Pay Now' button shown
    in this chat's welcome message. Reuses chat_memberships (same row as the
    welcome message) rather than a new table."""
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            existing = await conn.fetchval("SELECT id FROM chat_memberships WHERE chat_id = $1", chat_id)
            if existing:
                await conn.execute(
                    "UPDATE chat_memberships SET pay_button_label = $2, pay_button_amount_ghs = $3 WHERE chat_id = $1",
                    chat_id, label, amount_ghs
                )
            else:
                await conn.execute(
                    "INSERT INTO chat_memberships (chat_id, pay_button_label, pay_button_amount_ghs) VALUES ($1, $2, $3)",
                    chat_id, label, amount_ghs
                )
        return True
    except Exception as e:
        print(f"[v0] Error setting pay button: {e}")
        return False


async def clear_pay_button(chat_id: int) -> bool:
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE chat_memberships SET pay_button_label = NULL, pay_button_amount_ghs = NULL WHERE chat_id = $1",
                chat_id
            )
        return True
    except Exception as e:
        print(f"[v0] Error clearing pay button: {e}")
        return False


async def log_action(chat_id: int, action_type: str, performed_by: int,
                      target_user_id: Optional[int] = None, reason: str = "") -> bool:
    """moderation_logs.performed_by has a FK to users(user_id) — same upsert
    reasoning as add_warn() above: Discord admins won't already have a
    `users` row the way a Telegram admin necessarily would. Without this,
    every Discord moderation action silently fails to log (this function
    already swallows the exception below), which would make /modlogs quietly
    useless for the Discord port instead of loudly broken — worth fixing at
    the source rather than relying on the try/except to hide it."""
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO users (user_id) VALUES ($1) ON CONFLICT (user_id) DO NOTHING",
                performed_by
            )
            await conn.execute("""
                INSERT INTO moderation_logs (chat_id, action_type, target_user_id, performed_by, reason)
                VALUES ($1, $2, $3, $4, $5)
            """, chat_id, action_type, target_user_id, performed_by, reason)
        return True
    except Exception as e:
        print(f"[v0] Error logging moderation action: {e}")
        return False


async def get_logs(chat_id: int, limit: int = 20, offset: int = 0) -> List[Dict]:
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT * FROM moderation_logs WHERE chat_id = $1
                ORDER BY created_at DESC LIMIT $2 OFFSET $3
            """, chat_id, limit, offset)
        return [dict(r) for r in rows]
    except Exception as e:
        print(f"[v0] Error fetching logs: {e}")
        return []


async def count_logs(chat_id: int) -> int:
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT COUNT(*) AS c FROM moderation_logs WHERE chat_id = $1", chat_id)
        return int(row["c"]) if row else 0
    except Exception as e:
        print(f"[v0] Error counting logs: {e}")
        return 0


async def record_flood_event(chat_id: int, user_id: int, message_text: str = None) -> bool:
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO flood_events (chat_id, user_id, message_text) VALUES ($1, $2, $3)",
                chat_id, user_id, message_text
            )
        return True
    except Exception as e:
        print(f"[v0] Error recording flood event: {e}")
        return False


async def get_recent_message_texts(chat_id: int, user_id: int, limit: int = 5) -> List[str]:
    """Most-recent-first list of this user's last `limit` message texts in
    this chat, for detecting 'same message posted repeatedly' spam."""
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT message_text FROM flood_events
                WHERE chat_id = $1 AND user_id = $2
                ORDER BY occurred_at DESC LIMIT $3
                """,
                chat_id, user_id, limit,
            )
        return [r["message_text"] for r in rows]
    except Exception as e:
        print(f"[v0] Error fetching recent message texts: {e}")
        return []


async def count_recent_flood_events(chat_id: int, user_id: int, window_seconds: int = FLOOD_WINDOW_SECONDS) -> int:
    try:
        pool = await get_pool()
        cutoff = datetime.now() - timedelta(seconds=window_seconds)
        async with pool.acquire() as conn:
            count = await conn.fetchval("""
                SELECT COUNT(*) FROM flood_events
                WHERE chat_id = $1 AND user_id = $2 AND occurred_at >= $3
            """, chat_id, user_id, cutoff)
        return count or 0
    except Exception as e:
        print(f"[v0] Error counting flood events: {e}")
        return 0


async def clear_flood_events(chat_id: int, user_id: int) -> bool:
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM flood_events WHERE chat_id = $1 AND user_id = $2", chat_id, user_id
            )
        return True
    except Exception as e:
        print(f"[v0] Error clearing flood events: {e}")
        return False
