# path: modules/scam_check.py

"""
Manual Scam Shield check: a member replies to someone's message, @mentions the bot and writes "scam shield".

Pure helpers only (trigger parsing, cooldowns, the wording of the answers) so they are easy to test; the Discord
side lives in discord_bot/cogs/scam_shield.py.

Rules the answers follow:
  * a confident match (known scam rule, or the AI scan said scam) is DELETED when the bot can; when it can't, the
    answer says why;
  * "nothing found" never says "safe": no match is not proof;
  * messages from server staff are not judged;
  * the answer says what kind of match it was, never the exact rule (that would help scammers dodge it).
"""

from __future__ import annotations

import re
import time
from typing import Dict, Optional, Tuple

ANSWER_SECONDS = 90                      # the answer removes itself after this long
USER_COOLDOWN_SECONDS = 20.0
GUILD_PER_MINUTE = 8
MAX_WORDS = 5

_MENTION = re.compile(r"<@[!&]?\d+>")
_PUNCT = re.compile(r"[^a-z0-9 ]+")
_PHRASES = ("scam shield", "scamshield", "scam check", "scamcheck")
_FILLER = {"please", "pls", "plz", "this", "it", "check", "run", "the", "a", "for", "is", "scam", "shield", "scamshield", "scamcheck", "on", "that"}


def is_check_request(content: str) -> bool:
    """True for a short message that, once the @mention is removed, asks for a scam shield check
    ("scam shield", "@bot scam shield please", "scamshield this"). Longer sentences are normal chat."""
    text = _PUNCT.sub(" ", _MENTION.sub(" ", (content or "").lower()))
    words = text.split()
    if not words or len(words) > MAX_WORDS:
        return False
    joined = " ".join(words)
    if not any(p in joined for p in _PHRASES):
        return False
    return all(w in _FILLER for w in words)


class Cooldown:
    """Per-member and per-server limits for manual checks (in memory, per process)."""

    def __init__(self) -> None:
        self._user: Dict[Tuple[int, int], float] = {}
        self._guild: Dict[int, list] = {}

    def wait_seconds(self, guild_id: int, user_id: int, now: Optional[float] = None) -> float:
        """0 when allowed (and the use is recorded); otherwise how many seconds to wait."""
        now = time.monotonic() if now is None else now
        last = self._user.get((guild_id, user_id))
        if last is not None and now - last < USER_COOLDOWN_SECONDS:
            return USER_COOLDOWN_SECONDS - (now - last)
        recent = [t for t in self._guild.get(guild_id, []) if now - t < 60]
        if len(recent) >= GUILD_PER_MINUTE:
            return 60 - (now - recent[0])
        recent.append(now)
        self._guild[guild_id] = recent
        self._user[(guild_id, user_id)] = now
        if len(self._user) > 5000:
            self._user = {k: v for k, v in self._user.items() if now - v < USER_COOLDOWN_SECONDS}
            self._guild = {k: v for k, v in self._guild.items() if v and now - v[-1] < 60}
        return 0.0


KIND_WORDS = {"text": "a known scam message", "domain": "a link to a known scam site", "image": "a known scam picture",
              "vision": "a picture the AI scan flagged as a scam"}


def kind_word(kind: str) -> str:
    return KIND_WORDS.get(str(kind), "a known scam pattern")


def match_text(kind: str) -> str:
    return (f"⚠️ **This looks like a scam.** It matches {kind_word(kind)}.\n"
            "Don't click links or scan codes in it, and never share your login or send crypto to claim a prize. "
            "Tap the button to tell the server staff.")


NO_PERMS_TEXT = "I'm missing the **Manage Messages** permission in {channel}."
FORBIDDEN_TEXT = ("Discord refused the delete (I'm missing **Manage Messages** there, or a channel permission override "
                  "is blocking me).")
ERROR_TEXT = "Discord returned an error while I tried to delete it (code {code}). Try again in a moment."
OTHER_BOT_TEXT = ("⚠️ **This looks like a scam**, and another bot in this server is already removing it. "
                  "If it's still there in a minute, tell a moderator.")


def deleted_text(kind: str) -> str:
    return (f"⚠️ **That was a scam, and I deleted it.** It matched {kind_word(kind)}.\n"
            "If you clicked a link or scanned a code from it, change your passwords and tell a moderator.")


def not_deleted_text(kind: str, reason: str) -> str:
    return (f"⚠️ **This looks like a scam** (it matches {kind_word(kind)}), but **I could not delete it.**\n"
            f"Reason: {reason}\n"
            "Don't click links or scan codes in it. Tap the button to tell the server staff.")


def clean_text(pictures_unchecked: bool) -> str:
    base = ("✅ I found **no known scam pattern** in that message.\n"
            "That does **not** mean it is safe. New scams appear all the time, so be careful with links, never share your "
            "login or recovery phrase, and ask a moderator if you're unsure.")
    if pictures_unchecked:
        base += "\n-# I couldn't scan its picture(s) right now (the AI picture scan is off or at its daily limit)."
    return base


STAFF_TEXT = ("That message is from server staff, so I don't judge it. If you think their account was hacked, "
              "tell another staff member directly.")
OFF_TEXT = "Scam Shield is switched off in this server, so I can't check messages here."
NOTHING_TEXT = "There's nothing in that message for me to check (no text or pictures)."
GONE_TEXT = "I couldn't find the message you replied to."


def wait_text(seconds: float) -> str:
    return f"Easy there, try again in {max(1, int(seconds + 0.99))}s."
