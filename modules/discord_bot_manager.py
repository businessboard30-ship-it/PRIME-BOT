"""
Discord Bot Manager — Discord equivalent of modules/bot_manager.py
(handlers/bot_manager_handler.py's BotFather-style UI on the Telegram side).

Lets a user register a Discord bot they already own (by token) and edit it
without leaving this bot — same idea as the Telegram version, adapted to
what Discord's API actually exposes:

  Telegram (bot_manager.py)      -> Discord (here)
  setMyName                      -> PATCH /users/@me {"username"}
  setMyDescription                -> PATCH /applications/@me {"description"}
  setMyCommands                   -> PUT /applications/{id}/commands
  getMe (verify token)            -> discord_clone_service.validate_bot_token

Reuses the same `managed_bots` table modules/bot_manager.py uses — there's
nothing Telegram-specific in that schema (id, user_id, token, username,
name), so no migration needed; a Discord bot token stored there means the
exact same thing a Telegram one did.

NOT PORTED: nothing meaningful was dropped — every Telegram BotFather-style
action here has a direct Discord API equivalent, unlike, say, welcome_pay's
inline-query features which don't exist on Discord at all.
"""

import logging
from typing import Optional, Dict, List

from database import get_pool
from discord_clone_service import validate_bot_token, _mutate

logger = logging.getLogger(__name__)


async def verify_bot_token(token: str) -> Dict:
    """Returns the same {"ok", ...} shape as discord_clone_service's
    validate_bot_token — kept as a thin re-export so callers only import
    from this module, matching bot_manager.py's verify_bot_token shape."""
    return await validate_bot_token(token)


async def add_managed_bot(user_id: int, token: str, bot_user_id: int, username: str) -> bool:
    """Register a verified bot token for this user. False if already registered."""
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            existing = await conn.fetchval(
                "SELECT id FROM managed_bots WHERE user_id = $1 AND token = $2",
                user_id, token
            )
            if existing:
                return False
            await conn.execute(
                "INSERT INTO managed_bots (user_id, token, username, name) VALUES ($1, $2, $3, $4)",
                user_id, token, username or "unknown", username or "Bot"
            )
        return True
    except Exception as e:
        logger.error(f"[v0] Error adding managed bot: {e}")
        return False


async def get_user_bots(user_id: int) -> List[Dict]:
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, token, username, name FROM managed_bots WHERE user_id = $1 ORDER BY added_at ASC",
                user_id
            )
        return [dict(r) for r in rows]
    except Exception as e:
        logger.error(f"[v0] Error fetching managed bots: {e}")
        return []


async def get_managed_bot(user_id: int, bot_id: int) -> Optional[Dict]:
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, token, username, name FROM managed_bots WHERE id = $1 AND user_id = $2",
                bot_id, user_id
            )
        return dict(row) if row else None
    except Exception as e:
        logger.error(f"[v0] Error fetching managed bot: {e}")
        return None


async def remove_managed_bot(user_id: int, bot_id: int) -> bool:
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM managed_bots WHERE id = $1 AND user_id = $2",
                bot_id, user_id
            )
        return result.endswith("1")
    except Exception as e:
        logger.error(f"[v0] Error removing managed bot: {e}")
        return False


async def set_bot_username(token: str, username: str) -> Dict:
    """PATCH /users/@me. Discord enforces its own username rules (2-32
    chars, rate-limited to a couple changes per hour) — errors from Discord
    are passed through as-is in the "error" field."""
    result = await _mutate("PATCH", token, "/users/@me", {"username": username[:32]})
    if result.get("ok"):
        # Keep our cached copy of the name in sync.
        try:
            pool = await get_pool()
            async with pool.acquire() as conn:
                await conn.execute("UPDATE managed_bots SET username = $1, name = $1 WHERE token = $2", username[:32], token)
        except Exception as e:
            logger.error(f"[v0] Error syncing cached bot username: {e}")
    return result


async def set_bot_description(token: str, description: str) -> Dict:
    """PATCH /applications/@me — this is the bot's public description
    shown on its Discord profile/invite page, not a per-guild setting."""
    return await _mutate("PATCH", token, "/applications/@me", {"description": description[:400]})


async def sync_bot_commands(token: str, application_id: int, commands: List[Dict]) -> Dict:
    """PUT /applications/{id}/commands — a full overwrite of the bot's
    global slash commands (Discord's closest equivalent to setMyCommands).
    `commands` is a list of {"name", "description", "type": 1} dicts;
    global command changes can take up to an hour to propagate everywhere,
    same caveat Discord applies to any bot doing this."""
    return await _mutate("PUT", token, f"/applications/{application_id}/commands", commands)
