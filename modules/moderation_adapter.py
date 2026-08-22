"""
Moderation Adapter — DB layer for group management features.

The tables this reads/writes (group_moderation_settings, blocked_words,
user_warns, custom_group_commands, join_gate_settings) were already being
created by database.py on cold start, but nothing in the codebase ever
queried them — this wires them up.
"""

import logging
from typing import Optional, Dict, List
from database import db

logger = logging.getLogger(__name__)

DEFAULT_SETTINGS = {
    "captcha_enabled": False,
    "captcha_timeout_seconds": 300,
    "slow_mode_enabled": False,
    "slow_mode_interval_seconds": 5,
    "night_mode_enabled": False,
    "night_mode_start_hour": None,
    "night_mode_end_hour": None,
    "word_filter_enabled": False,
    "anti_raid_enabled": False,
    "anti_raid_threshold": 5,
    "anti_raid_window_minutes": 5,
    "report_enabled": True,
    "logging_channel_id": None,
    "auto_delete_links_enabled": True,
    "auto_ban_spam_enabled": False,
    "spam_duplicate_threshold": 3,
    "spam_flood_threshold": 10,
    "spam_flood_window_seconds": 10,
    "auto_delete_service_messages": False,
    "auto_mute_new_members_enabled": False,
    "auto_mute_new_members_minutes": 10,
    "auto_ban_on_warns_enabled": False,
    "warn_ban_threshold": 5,
    "auto_pin_announcements_enabled": False,
    "auto_pin_tag": "#pin",
    "auto_dm_on_join_enabled": False,
}


async def get_settings(chat_id: int) -> Dict:
    """Get moderation settings for a chat, seeded with defaults if never configured."""
    row = await db.fetchrow(
        "SELECT * FROM group_moderation_settings WHERE chat_id = $1", chat_id
    )
    if row:
        return dict(row)
    return {"chat_id": chat_id, **DEFAULT_SETTINGS}


async def ensure_settings_row(chat_id: int, admin_id: int) -> None:
    """Create a settings row for this chat if one doesn't exist yet."""
    await db.execute(
        """
        INSERT INTO group_moderation_settings (chat_id, admin_id)
        VALUES ($1, $2)
        ON CONFLICT (chat_id) DO NOTHING
        """,
        chat_id, admin_id,
    )


async def set_setting(chat_id: int, admin_id: int, field: str, value) -> None:
    """Toggle/update a single moderation setting column."""
    if field not in DEFAULT_SETTINGS:
        raise ValueError(f"Unknown moderation setting: {field}")
    await ensure_settings_row(chat_id, admin_id)
    await db.execute(
        f"UPDATE group_moderation_settings SET {field} = $1 WHERE chat_id = $2",
        value, chat_id,
    )


# ─────────────────────────────────────────────────────────────────────────
# Word filter
# ─────────────────────────────────────────────────────────────────────────

async def add_blocked_word(chat_id: int, word: str, added_by: int) -> bool:
    try:
        await db.execute(
            """
            INSERT INTO blocked_words (chat_id, word_phrase, added_by)
            VALUES ($1, $2, $3)
            ON CONFLICT (chat_id, word_phrase) DO NOTHING
            """,
            chat_id, word.lower().strip(), added_by,
        )
        return True
    except Exception as e:
        logger.error(f"[v0] Error adding blocked word: {e}")
        return False


async def remove_blocked_word(chat_id: int, word: str) -> bool:
    result = await db.execute(
        "DELETE FROM blocked_words WHERE chat_id = $1 AND word_phrase = $2",
        chat_id, word.lower().strip(),
    )
    return result is not None and "DELETE 0" not in result


async def list_blocked_words(chat_id: int) -> List[str]:
    rows = await db.fetch(
        "SELECT word_phrase FROM blocked_words WHERE chat_id = $1 ORDER BY word_phrase",
        chat_id,
    )
    return [r["word_phrase"] for r in rows]


async def message_contains_blocked_word(chat_id: int, text: str) -> Optional[str]:
    """Returns the matched word/phrase, or None."""
    if not text:
        return None
    words = await list_blocked_words(chat_id)
    lowered = text.lower()
    for w in words:
        if w in lowered:
            return w
    return None


# ─────────────────────────────────────────────────────────────────────────
# Auto Delete Links: domain whitelist
# ─────────────────────────────────────────────────────────────────────────

async def add_whitelist_domain(chat_id: int, domain: str, added_by: int) -> bool:
    try:
        await db.execute(
            """
            INSERT INTO link_whitelist_domains (chat_id, domain, added_by)
            VALUES ($1, $2, $3)
            ON CONFLICT (chat_id, domain) DO NOTHING
            """,
            chat_id, domain.lower().strip(), added_by,
        )
        return True
    except Exception as e:
        logger.error(f"[v0] Error adding whitelist domain: {e}")
        return False


async def remove_whitelist_domain(chat_id: int, domain: str) -> bool:
    result = await db.execute(
        "DELETE FROM link_whitelist_domains WHERE chat_id = $1 AND domain = $2",
        chat_id, domain.lower().strip(),
    )
    return result is not None and "DELETE 0" not in result


async def list_whitelist_domains(chat_id: int) -> List[str]:
    rows = await db.fetch(
        "SELECT domain FROM link_whitelist_domains WHERE chat_id = $1 ORDER BY domain",
        chat_id,
    )
    return [r["domain"] for r in rows]


