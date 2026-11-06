# path: modules/antinuke.py

"""
Anti-nuke v1 -- pure logic (no Discord calls, no database), so it is easy to test.

A "nuke" is one actor destroying a server fast: deleting channels or roles, banning or kicking members. This module only
DECIDES: count each actor's destructive actions inside a sliding window and say when the threshold is crossed. The cog
(discord_bot/cogs/antinuke.py) does the I/O.

Rules (v1):
  * Per guild + per actor, all watched kinds count together (3 channel deletes + 1 ban in the window = 4 actions).
  * The server owner and the bot itself are never actioned (see `exempt`).
  * After a trip the actor is on a cooldown so one wave produces ONE containment, not one per remaining action.
  * Containment = remove the actor's dangerous roles (only ones the bot can edit) and alert staff. Nothing else.
"""

import time
from collections import deque
from typing import Deque, Dict, Iterable, List, Optional, Set, Tuple

WATCHED = ("channel_delete", "role_delete", "member_ban", "member_kick")
DEFAULT_THRESHOLD = 3          # destructive actions ...
DEFAULT_WINDOW = 10            # ... inside this many seconds
MIN_THRESHOLD, MAX_THRESHOLD = 2, 20
MIN_WINDOW, MAX_WINDOW = 5, 120
COOLDOWN_SECONDS = 120
ACTIONS = ("alert", "contain")  # alert = tell staff only; contain = also strip the actor's dangerous roles
DANGEROUS_PERMS = ("administrator", "manage_guild", "manage_roles", "manage_channels", "ban_members", "kick_members",
                   "manage_webhooks", "mention_everyone")

_events: Dict[Tuple[int, int], Deque[Tuple[float, str]]] = {}
_cooldown: Dict[Tuple[int, int], float] = {}


def _int(v, default):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def clamp_threshold(v) -> int:
    return max(MIN_THRESHOLD, min(MAX_THRESHOLD, _int(v, DEFAULT_THRESHOLD)))


def clamp_window(v) -> int:
    return max(MIN_WINDOW, min(MAX_WINDOW, _int(v, DEFAULT_WINDOW)))


def exempt(guild_owner_id, actor_id, bot_id) -> bool:
    """Owner and the bot itself are never actioned. An unknown actor (None) is also skipped: nothing to act on."""
    return actor_id is None or actor_id == guild_owner_id or actor_id == bot_id


def record(guild_id: int, actor_id: int, kind: str, *, threshold=DEFAULT_THRESHOLD, window=DEFAULT_WINDOW,
           now: Optional[float] = None) -> Optional[dict]:
    """Count one destructive action. Returns a trip dict {count, kinds, window} the FIRST time the threshold is crossed
    (then None until the cooldown ends), else None. Unwatched kinds are ignored."""
    if kind not in WATCHED:
        return None
    now = time.monotonic() if now is None else now
    threshold, window = clamp_threshold(threshold), clamp_window(window)
    key = (int(guild_id), int(actor_id))
    q = _events.setdefault(key, deque())
    q.append((now, kind))
    while q and now - q[0][0] > window:
        q.popleft()
    if len(q) < threshold:
        return None
    if now < _cooldown.get(key, 0.0):
        return None
    _cooldown[key] = now + COOLDOWN_SECONDS
    kinds: Dict[str, int] = {}
    for _, k in q:
        kinds[k] = kinds.get(k, 0) + 1
    return {"count": len(q), "kinds": kinds, "window": window}


def reset(guild_id: Optional[int] = None) -> None:
    """Forget counters (tests, or when a guild is disabled)."""
    for store in (_events, _cooldown):
        for k in [k for k in store if guild_id is None or k[0] == guild_id]:
            del store[k]


def is_dangerous(perms) -> bool:
    return any(getattr(perms, p, False) for p in DANGEROUS_PERMS)


def strippable_roles(member_roles: Iterable, bot_top_position: int) -> List:
    """Roles to remove from the actor: not @everyone, not managed (bot/integration), granting a dangerous permission,
    and strictly below the bot's top role (Discord refuses anything else)."""
    out = []
    for r in member_roles:
        if getattr(r, "is_default", lambda: False)() or getattr(r, "managed", False):
            continue
        if getattr(r, "position", 0) >= bot_top_position:
            continue
        if is_dangerous(getattr(r, "permissions", None)):
            out.append(r)
    return out


def describe(trip: dict) -> str:
    parts = ", ".join(f"{n}× {k.replace('_', ' ')}" for k, n in sorted(trip["kinds"].items()))
    return f"{trip['count']} destructive actions in {trip['window']}s ({parts})"
