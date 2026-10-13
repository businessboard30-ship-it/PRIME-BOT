# path: modules/ai_bulk.py

"""
Pure logic behind the AI's bulk server commands: create several channels (optionally in a font), delete several
channels, and delete one member's recent messages.

Why this exists as ONE command per job instead of the model sending many commands:
  * the AI chat only ever runs the first tool call of a reply (see ai_tools._pick_call), so "make 8 channels" can't
    be 8 calls anyway -- the command itself takes the whole list;
  * every list has a hard cap and is checked BEFORE the confirm button, so the confirm text is the exact plan;
  * the work then runs one call at a time with a pause (never asyncio.gather) so Discord's rate limiter is never
    hit with a burst, and only one such job runs per server at a time;
  * an identical request repeated within a few seconds (model retry, double message) is dropped.

Nothing in here touches Discord -- it is all plain functions so it can be unit-tested.
"""

import json
import re
import time
from typing import Dict, Iterable, List, Optional, Tuple

MAX_CREATE = 15                 # channels made by one request
MAX_DELETE_CHANNELS = 10        # channels deleted by one request
MAX_USER_MESSAGES = 200         # one member's messages deleted by one request
MAX_SCAN_MESSAGES = 1000        # how far back in the channel we look for them
BULK_AGE_DAYS = 13              # Discord only bulk-deletes messages under 14 days old; stay safely inside that
PAUSE_SECONDS = 1.2             # between two channel create/delete calls
CALL_TIMEOUT = 15               # seconds a single Discord call may take before the job stops
JOB_COOLDOWN_SECONDS = 20       # after a job, the same server waits this long before the next one
MAX_RUNNING_JOBS = 3            # across the whole bot
DEDUPE_SECONDS = 15             # an identical AI command inside this window is ignored

_SPLIT = re.compile(r"[,;\n|]+")


def split_names(raw) -> List[str]:
    """'general, #memes ; staff-chat' -> ['general', 'memes', 'staff-chat']. Order kept, blanks and repeats dropped."""
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        raw = ",".join(str(x) for x in raw)
    out, seen = [], set()
    for part in _SPLIT.split(str(raw)):
        name = part.strip().lstrip("#").strip()
        if not name:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(name)
    return out


def slug(name: str, *, voice: bool = False, category: bool = False) -> str:
    """The plain (unstyled) channel name Discord would accept. Text channels: lowercase, hyphens, no punctuation.
    Voice channels keep spaces; categories keep their case and spaces."""
    text = " ".join((name or "").split())
    if category:
        return text[:100]
    if voice:
        return text.lower()[:100]
    text = re.sub(r"[^\w\- ]+", "", text.lower(), flags=re.UNICODE)
    text = re.sub(r"[\s_]+", "-", text.strip())
    return re.sub(r"-{2,}", "-", text).strip("-")[:100]


def plan_creation(raw_names, existing: Iterable[str], *, voice: bool = False, category: bool = False
                  ) -> Dict[str, list]:
    """Splits what was asked for into what will be made and why anything is left out.
    Returns {"create": [...], "duplicates": [...], "invalid": [...], "over_cap": [...]}."""
    taken = {(e or "").lower() for e in existing}
    plan = {"create": [], "duplicates": [], "invalid": [], "over_cap": []}
    seen = set()
    for raw in split_names(raw_names):
        name = slug(raw, voice=voice, category=category)
        if not name:
            plan["invalid"].append(raw)
        elif name.lower() in taken or name.lower() in seen:
            plan["duplicates"].append(name)
        elif len(plan["create"]) >= MAX_CREATE:
            plan["over_cap"].append(name)
        else:
            plan["create"].append(name)
            seen.add(name.lower())
    return plan


def clamp_messages(amount) -> int:
    try:
        n = int(amount)
    except (TypeError, ValueError):
        n = 50
    return max(1, min(MAX_USER_MESSAGES, n))


# ── one job at a time per server ──────────────────────────────────────────

class JobGuard:
    """Per-server lock + short cooldown, plus a bot-wide cap on running jobs."""

    def __init__(self):
        self._running: Dict[int, float] = {}      # guild_id -> started at
        self._finished: Dict[int, float] = {}     # guild_id -> finished at

    def try_start(self, guild_id: int, now: Optional[float] = None) -> Optional[str]:
        """None if the job may start (and is now marked running), else the reason it can't."""
        now = time.monotonic() if now is None else now
        if guild_id in self._running:
            return "A bulk job is already running in this server -- wait for it to finish."
        if len(self._running) >= MAX_RUNNING_JOBS:
            return "I'm busy with other servers' bulk jobs right now -- try again in a minute."
        wait = JOB_COOLDOWN_SECONDS - (now - self._finished.get(guild_id, -1e9))
        if wait > 0:
            return f"Give Discord a moment -- you can run another bulk job in {int(wait) + 1}s."
        self._running[guild_id] = now
        return None

    def finish(self, guild_id: int, now: Optional[float] = None) -> None:
        self._running.pop(guild_id, None)
        self._finished[guild_id] = time.monotonic() if now is None else now

    def is_running(self, guild_id: int) -> bool:
        return guild_id in self._running


JOBS = JobGuard()


# ── identical repeated AI commands ────────────────────────────────────────

class CallDeduper:
    """True for the second identical (user, command, args) inside DEDUPE_SECONDS."""

    def __init__(self, window: float = DEDUPE_SECONDS):
        self.window = window
        self._seen: Dict[str, float] = {}

    @staticmethod
    def key(user_id: int, name: str, args: dict) -> str:
        try:
            body = json.dumps(args or {}, sort_keys=True, default=str)
        except (TypeError, ValueError):
            body = repr(args)
        return f"{user_id}:{name}:{body}"

    def is_duplicate(self, user_id: int, name: str, args: dict, now: Optional[float] = None) -> bool:
        now = time.monotonic() if now is None else now
        for k in [k for k, t in self._seen.items() if now - t > self.window]:
            del self._seen[k]
        k = self.key(user_id, name, args)
        if k in self._seen:
            return True
        self._seen[k] = now
        return False


DEDUPER = CallDeduper()


def pick_tool_call(calls: List[dict]) -> Tuple[Optional[dict], int]:
    """From everything the model asked for in one reply, the single call to run and how many were dropped.
    Only the first call is ever run: a mutating command needs its own confirm click, so a second one in the same
    reply could never be confirmed alongside it. Anything beyond the first is reported, not silently executed."""
    calls = [c for c in (calls or []) if isinstance(c, dict) and c.get("name")]
    if not calls:
        return None, 0
    return calls[0], len(calls) - 1


def summarize(items: List[str], limit: int = 12) -> str:
    """'a, b, c and 4 more' for a confirm prompt."""
    items = list(items)
    if len(items) <= limit:
        return ", ".join(items)
    return ", ".join(items[:limit]) + f" and {len(items) - limit} more"
