"""
Utility functions shared across the bot.

Trimmed for the Discord-only codebase: this used to also hold Telegram-
specific helpers (is_owner's clone_config/bot_data shape, safe_send_message/
safe_edit_message wrapping python-telegram-bot's Message/Bot objects and
telegram.error.BadRequest, and Markdown-v1/v2 escaping for Telegram's parse
modes) — all removed since nothing in discord_bot/ or modules/ used them
(only is_founder was actually imported, by modules/superbot_adapter.py).
"""

from config import ADMIN_ID


def is_founder(user_id: int) -> bool:
    """Check if user is the bot founder/admin (main bot's ADMIN_ID env var only)."""
    return ADMIN_ID is not None and user_id == ADMIN_ID