# ─────────────────────────────────────────────────────────────────────────
# Warns
# ─────────────────────────────────────────────────────────────────────────

async def add_warn(user_id: int, chat_id: int, warned_by: int, reason: str = "") -> int:
    """Insert a warn row and return the user's current total warn count in this chat.

    user_warns.warned_by has a FK to users(user_id). On Telegram this is
    always already satisfied (an admin issuing /warn has necessarily
    interacted with the bot before). The Discord port's moderators, however,
    are Discord admins who may never have touched this bot's `users` table
    at all — without this upsert, the very first /warn from a fresh Discord
    admin raises a ForeignKeyViolation and the command crashes. Upserting is
    a no-op (ON CONFLICT DO NOTHING) for the already-satisfied Telegram case.
    """
    await db.execute(
        "INSERT INTO users (user_id) VALUES ($1) ON CONFLICT (user_id) DO NOTHING",
        warned_by,
    )
    await db.execute(
        """
        INSERT INTO user_warns (user_id, chat_id, reason, warned_by, warn_count)
        VALUES ($1, $2, $3, $4, 1)
        """,
        user_id, chat_id, reason, warned_by,
    )
    count = await db.fetchval(
        "SELECT COUNT(*) FROM user_warns WHERE user_id = $1 AND chat_id = $2",
        user_id, chat_id,
    )
    return count or 0


async def clear_warns(user_id: int, chat_id: int) -> None:
    await db.execute(
        "DELETE FROM user_warns WHERE user_id = $1 AND chat_id = $2",
        user_id, chat_id,
    )


async def get_warn_count(user_id: int, chat_id: int) -> int:
    count = await db.fetchval(
        "SELECT COUNT(*) FROM user_warns WHERE user_id = $1 AND chat_id = $2",
        user_id, chat_id,
    )
    return count or 0


# ─────────────────────────────────────────────────────────────────────────
# Custom commands
# ─────────────────────────────────────────────────────────────────────────

async def set_custom_command(chat_id: int, name: str, response: str, created_by: int) -> None:
    name = name.lower().strip().lstrip("!/")
    await db.execute(
        """
        INSERT INTO custom_group_commands (chat_id, command_name, response_text, created_by)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (chat_id, command_name)
        DO UPDATE SET response_text = EXCLUDED.response_text, created_by = EXCLUDED.created_by
        """,
        chat_id, name, response, created_by,
    )


async def delete_custom_command(chat_id: int, name: str) -> bool:
    name = name.lower().strip().lstrip("!/")
    result = await db.execute(
        "DELETE FROM custom_group_commands WHERE chat_id = $1 AND command_name = $2",
        chat_id, name,
    )
    return result is not None and "DELETE 0" not in result


async def get_custom_command(chat_id: int, name: str) -> Optional[str]:
    name = name.lower().strip().lstrip("!/")
    row = await db.fetchrow(
        "SELECT response_text FROM custom_group_commands WHERE chat_id = $1 AND command_name = $2",
        chat_id, name,
    )
    return row["response_text"] if row else None


async def list_custom_commands(chat_id: int) -> List[str]:
    rows = await db.fetch(
        "SELECT command_name FROM custom_group_commands WHERE chat_id = $1 ORDER BY command_name",
        chat_id,
    )
    return [r["command_name"] for r in rows]


# ─────────────────────────────────────────────────────────────────────────
# Join gate
# ─────────────────────────────────────────────────────────────────────────

async def get_join_gate(chat_id: int) -> Optional[Dict]:
    row = await db.fetchrow(
        "SELECT * FROM join_gate_settings WHERE chat_id = $1 ORDER BY updated_at DESC LIMIT 1",
        chat_id,
    )
    return dict(row) if row else None


async def set_join_gate(chat_id: int, gate_link: str, gate_label: str = "Join Required") -> None:
    # join_gate_settings has no unique constraint on chat_id, so upsert by hand
    # rather than relying on ON CONFLICT (which would need one).
    existing = await get_join_gate(chat_id)
    if existing:
        await db.execute(
            """
            UPDATE join_gate_settings
            SET gate_link = $1, gate_label = $2, enabled = TRUE, updated_at = NOW()
            WHERE id = $3
            """,
            gate_link, gate_label, existing["id"],
        )
    else:
        await db.execute(
            """
            INSERT INTO join_gate_settings (chat_id, gate_link, gate_label, enabled)
            VALUES ($1, $2, $3, TRUE)
            """,
            chat_id, gate_link, gate_label,
        )


async def set_join_gate_enabled(chat_id: int, enabled: bool) -> None:
    existing = await get_join_gate(chat_id)
    if existing:
        await db.execute(
            "UPDATE join_gate_settings SET enabled = $1, updated_at = NOW() WHERE id = $2",
            enabled, existing["id"],
        )


# ─────────────────────────────────────────────────────────────────────────
# Anti-raid: recent join tracking (reuses the existing user_group_events
# table that feature_handlers.py already logs joins into)
# ─────────────────────────────────────────────────────────────────────────

async def count_recent_joins(chat_id: int, window_minutes: int) -> int:
    count = await db.fetchval(
        """
        SELECT COUNT(*) FROM user_group_events
        WHERE group_id = $1 AND event_type = 'join'
          AND event_timestamp > NOW() - ($2 || ' minutes')::interval
        """,
        chat_id, str(window_minutes),
    )
    return count or 0
