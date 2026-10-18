# path: modules/sleep_events.py

"""
Small in-memory queue of 'what the bot put to sleep / woke up' events. The support-server log cog
(discord_bot/cogs/guild_activity.py) drains it every couple of minutes and posts ONE batched message.

- emit() never raises, never blocks, never touches the database. Bounded (MAX_QUEUE), oldest dropped.
- The same (kind, name) is announced at most once per REPEAT_SECONDS; extra ones are only counted for the daily digest,
  so a loop that flaps between asleep and awake cannot flood the channel.
- Only the main bot posts (clones don't have the support server cached); a clone's queue is simply never drained and
  stays bounded. Switch off with SLEEP_LOG=0.
"""

import os
import time
from collections import deque

MAX_QUEUE = 200
REPEAT_SECONDS = 6 * 3600

LABELS = {
    "giveaways": "Giveaway checker",
    "ship": "Ship matchmaker",
    "catch_scheduler": "Catch game scheduler",
    "cat_spawner": "Cat spawner",
    "scam_force_poll": "Scam Shield force-scan listener",
    "antiraid_sweeper": "Anti-raid sweeper",
    "guildatlas_invites": "GuildAtlas invite requests",
    "guildatlas_weblog": "GuildAtlas web log feed",
    "verification_poll": "Captcha pass listener",
    "schedule": "Scheduled messages",
    "bump_worker": "Bump queue",
    "bump_reminders": "Bump reminders",
    "report_notifications": "Report notifications",
    "roast_expire": "Roast expiry",
    "roast_trigger": "Roast triggers",
}

_queue: deque = deque(maxlen=MAX_QUEUE)
_last_sent: dict = {}
counts = {"sleep": 0, "wake": 0, "suppressed": 0, "dormant": 0, "active": 0}


def enabled() -> bool:
    return os.getenv("SLEEP_LOG", "1") != "0"


def label(name: str) -> str:
    return LABELS.get(name, name)


def emit(kind: str, name: str, text: str, *, force: bool = False) -> None:
    """kind: 'sleep' | 'wake' | 'dormant' | 'active' | 'note'. name identifies the thing (loop key or server id)."""
    try:
        if not enabled():
            return
        if kind in counts:
            counts[kind] += 1
        now = time.monotonic()
        key = (kind, name)
        last = _last_sent.get(key)
        if not force and last is not None and now - last < REPEAT_SECONDS:
            counts["suppressed"] += 1
            return
        _last_sent[key] = now
        if len(_last_sent) > 1000:
            cutoff = now - REPEAT_SECONDS
            for k in [k for k, v in _last_sent.items() if v < cutoff]:
                del _last_sent[k]
        _queue.append((kind, text))
    except Exception:
        pass


def drain(limit: int = 20) -> list:
    out = []
    while _queue and len(out) < limit:
        out.append(_queue.popleft())
    return out


def pending() -> int:
    return len(_queue)


def reset_digest() -> dict:
    snap = dict(counts)
    for k in counts:
        counts[k] = 0
    return snap
