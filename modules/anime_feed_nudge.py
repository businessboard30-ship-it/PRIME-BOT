# path: modules/anime_feed_nudge.py

"""
Anime setup nudge: ONE reminder per server that never set the anime feed up, posted in the server's mod-log channel (the
automod log channel), never in a DM, with a Set up button. The reminder is claimed in the database BEFORE it is sent, so a
restart or a second process can never post it twice, and it is given back if the post fails. A server with no mod-log
channel (or one the bot can't write in) is simply skipped and looked at again later. Pure rules are at the top.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional, Set

logger = logging.getLogger(__name__)

MIN_SERVER_AGE = timedelta(hours=24)     # give a new server a day before the first reminder
MAX_PER_PASS = 5                          # at most this many reminders per pass, so a deploy never floods channels
PASS_HOURS = 6


def old_enough(joined_at: Optional[datetime], now: Optional[datetime] = None) -> bool:
    """True when the bot has been in the server long enough. An unknown join time counts as old enough."""
    if joined_at is None:
        return True
    now = now or datetime.now(timezone.utc)
    if joined_at.tzinfo is None:
        joined_at = joined_at.replace(tzinfo=timezone.utc)
    return now - joined_at >= MIN_SERVER_AGE


def is_set_up(enabled: bool, channel_id: Optional[int]) -> bool:
    """A server counts as set up as soon as it picked a channel or turned the feed on."""
    return bool(enabled or channel_id)


def nudge_text(bot_name: str) -> str:
    return "\n".join([
        "## 🎌 Anime spawns aren't set up yet",
        f"{bot_name} can drop a random popular anime or donghua card in a channel of your choice on a schedule you pick. Every card has "
        "Read more, Follow, Alerts, Search and Categories buttons. Adult titles are never shown.",
        "It takes under a minute: tap **Set up**, then **Quick setup** to let me create the channel for you.",
        "-# Only people with **Manage Server** can use the button. This reminder is sent once and never again.",
    ])


def _key(clone_id) -> int:
    return -1 if clone_id is None else int(clone_id)


async def _pool():
    from database import get_pool
    return await get_pool()


async def nudged_guild_ids(clone_id=None) -> Set[int]:
    try:
        pool = await _pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT guild_id FROM anime_feed_nudge WHERE clone_key = $1", _key(clone_id))
        return {int(r["guild_id"]) for r in rows}
    except Exception:
        logger.exception("[anime-nudge] couldn't read who was already nudged")
        return set()


async def claim(guild_id: int, clone_id=None) -> bool:
    """True for exactly one caller per server, ever: the row is the claim."""
    pool = await _pool()
    async with pool.acquire() as conn:
        got = await conn.fetchval(
            "INSERT INTO anime_feed_nudge (guild_id, clone_key) VALUES ($1, $2) ON CONFLICT DO NOTHING RETURNING 1",
            int(guild_id), _key(clone_id))
    return bool(got)


async def release(guild_id: int, clone_id=None) -> None:
    """Gives the claim back when the post failed, so a later pass can try again."""
    try:
        pool = await _pool()
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM anime_feed_nudge WHERE guild_id = $1 AND clone_key = $2", int(guild_id), _key(clone_id))
    except Exception:
        logger.exception("[anime-nudge] couldn't give the claim back for guild %s", guild_id)
