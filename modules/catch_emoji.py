"""Emoji and small text-art used by the catch screens (Phase 3).

EVERY emoji the new screens show lives in this one file, so the owner can restyle the
whole game in one place later. To use a server custom emoji, replace a value with its
markup, for example ``"<:ember:123456789012345678>"`` or an animated one,
``"<a:crown:123456789012345678>"``. Nothing else needs to change.

Rules for editing:
* Keep every key. A test fails if a rarity, element, stat or flag has no emoji.
* Values are plain strings. Keep each short (a custom emoji markup is about 30 characters).
* Unknown keys never crash a screen: ``mark`` falls back to ``FALLBACK``.
"""

from __future__ import annotations

FALLBACK = "▫️"

RARITY: dict[str, str] = {
    "common": "🩶",
    "uncommon": "💚",
    "rare": "💠",
    "epic": "🔮",
    "mythic": "👑",
}

ELEMENT: dict[str, str] = {
    "ember": "🌋",
    "tide": "🌊",
    "verdant": "🪴",
    "volt": "🌩️",
    "frost": "🧊",
    "stone": "🪨",
    "gale": "🌪️",
    "umbra": "🌑",
    "lumen": "🪔",
}

STAT: dict[str, str] = {
    "vigor": "🫀",
    "power": "🗡️",
    "guard": "🛡️",
    "speed": "🪽",
    "spirit": "🧿",
}

FLAG: dict[str, str] = {
    "shiny": "🌠",
    "special": "🎆",
    "favorite": "💖",
    "locked": "🔐",
    "buddy": "🐾",
}

UI: dict[str, str] = {
    "evolve": "🧬",
    "evolved": "🎇",
    "profile": "🪪",
    "streak": "🔥",
    "daily": "📅",
    "dex": "📖",
    "collection": "🎒",
    "rarest": "💎",
    "nickname": "🏷️",
    "back": "↩️",
    "ok": "✅",
    "arrow": "➜",
    "sparkle": "✨",
    "coins": "🪙",
    "filter": "🔎",
    "jump": "🔢",
    "clear": "🧹",
    "release": "🕊️",
}

# Progress-bar pieces (plain text on purpose: they render the same on every device).
BAR_FULL = "▰"
BAR_EMPTY = "▱"
BAR_LENGTH = 10
STAT_BAR_MAX = 300  # a stat of 300 or more fills the bar (level 100, strong species)

GROUPS: dict[str, dict[str, str]] = {
    "rarity": RARITY, "element": ELEMENT, "stat": STAT, "flag": FLAG, "ui": UI,
}


def mark(group: str, key: str) -> str:
    """Emoji for ``key`` in ``group``; never raises."""
    return GROUPS.get(group, {}).get(key, FALLBACK)


def bar(value: int, maximum: int = STAT_BAR_MAX, length: int = BAR_LENGTH) -> str:
    """``▰▰▰▱▱▱▱▱▱▱``-style bar. Any positive value shows at least one block."""
    maximum = max(1, int(maximum))
    value = max(0, int(value))
    filled = min(length, max(1 if value > 0 else 0, round(length * value / maximum)))
    return BAR_FULL * filled + BAR_EMPTY * (length - filled)


def flags(*, shiny: bool = False, special: bool = False, favorite: bool = False,
          locked: bool = False, buddy: bool = False) -> str:
    """Concatenated flag emoji for a creature line (empty string when none apply)."""
    parts = []
    if buddy:
        parts.append(FLAG["buddy"])
    if special:
        parts.append(FLAG["special"])
    if shiny:
        parts.append(FLAG["shiny"])
    if favorite:
        parts.append(FLAG["favorite"])
    if locked:
        parts.append(FLAG["locked"])
    return "".join(parts)


__all__ = [
    "BAR_EMPTY", "BAR_FULL", "BAR_LENGTH", "ELEMENT", "FALLBACK", "FLAG", "GROUPS", "RARITY",
    "STAT", "STAT_BAR_MAX", "UI", "bar", "flags", "mark",
]
