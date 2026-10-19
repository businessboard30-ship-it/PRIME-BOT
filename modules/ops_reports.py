# path: modules/ops_reports.py
"""Bot health reports without DMs. Every bot (main or clone) calls submit(); the row goes into ops_reports. The MAIN bot
runs flush() on a timer: it finds (or creates) the "bot-health" channel in the support server and posts the new rows
there. Never raises: a report that can't be written or posted is just dropped/retried, the game is never affected."""
from __future__ import annotations

import logging
from typing import Optional

import discord

logger = logging.getLogger(__name__)

CHANNEL_NAME = "bot-health"
CHANNEL_TOPIC = "Automatic health reports from the main bot and every clone. Posted by the main bot."
FLUSH_LIMIT = 10
MAX_TEXT = 1900
KEEP_DAYS = 14

_channel_id: Optional[int] = None


def bot_label(bot) -> str:
    cid = getattr(bot, "clone_id", None)
    return "main bot" if cid is None else f"clone #{cid}"


async def _pool():
    from database import get_pool
    return await get_pool()


async def submit(bot, key: str, text: str, cooldown: int = 0) -> bool:
    """Queue one report. With `cooldown` (seconds) the same (bot, key) is queued at most once per period.
    Returns True when queued."""
    try:
        label = bot_label(bot)
        pool = await _pool()
        async with pool.acquire() as conn:
            if cooldown and await conn.fetchval(
                    "SELECT 1 FROM ops_reports WHERE bot_label = $1 AND key = $2 "
                    "AND created_at > NOW() - make_interval(secs => $3) LIMIT 1", label, key, float(cooldown)):
                return False
            await conn.execute("INSERT INTO ops_reports (bot_label, key, text) VALUES ($1,$2,$3)",
                               label, key[:80], text[:MAX_TEXT])
        return True
    except Exception:
        logger.warning("[ops-reports] couldn't queue a report", exc_info=True)
        return False


async def ensure_channel(bot) -> Optional[discord.TextChannel]:
    """The support server's bot-health channel, created (private to server admins + this bot) if it doesn't exist."""
    global _channel_id
    import config
    guild = bot.get_guild(int(getattr(config, "DISCORD_SUPPORT_SERVER_ID", 0) or 0))
    if guild is None:
        return None
    ch = guild.get_channel(_channel_id) if _channel_id else None
    if ch is None:
        ch = discord.utils.get(guild.text_channels, name=CHANNEL_NAME)
    if ch is None:
        try:
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(view_channel=False),
                guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, embed_links=True,
                                                     read_message_history=True),
            }
            ch = await guild.create_text_channel(CHANNEL_NAME, overwrites=overwrites, topic=CHANNEL_TOPIC,
                                                 reason="Bot health reports")
        except discord.HTTPException:
            logger.warning("[ops-reports] couldn't create #%s in the support server (needs Manage Channels)",
                           CHANNEL_NAME, exc_info=True)
            return None
    _channel_id = ch.id
    return ch


async def flush(bot) -> int:
    """MAIN bot only: post the queued reports. Returns how many were posted."""
    try:
        pool = await _pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT id, bot_label, text FROM ops_reports WHERE posted_at IS NULL "
                                    "ORDER BY id LIMIT $1", FLUSH_LIMIT)
        if not rows:
            return 0
        ch = await ensure_channel(bot)
        if ch is None:
            return 0
        sent = 0
        for r in rows:
            try:
                await ch.send(f"**[{r['bot_label']}]**\n{r['text']}"[:2000], allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException:
                logger.warning("[ops-reports] couldn't post to #%s", CHANNEL_NAME, exc_info=True)
                break
            async with pool.acquire() as conn:
                await conn.execute("UPDATE ops_reports SET posted_at = NOW() WHERE id = $1", r["id"])
            sent += 1
        if sent:
            async with pool.acquire() as conn:
                await conn.execute("DELETE FROM ops_reports WHERE posted_at < NOW() - make_interval(days => $1)", KEEP_DAYS)
        return sent
    except Exception:
        logger.warning("[ops-reports] flush failed", exc_info=True)
        return 0
