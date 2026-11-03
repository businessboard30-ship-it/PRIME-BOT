# path: modules/ai_trust.py

"""Phase 7: who wrote the context the AI is reading? Command tools are only offered when every HUMAN message in the conversation
context was written by the person who is asking. Anything else (a stranger's recent channel message, someone else in a reply
chain) is untrusted content: the AI may read it as data, but it can never be the reason a command runs with the asker's
authority. Execution still re-checks permissions and confirmations; this closes the "confused deputy" gap earlier in the chain."""

from typing import Iterable, Optional

# Flip to False to restore tools in mixed conversations (not recommended).
REPLY_TOOLS_REQUIRE_OWN_CONTEXT = True


class ChatHistory(list):
    """A plain history list that remembers which humans wrote it."""

    def __init__(self, messages: Iterable = (), human_authors: Optional[Iterable[int]] = None):
        super().__init__(messages)
        self.human_authors = frozenset(human_authors or ())


def has_foreign_humans(history, asker_id: int) -> bool:
    """True when the context holds a human message from anyone but the asker. Unknown authorship with user turns fails CLOSED."""
    if not history:
        return False
    authors = getattr(history, "human_authors", None)
    if authors is None:
        return any(isinstance(m, dict) and m.get("role") == "user" for m in history)
    return any(a != asker_id for a in authors)


def tools_allowed(history, asker_id: int) -> bool:
    return not (REPLY_TOOLS_REQUIRE_OWN_CONTEXT and has_foreign_humans(history, asker_id))
