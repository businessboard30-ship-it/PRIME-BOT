# path: modules/word_router.py

"""
One-word router -- pure logic (no Discord calls). Type ONE word, get a button-only panel of the matching commands.

Triggers (never a bare word in a server channel, that would hijack normal chat):
    "@Bot raid"   mention + exactly one word
    "!raid"       prefix + exactly one word (unless it is a real prefix command)
    "raid"        a bare word in a DM with the bot

Everything here works on duck-typed command objects (qualified_name, description, parameters, parent, default_permissions,
guild_only), so it is tested without a bot. Safety lives in `allowed`: the SAME check runs when the panel is built and again
when a button is pressed, because Discord only enforces default_permissions in its own client, not on our direct call.
"""

import re
from dataclasses import dataclass
from typing import Callable, Iterable, List, Optional

WORD_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{1,31}$")
MAX_BUTTONS = 15                 # 3 rows x 5; keeps the V2 message well under its component limit
MAX_FALLBACK_OPTIONS = 25        # one select menu
ADMINISTRATOR = 1 << 3
# One-word shortcuts to the owner-only test harness panel (shown only where every harness gate passes).
HARNESS_WORDS = frozenset({"test", "nuke", "harness", "attack"})


@dataclass(frozen=True)
class Entry:
    qname: str
    description: str
    perms: int            # permission bits the command needs (0 = none)
    guild_only: bool


def parse_trigger(content: str, bot_id: Optional[int], in_dm: bool, is_prefix_command: Callable[[str], bool] = lambda w: False) -> Optional[str]:
    """The single word the user typed, or None if this message is not a trigger."""
    text = (content or "").strip()
    if not text:
        return None
    if bot_id is not None:
        for m in (f"<@{bot_id}>", f"<@!{bot_id}>"):
            if text.startswith(m):
                rest = text[len(m):].strip()
                return rest.lower() if WORD_RE.match(rest) else None
    if text.startswith("!"):
        w = text[1:]
        return w.lower() if WORD_RE.match(w) and not is_prefix_command(w.lower()) else None
    if in_dm and WORD_RE.match(text):
        return text.lower()
    return None


def _perm_bits(cmd) -> int:
    """Bits required by the command or the nearest parent that sets any (same rule Discord uses)."""
    node = cmd
    while node is not None:
        p = getattr(node, "default_permissions", None)
        if p is not None:
            return int(getattr(p, "value", p) or 0)
        node = getattr(node, "parent", None)
    return 0


def _guild_only(cmd) -> bool:
    node = cmd
    while node is not None:
        if getattr(node, "guild_only", False):
            return True
        node = getattr(node, "parent", None)
    return False


def entries_from(commands: Iterable) -> List[Entry]:
    """Leaf commands only, and only ones that can run with NO arguments (a button cannot collect them)."""
    out: List[Entry] = []
    for c in commands:
        if hasattr(c, "walk_commands") or hasattr(c, "commands"):      # a Group, not a leaf
            continue
        if any(getattr(p, "required", False) for p in getattr(c, "parameters", [])):
            continue
        out.append(Entry(c.qualified_name, (getattr(c, "description", "") or "")[:100], _perm_bits(c), _guild_only(c)))
    return out


def allowed(entry: Entry, user_perms: Optional[int], in_guild: bool) -> bool:
    """May this user open this command from a button? user_perms is None in a DM."""
    if entry.guild_only and not in_guild:
        return False
    if not entry.perms:
        return True
    if user_perms is None:
        return False
    return bool(user_perms & ADMINISTRATOR) or (user_perms & entry.perms) == entry.perms


def score(word: str, e: Entry) -> int:
    w = word.lower()
    parts = re.split(r"[ _-]+", e.qname.lower())
    s = 0
    if w in parts:
        s = 100
    elif any(p.startswith(w) for p in parts):
        s = 75
    elif w in e.qname.lower().replace(" ", ""):
        s = 55
    elif re.search(rf"\b{re.escape(w)}", e.description.lower()):
        s = 30
    return s


def rank(word: str, entries: Iterable[Entry], user_perms: Optional[int], in_guild: bool, limit: int = MAX_BUTTONS) -> List[Entry]:
    scored = [(score(word, e), e) for e in entries if allowed(e, user_perms, in_guild)]
    scored = [(s, e) for s, e in scored if s > 0]
    scored.sort(key=lambda t: (-t[0], len(t[1].qname), t[1].qname))
    return [e for _, e in scored[:limit]]


def top_level_names(entries: Iterable[Entry], user_perms: Optional[int], in_guild: bool, limit: int = MAX_FALLBACK_OPTIONS) -> List[str]:
    """Fallback when nothing matches: the names the user can reach, one per top-level command/group, alphabetical."""
    names = sorted({e.qname.split(" ")[0] for e in entries if allowed(e, user_perms, in_guild)})
    return names[:limit]
