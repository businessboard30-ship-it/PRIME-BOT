# path: modules/cat_reminder.py

"""
Reminder for servers where the Cat game is OFF (no setup, switched off, or no channel picked).

The rule is deliberately small and final: a server gets AT MOST 2 reminders ("twice a week"), the second at least
3 days after the first, and all inside ONE week counted from its first reminder. When that week is over nothing more
is ever sent, whatever the server does. A server that turns the game on is never reminded (and if it turns it off
again later, the week is already used up or running, it is not restarted). Servers that joined less than
3 days ago are skipped (the welcome message already shows the Cat button).

Delivery is a DM to the server owner (same place the other setup nudges go). It is claimed in the database BEFORE
sending, so two ticks or two workers can never send the same reminder twice; a closed-DM owner is marked finished so
the bot never retries.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

MAX_REMINDERS = 2
WINDOW = timedelta(days=7)
MIN_GAP = timedelta(days=3)
NEW_SERVER_GRACE = timedelta(days=3)

# state = (sent_count, first_sent_at, last_sent_at)
State = Tuple[int, datetime, datetime]


def is_due(state: Optional[State], now: Optional[datetime] = None) -> bool:
    """True when this server may get a reminder right now."""
    now = now or datetime.now(timezone.utc)
    if state is None:
        return True
    sent, first, last = state
    if sent >= MAX_REMINDERS:
        return False
    if now - first >= WINDOW:
        return False                     # the week is over: no more, ever
    return now - last >= MIN_GAP


def game_is_off(cfg_enabled: bool, channel_ids) -> bool:
    return not (cfg_enabled and channel_ids)


def reminder_text(guild_name: str, premium: bool = False) -> str:
    return (
        f"🐱 **Cats for {guild_name}?** Your server doesn't have the Cat game switched on yet. "
        "Cute cat pictures appear in a channel you pick, and members tap **Adopt me!** to keep them, "
        "name them and trade them. It's just for fun.\n\n"
        "**Set it up in a minute:** run `/catsetup` in your server (needs Manage Server), pick a channel, "
        "and tap **Turn on**. You can also choose fewer cats and the hours they appear.\n"
        "Free servers get about 120 to 140 cats a day in 1 channel; ⭐ Premium gets up to 3 channels and 250 to 300 a day.\n\n"
        "This is one of only two reminders about this, and then I won't mention it again.")


async def _pool():
    from database import get_pool
    return await get_pool()


def _key(clone_id) -> int:
    return -1 if clone_id is None else int(clone_id)


async def states(clone_id) -> Dict[int, State]:
    pool = await _pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT guild_id, sent_count, first_sent_at, last_sent_at FROM cat_setup_reminders "
            "WHERE COALESCE(clone_id, -1) = $1", _key(clone_id))
    return {int(r["guild_id"]): (int(r["sent_count"]), r["first_sent_at"], r["last_sent_at"]) for r in rows}


async def enabled_guilds(clone_id) -> Set[int]:
    """Servers (of this bot) that have the game properly on: enabled AND at least one channel."""
    pool = await _pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT guild_id FROM cat_guild_config WHERE enabled AND cardinality(channel_ids) > 0 "
            "AND COALESCE(clone_id, -1) = $1", _key(clone_id))
    return {int(r["guild_id"]) for r in rows}


async def claim(guild_id: int, clone_id) -> bool:
    """Atomically take one reminder for this server. False = not allowed (limit, week over, too soon, or another
    worker got it first)."""
    pool = await _pool()
    async with pool.acquire() as conn:
        got = await conn.fetchval(
            "INSERT INTO cat_setup_reminders (guild_id, clone_id, sent_count) VALUES ($1, $2, 1) "
            "ON CONFLICT (guild_id, (COALESCE(clone_id, -1))) DO UPDATE SET "
            "sent_count = cat_setup_reminders.sent_count + 1, last_sent_at = NOW() "
            "WHERE cat_setup_reminders.sent_count < $3 "
            "AND cat_setup_reminders.first_sent_at > NOW() - make_interval(secs => $4) "
            "AND cat_setup_reminders.last_sent_at <= NOW() - make_interval(secs => $5) "
            "RETURNING sent_count", guild_id, clone_id, MAX_REMINDERS, WINDOW.total_seconds(), MIN_GAP.total_seconds())
    return got is not None


async def finish(guild_id: int, clone_id) -> None:
    """Closed DMs / no owner: count the server as done so it is never retried."""
    pool = await _pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE cat_setup_reminders SET sent_count = $3 WHERE guild_id = $1 AND COALESCE(clone_id, -1) = $2",
            guild_id, _key(clone_id), MAX_REMINDERS)


def pick_candidates(guilds: Iterable, off: Dict[int, bool], known: Dict[int, State], now: Optional[datetime] = None,
                    limit: int = 5) -> List:
    """The guilds to remind this tick: game off, due, not brand new. `off[guild_id]` is True when the game is off."""
    now = now or datetime.now(timezone.utc)
    out = []
    for g in guilds:
        if not off.get(g.id, True):
            continue
        me = getattr(g, "me", None)
        joined = getattr(me, "joined_at", None)
        if joined is not None and now - joined < NEW_SERVER_GRACE:
            continue
        if is_due(known.get(g.id), now):
            out.append(g)
            if len(out) >= limit:
                break
    return out
