# path: modules/ai_names.py

"""
Pure helpers for telling a CATEGORY apart from a CHANNEL that carries the same name (e.g. a category "gallo" and a text
channel "gallo"). Nothing here touches Discord: objects only need a `.type` whose `.name` is "category" for categories
(true of discord.py's ChannelType.category, and of the fakes used in tests).

Rules (the same everywhere the AI resolves a channel by name):
  * the person's wording wins: "category gallo", "gallo category", "category: gallo", "cat:gallo", "gallo (category)"
    -> categories only; "channel gallo", "#gallo", "gallo channel" -> channels only;
  * with no wording to go on, a category and a real channel sharing a name is NOT ambiguous: a plain name (and
    especially a #name) means the channel. Two channels (or two categories) with the same name still ask which one.
"""

import re
from typing import Callable, List, Optional, Tuple

CATEGORY, CHANNEL = "category", "channel"

_HINT_PATTERNS = (
    (re.compile(r"^\s*(?:the\s+)?(?:category|cat)\s*[:\-]?\s+(.+?)\s*$", re.I), CATEGORY),
    (re.compile(r"^\s*(?:the\s+)?(?:category|cat)\s*:\s*(.+?)\s*$", re.I), CATEGORY),
    (re.compile(r"^\s*(.+?)\s+(?:category|cat)\s*$", re.I), CATEGORY),
    (re.compile(r"^\s*(.+?)\s*[\(\[]\s*(?:category|cat)\s*[\)\]]\s*$", re.I), CATEGORY),
    (re.compile(r"^\s*(?:the\s+)?(?:text\s+|voice\s+)?channel\s*[:\-]?\s+(.+?)\s*$", re.I), CHANNEL),
    (re.compile(r"^\s*(.+?)\s+(?:text\s+|voice\s+)?channel\s*$", re.I), CHANNEL),
    (re.compile(r"^\s*(.+?)\s*[\(\[]\s*(?:text\s+|voice\s+)?channel\s*[\)\]]\s*$", re.I), CHANNEL),
)


def split_kind_hint(label) -> Tuple[str, Optional[str]]:
    """'category gallo' -> ('gallo', 'category'); '#gallo' -> ('gallo', 'channel'); 'gallo' -> ('gallo', None).
    A name that IS just the word ('category') is left alone."""
    text = str(label or "").strip()
    if not text:
        return "", None
    if text.startswith("#"):
        return text.lstrip("#").strip(), CHANNEL
    for pattern, hint in _HINT_PATTERNS:
        m = pattern.match(text)
        if m and m.group(1).strip().lower() not in ("category", "cat", "channel"):
            name = re.sub(r"^the\s+", "", m.group(1).strip(), flags=re.I)
            return name.lstrip("#").strip(), hint
    return text, None


def is_category(obj) -> bool:
    return getattr(getattr(obj, "type", None), "name", None) == "category"


def narrow(matches: List, hint: Optional[str] = None, is_cat: Callable = is_category) -> List:
    """Cuts a same-name match list down by kind. Only ever REMOVES wrong-kind entries when that leaves something;
    several of the same kind stay several (the caller asks which one)."""
    matches = list(matches)
    cats = [m for m in matches if is_cat(m)]
    chans = [m for m in matches if not is_cat(m)]
    if not cats or not chans:
        return matches
    if hint == CATEGORY:
        return cats
    return chans                                    # CHANNEL hint, or no hint at all: the real channel


def kind_label(obj) -> str:
    return "category" if is_category(obj) else "channel"
