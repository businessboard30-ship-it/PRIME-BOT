# path: modules/cat_perms.py

"""
Why is the Cat game ON but not posting?  The usual cause is a Discord permission (or channel) that changed after
the server picked its spawn channel: the game stays ON, the spawner quietly skips the channel, and nobody is told.

Pure helpers (no database, no network) so the setup panel, the spawner and /catsetup all give the same answer.

What the bot needs in a spawn channel (the Adopt button itself needs nothing extra):
  * View Channel   - to see it at all
  * Send Messages  - to post the cat
  * Embed Links    - the cat is an embed with the picture in it
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Dict, Iterable, List, Tuple

REQUIRED: Tuple[Tuple[str, str], ...] = (
    ("view_channel", "View Channel"),
    ("send_messages", "Send Messages"),
    ("embed_links", "Embed Links"),
)

ALERT_AFTER_FAILED_TURNS = 3          # a server must fail this many turns in a row before its owner is told
ALERT_COOLDOWN_SECONDS = 7 * 86400    # then at most one message a week (kept in memory: a restart can repeat it once)
MAX_TRACKED = 5000


@dataclass(frozen=True)
class Problem:
    channel_id: int
    kind: str                         # "gone" | "not_text" | "perms"
    missing: Tuple[str, ...] = ()     # readable names, only for kind == "perms"


def missing_names(perms) -> List[str]:
    """Readable names of the required permissions that `perms` lacks (a Discord Permissions-like object)."""
    return [label for attr, label in REQUIRED if not getattr(perms, attr, False)]


def diagnose(guild, channel_ids: Iterable[int], text_channel_type) -> List[Problem]:
    """One Problem per configured channel the bot can't post in right now. [] means all good.
    `text_channel_type` is discord.TextChannel (passed in so this module never imports discord)."""
    out: List[Problem] = []
    me = getattr(guild, "me", None)
    for cid in channel_ids:
        ch = guild.get_channel(cid)
        if ch is None:
            out.append(Problem(int(cid), "gone"))
        elif not isinstance(ch, text_channel_type):
            out.append(Problem(int(cid), "not_text"))
        else:
            miss = missing_names(ch.permissions_for(me)) if me is not None else [label for _, label in REQUIRED]
            if miss:
                out.append(Problem(int(cid), "perms", tuple(miss)))
    return out


def _join(items: List[str]) -> str:
    items = [f"**{i}**" for i in items]
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def describe(problem: Problem) -> str:
    c = f"<#{problem.channel_id}>"
    if problem.kind == "gone":
        return f"{c} no longer exists (or I can't see it). Pick a new spawn channel."
    if problem.kind == "not_text":
        return f"{c} isn't a text channel any more. Pick a new spawn channel."
    return f"In {c} I'm missing {_join(list(problem.missing))}."


FIX_HINT = ("Fix: open the channel, **Edit Channel → Permissions**, and allow my role (or the bot) those permissions. "
            "Cats start again by themselves.")


def panel_text(problems: List[Problem]) -> str:
    """For the setup panel. '' when there is nothing wrong."""
    if not problems:
        return ""
    return "\n".join(["⚠️ **The Cat game is ON but I can't post:**", *(f"• {describe(p)}" for p in problems), FIX_HINT])


def owner_message(guild_name: str, problems: List[Problem]) -> str:
    """The one-off DM to the server owner."""
    name = str(guild_name or "your server").replace("*", "").replace("`", "")[:80]
    return "\n".join([
        f"🐱 **Cats aren't appearing in {name}.** The Cat game is switched on, but:",
        *(f"• {describe(p)}" for p in problems),
        FIX_HINT,
        "(You can also check with `/catsetup`, it shows this live. I'll only remind you about this once a week.)",
    ])


class AlertTracker:
    """Decides when the owner should be told. Counts failed turns in a row per server; a good turn resets it."""

    def __init__(self, after: int = ALERT_AFTER_FAILED_TURNS, cooldown: float = ALERT_COOLDOWN_SECONDS):
        self.after, self.cooldown = after, cooldown
        self._fails: Dict[Tuple[int, object], int] = {}
        self._sent: Dict[Tuple[int, object], float] = {}

    def ok(self, guild_id: int, clone_id) -> None:
        self._fails.pop((guild_id, clone_id), None)

    def failed(self, guild_id: int, clone_id, now: float = None) -> bool:
        """Record one failed turn. True when the owner should be messaged now."""
        now = time.monotonic() if now is None else now
        key = (guild_id, clone_id)
        n = self._fails.get(key, 0) + 1
        self._fails[key] = n
        if n < self.after:
            return False
        if now - self._sent.get(key, -1e12) < self.cooldown:
            return False
        self._sent[key] = now
        if len(self._sent) > MAX_TRACKED:
            self._sent.clear()
            self._fails.clear()
        return True
