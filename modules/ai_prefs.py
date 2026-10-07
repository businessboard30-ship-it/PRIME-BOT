"""
Per-user AI preferences: which character the AI talks as, and whether it may
choose to reply with voice. No slash command: people change these by just
asking in chat ("switch to gentle", "change character", "no more voice
notes") — see AIToolsCog._prefs_quick_answer.

Characters
----------
Three of them. Edit CHARACTERS to rename or swap one; everything else (the
picker, the chat prompt, the voice) reads from it. A character only changes
tone and style — BOT_RULES in ai_features.py always apply on top.

Storage: one small table, created on first use (same convention as the rest
of the bot: the Python module owns its own CREATE TABLE IF NOT EXISTS).
Reads are cached for a few minutes so chatting doesn't add a query per message.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

DEFAULT_CHARACTER = "genz"
VOICE_AUTO = "auto"
VOICE_OFF = "off"

# key -> label, emoji, one-line blurb for the picker, style prompt, TTS voice,
# the line the character says when picked.
CHARACTERS: Dict[str, dict] = {
    "genz": {
        "label": "Gen Z",
        "emoji": "😎",
        "blurb": "Casual, funny and chronically online",
        "voice": "autumn",
        "style": (
            "CHARACTER — Gen Z: you talk like a witty, chronically-online Gen Z friend. Relaxed lowercase-ish "
            "texting energy, current slang used naturally (fr, lowkey, ngl, no cap, bet, cooked) without "
            "overdoing it, playful teasing, the odd emoji. Hype people up. Stay genuinely helpful and accurate "
            "underneath the vibe, and drop the slang if someone is upset or asks something serious."
        ),
        "intro": "bet, gen z mode on 😎 what's good?",
    },
    "gentle": {
        "label": "Gentle",
        "emoji": "🌸",
        "blurb": "Soft, warm and patient",
        "voice": "diana",
        "style": (
            "CHARACTER — Gentle: you are a soft-spoken, kind and patient companion. Warm, calm, encouraging "
            "wording, simple clear language, no slang and no sarcasm. Reassure before you correct, explain "
            "step by step when something is confusing, celebrate small wins, and check in kindly when someone "
            "seems stressed. Use at most one soft emoji now and then."
        ),
        "intro": "Of course 🌸 I'll be gentle. What's on your mind?",
    },
    "sensei": {
        "label": "Sensei",
        "emoji": "🥋",
        "blurb": "Calm, wise mentor with dry humor",
        "voice": "daniel",
        "style": (
            "CHARACTER — Sensei: you are a calm, wise anime-style mentor. Measured, concise lines, the "
            "occasional short proverb or training metaphor, dry understated humor, and quiet confidence in the "
            "person you're talking to. Give straight, practical guidance rather than fluff. No slang, rarely "
            "any emoji."
        ),
        "intro": "Very well. Sensei is listening. 🥋 Ask your question.",
    },
}

_CACHE_TTL = 300
_cache: Dict[int, Tuple[str, str, float]] = {}
_table_ready = False


async def _pool():
    from database import get_pool  # lazy: keeps this module importable in tests
    return await get_pool()


async def _ensure_table(conn) -> None:
    global _table_ready
    if _table_ready:
        return
    await conn.execute(
        "CREATE TABLE IF NOT EXISTS ai_user_prefs ("
        " user_id BIGINT PRIMARY KEY,"
        " persona TEXT NOT NULL DEFAULT 'genz',"
        " voice_mode TEXT NOT NULL DEFAULT 'auto',"
        " updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())"
    )
    _table_ready = True


async def get_prefs(user_id: int) -> Tuple[str, str]:
    """(character_key, voice_mode). Falls back to defaults on any problem."""
    hit = _cache.get(user_id)
    if hit and time.monotonic() - hit[2] < _CACHE_TTL:
        return hit[0], hit[1]
    character, voice_mode = DEFAULT_CHARACTER, VOICE_AUTO
    try:
        pool = await _pool()
        async with pool.acquire() as conn:
            await _ensure_table(conn)
            row = await conn.fetchrow(
                "SELECT persona, voice_mode FROM ai_user_prefs WHERE user_id = $1", user_id)
        if row:
            character = row["persona"] if row["persona"] in CHARACTERS else DEFAULT_CHARACTER
            voice_mode = row["voice_mode"] if row["voice_mode"] in (VOICE_AUTO, VOICE_OFF) else VOICE_AUTO
    except Exception:
        logger.debug("[ai-prefs] couldn't load prefs for %s; using defaults", user_id, exc_info=True)
        return character, voice_mode          # don't cache a failure
    _cache[user_id] = (character, voice_mode, time.monotonic())
    if len(_cache) > 5000:
        _cache.clear()
    return character, voice_mode


async def _save(user_id: int, character: str, voice_mode: str) -> None:
    pool = await _pool()
    async with pool.acquire() as conn:
        await _ensure_table(conn)
        await conn.execute(
            "INSERT INTO ai_user_prefs (user_id, persona, voice_mode, updated_at) "
            "VALUES ($1, $2, $3, NOW()) "
            "ON CONFLICT (user_id) DO UPDATE SET persona = $2, voice_mode = $3, updated_at = NOW()",
            user_id, character, voice_mode,
        )
    _cache[user_id] = (character, voice_mode, time.monotonic())


async def set_character(user_id: int, character: str) -> bool:
    if character not in CHARACTERS:
        return False
    _, voice_mode = await get_prefs(user_id)
    try:
        await _save(user_id, character, voice_mode)
        return True
    except Exception:
        logger.exception("[ai-prefs] couldn't save character for %s", user_id)
        return False


async def set_voice_mode(user_id: int, mode: str) -> bool:
    if mode not in (VOICE_AUTO, VOICE_OFF):
        return False
    character, _ = await get_prefs(user_id)
    try:
        await _save(user_id, character, mode)
        return True
    except Exception:
        logger.exception("[ai-prefs] couldn't save voice mode for %s", user_id)
        return False


def style_prompt(character: str) -> str:
    return CHARACTERS.get(character, CHARACTERS[DEFAULT_CHARACTER])["style"]


def tts_voice(character: str) -> str:
    return CHARACTERS.get(character, CHARACTERS[DEFAULT_CHARACTER])["voice"]


# ── understanding "switch to gentle" / "change character" in plain chat ──

_ALIASES = {
    "genz": re.compile(r"gen\s*-?\s*z", re.IGNORECASE),
    "gentle": re.compile(r"gentle", re.IGNORECASE),
    "sensei": re.compile(r"sensei", re.IGNORECASE),
}
_NAMES = r"(gen\s*-?\s*z|gentle|sensei)"
_SWITCH = re.compile(
    rf"\b(?:switch|change|set|turn|go|swap)\b[^.?!]{{0,40}}?\b(?:to|into|as)\s+(?:the\s+|a\s+)?{_NAMES}\b"
    rf"|\b(?:talk|speak|reply|respond|act|chat)\s+(?:to\s+me\s+)?(?:like|as|in)\s+(?:a\s+|the\s+)?{_NAMES}\b"
    rf"|\b{_NAMES}\s+(?:mode|character|persona|personality)\b",
    re.IGNORECASE,
)
# Deliberately needs "your/the AI's/bot's" or an explicit change/switch verb so
# "show me anime characters" or "pick a character from Naruto" never trigger it.
_MENU = re.compile(
    r"\b(?:change|switch|swap|pick|choose|select)\b[^.?!]{0,20}?\b(?:your|ur|the\s+ai'?s?|the\s+bot'?s?|ai|bot)\s+"
    r"(?:character|persona|personality)\b"
    r"|\b(?:change|switch|swap)\s+(?:the\s+)?(?:character|persona|personality)\b"
    r"|\b(?:what|which)\s+(?:characters?|personas?|personalities)\s+"
    r"(?:do\s+you\s+have|can\s+you\s+(?:be|do|play|use)|are\s+there|are\s+available)\b",
    re.IGNORECASE,
)


def detect_character_request(text: str) -> Optional[str]:
    """A character key, 'menu' (show the picker), or None."""
    if not text:
        return None
    m = _SWITCH.search(text)
    if m:
        named = next((g for g in m.groups() if g), "")
        for key, pat in _ALIASES.items():
            if pat.fullmatch(named.strip()):
                return key
    if _MENU.search(text):
        return "menu"
    return None
