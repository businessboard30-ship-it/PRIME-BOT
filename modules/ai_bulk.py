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
MAX_CREATE_ROLES = 15           # roles made by one request
MAX_DELETE_ROLES = 10           # roles deleted by one request
GUILD_ROLE_LIMIT = 250          # Discord's per-server role cap
MAX_USER_MESSAGES = 200         # one member's messages deleted by one request
MAX_SCAN_MESSAGES = 1000        # how far back in the channel we look for them
BULK_AGE_DAYS = 13              # Discord only bulk-deletes messages under 14 days old; stay safely inside that
PAUSE_SECONDS = 1.2             # between two channel create/delete calls
CALL_TIMEOUT = 15               # seconds a single Discord call may take before the job stops
JOB_COOLDOWN_SECONDS = 20       # after a job, the same server waits this long before the next one
MAX_RUNNING_JOBS = 3            # across the whole bot
MAX_COMMANDS_PER_MESSAGE = 10   # different commands the AI may run from ONE sentence
BATCH_PAUSE_SECONDS = 0.8       # between two commands of one batch (keeps Discord's rate limiter calm)
MAX_BULK_ASSIGN = 3000          # members one 'give/remove a role to many' request may touch
ASSIGN_PAUSE_SECONDS = 0.6      # between two role changes (discord.py also honours Discord's own rate limits)
# A role holding any of these is never handed out in bulk (give it to people one at a time, on purpose).
RISKY_BULK_PERMS = ("administrator", "manage_guild", "manage_roles", "manage_channels", "manage_webhooks", "manage_messages",
                    "manage_nicknames", "manage_expressions", "ban_members", "kick_members", "moderate_members", "mention_everyone",
                    "view_audit_log", "manage_events", "manage_threads")
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


def plan_dedupe(items: List[dict], only_keys: Optional[Iterable[str]] = None,
                limit: int = MAX_DELETE_CHANNELS) -> dict:
    """Which copies of same-named channels to delete, keeping ONE of each.

    `items` are plain dicts (so this stays testable without Discord), one per channel:
      id, kind ('text'|'voice'|'stage'|'forum'|'category'), key (the name with fancy fonts/brackets folded to plain
      letters, lowercased), protected (True for the rules/system/updates/AFK channel and the one we're chatting in),
      has_children (categories only: it still holds channels).
    Channels are the same when kind AND key match. Within a group the kept channel is a protected one if there is one
    (several protected ones are all kept), otherwise the OLDEST (lowest id). A category copy that still has channels
    in it is never deleted (it is reported in `skipped`). `only_keys` limits the check to those names.
    At most `limit` deletions are planned; `more` counts what was left for a next round.
    Returns {"groups": [{"key","kind","keep":[ids],"delete":[ids]}], "delete": [ids, in order],
             "skipped": [(id, reason)], "more": int}."""
    only = {k.strip().lower() for k in only_keys} if only_keys else None
    buckets: Dict[Tuple[str, str], List[dict]] = {}
    for it in items:
        key = (it.get("key") or "").strip()
        if not key:
            continue
        if only is not None and key not in only:
            continue
        buckets.setdefault((it["kind"], key), []).append(it)
    groups, delete, skipped = [], [], []
    for (kind, key), members in buckets.items():
        if len(members) < 2:
            continue
        members = sorted(members, key=lambda m: int(m["id"]))
        keep = [m for m in members if m.get("protected")] or [members[0]]
        keep_ids = {m["id"] for m in keep}
        doomed = []
        for m in members:
            if m["id"] in keep_ids:
                continue
            if kind == "category" and m.get("has_children"):
                skipped.append((m["id"], "it still has channels in it"))
                continue
            doomed.append(m["id"])
        if doomed:
            groups.append({"key": key, "kind": kind, "keep": [m["id"] for m in keep], "delete": doomed})
    groups.sort(key=lambda g: int(g["keep"][0]))
    for g in groups:
        delete.extend(g["delete"])
    more = max(0, len(delete) - limit)
    delete = delete[:limit]
    allowed = set(delete)
    for g in groups:
        g["delete"] = [i for i in g["delete"] if i in allowed]
    groups = [g for g in groups if g["delete"]]
    return {"groups": groups, "delete": delete, "skipped": skipped, "more": more}


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


def pick_tool_calls(calls: List[dict], limit: int = MAX_COMMANDS_PER_MESSAGE) -> Tuple[List[dict], int]:
    """Up to `limit` distinct commands from one model reply, in the order asked, and how many were dropped.
    Identical (name, arguments) repeats inside the reply are merged, not counted as dropped."""
    seen, picked, dropped = set(), [], 0
    for c in (calls or []):
        if not (isinstance(c, dict) and c.get("name")):
            continue
        try:
            key = (c["name"], json.dumps(c.get("arguments") or {}, sort_keys=True, default=str))
        except (TypeError, ValueError):
            key = (c["name"], repr(c.get("arguments")))
        if key in seen:
            continue
        seen.add(key)
        if len(picked) < limit:
            picked.append(c)
        else:
            dropped += 1
    return picked, dropped


def summarize(items: List[str], limit: int = 12) -> str:
    """'a, b, c and 4 more' for a confirm prompt."""
    items = list(items)
    if len(items) <= limit:
        return ", ".join(items)
    return ", ".join(items[:limit]) + f" and {len(items) - limit} more"


# ── roles ────────────────────────────────────────────────────────────────

NAMED_COLORS = {
    "red": 0xED4245, "orange": 0xE67E22, "yellow": 0xF1C40F, "green": 0x2ECC71, "teal": 0x1ABC9C,
    "blue": 0x3498DB, "purple": 0x9B59B6, "pink": 0xEB459E, "gold": 0xC9A227, "gray": 0x99AAB5,
    "grey": 0x99AAB5, "white": 0xFFFFFF, "black": 0x010101, "brown": 0x8B4513, "cyan": 0x11A8CD,
}


def parse_color(raw) -> Optional[int]:
    """'#ff0000' / 'ff0000' / '0xff0000' / 'red' -> int. None for blank (= no colour). -1 for something unusable."""
    if raw is None or str(raw).strip() == "":
        return None
    text = str(raw).strip().lower()
    if text in NAMED_COLORS:
        return NAMED_COLORS[text]
    text = text.lstrip("#")
    if text.startswith("0x"):
        text = text[2:]
    if re.fullmatch(r"[0-9a-f]{6}", text):
        return int(text, 16)
    if re.fullmatch(r"[0-9a-f]{3}", text):
        return int("".join(c * 2 for c in text), 16)
    return -1


def role_name(name: str) -> str:
    """A role name Discord accepts: spaces tidied, case kept, 100 chars max, no @ (so a role can't be named to look
    like a mass-ping)."""
    return " ".join((name or "").replace("@", "").split())[:100]


def plan_role_creation(raw_names, existing: Iterable[str], room: int = GUILD_ROLE_LIMIT) -> Dict[str, list]:
    """Same shape as plan_creation, for roles. `room` is how many more roles the server can hold."""
    taken = {(e or "").lower() for e in existing}
    plan = {"create": [], "duplicates": [], "invalid": [], "over_cap": []}
    seen = set()
    for raw in split_names(raw_names):
        name = role_name(raw)
        if not name or name.lower() in ("everyone", "here"):
            plan["invalid"].append(raw)
        elif name.lower() in taken or name.lower() in seen:
            plan["duplicates"].append(name)
        elif len(plan["create"]) >= min(MAX_CREATE_ROLES, max(room, 0)):
            plan["over_cap"].append(name)
        else:
            plan["create"].append(name)
            seen.add(name.lower())
    return plan
