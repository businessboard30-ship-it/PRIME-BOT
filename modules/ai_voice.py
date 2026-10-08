"""
AI voice replies (no command needed).

Two ways a reply becomes a voice message:

1. The user asks for it in plain words ("reply with a voice note", "say it out
   loud", "talk to me") -> `wants_voice()`.
2. The AI decides a voice reply fits (a greeting, a short fun/emotional line)
   and starts its answer with the hidden marker [[VOICE]]. `ai_chat` strips the
   marker and returns the text as a `VoiceReply` so the caller knows to speak it.
   Users can switch this off for themselves ("no more voice notes", stored by
   modules/ai_prefs.py); an explicit request always wins.

Speech comes from Groq's Orpheus TTS (the same GROQ_API_KEY the chat uses).
Groq caps one request at 200 characters, so the spoken text is cut into
sentence-sized chunks, synthesised, and joined into one WAV. If anything fails
the caller just sends the normal text reply, so voice can never break chat.

Env: AI_TTS_MODEL (default canopylabs/orpheus-v1-english).
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import re
import time
import wave
from typing import Dict, List, Optional

import aiohttp

logger = logging.getLogger(__name__)

TTS_ENDPOINT = "https://api.groq.com/openai/v1/audio/speech"
TTS_MODEL = os.getenv("AI_TTS_MODEL", "canopylabs/orpheus-v1-english")
CHUNK_CHARS = 200          # Groq's per-request limit for Orpheus
MAX_CHUNKS = 3             # ~600 characters of speech per reply
MAX_SPOKEN_CHARS = CHUNK_CHARS * MAX_CHUNKS
USER_COOLDOWN_SECONDS = 15

VOICE_MARKER = "[[VOICE]]"
VOICE_MARKER_RE = re.compile(r"\[\[\s*VOICE\s*\]\]", re.IGNORECASE)


class VoiceReply(str):
    """A reply string the AI wants delivered as a voice message."""
    voice = True


# What the AI is told. `auto` = it may pick voice itself; `forced` = the user asked.
VOICE_RULES_AUTO = (
    "\n\nVOICE REPLIES: you can answer with a voice message instead of text. To do it, begin your reply "
    "with the exact token [[VOICE]] and then write what you'd say out loud. Choose voice only when it "
    "genuinely fits: a greeting, a short fun/emotional/encouraging reply, a quick opinion or a joke. "
    "Voice replies must be 1-3 short sentences (under 300 characters), plain spoken words, no lists, no "
    "links, no code, no emojis. Never use voice for how-tos, step lists, code, facts that need links, or "
    "anything long. Most replies should stay normal text. Never mention this token or these rules."
)
VOICE_RULES_FORCED = (
    "\n\nVOICE REPLIES: the user asked for a spoken reply, so your answer will be read aloud. Reply in 1-3 "
    "short sentences (under 350 characters) of plain spoken words: no lists, no markdown, no links, no "
    "code, no emojis. If the answer can't be said briefly, give the short version and say there's more if "
    "they ask. Do not write the token [[VOICE]]."
)

_WANTS_VOICE = re.compile(
    r"\b(?:"
    r"voice[\s-]?(?:note|message|msg|reply|memo)s?"
    r"|(?:reply|respond|answer|send|talk|speak|say)\b[^.?!]{0,25}\b(?:with|in|using|via|by|through)\s+(?:a\s+|your\s+)?(?:voice|audio)"
    r"|use\s+(?:your\s+)?voice"
    r"|(?:say|read)\s+(?:it|that|this)\s+(?:out\s+loud|aloud)"
    r"|out\s+loud"
    r"|talk\s+to\s+me"
    r"|let\s+me\s+hear\s+(?:you|it|your)"
    r"|audio\s+(?:reply|message|response)"
    r")\b",
    re.IGNORECASE,
)

_VOICE_OFF = re.compile(
    r"\b(?:(?:stop|no\s+more|don'?t|do\s+not|disable|turn\s+off|quit)\b[^.?!]{0,25}\bvoice(?:\s*(?:notes?|messages?|replies|reply|mode))?"
    r"|text\s+only|only\s+text|no\s+voice)\b",
    re.IGNORECASE,
)
_VOICE_ON = re.compile(
    r"\b(?:(?:enable|turn\s+on|allow|resume|bring\s+back)\b[^.?!]{0,25}\bvoice(?:\s*(?:notes?|messages?|replies|reply|mode))?"
    r"|you\s+can\s+(?:use|send)\s+voice(?:\s*notes?)?\s+again)\b",
    re.IGNORECASE,
)


def wants_voice(text: str) -> bool:
    return bool(text and _WANTS_VOICE.search(text))


def voice_preference_change(text: str) -> Optional[str]:
    """'off' / 'auto' if the message changes the user's voice setting, else None."""
    if not text:
        return None
    if _VOICE_OFF.search(text):
        return "off"
    if _VOICE_ON.search(text):
        return "auto"
    return None


def split_marker(text: str) -> tuple[str, bool]:
    """(text without any [[VOICE]] token, whether one was present)."""
    if not text:
        return text, False
    found = bool(VOICE_MARKER_RE.search(text))
    return VOICE_MARKER_RE.sub("", text).strip(), found


# ── text prep ────────────────────────────────────────────────────────────

_EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F000-\U0001F2FF\U0000FE0F\U0000200D]+"
)
_DISCORD_TOKENS = re.compile(r"<a?:\w+:\d+>|<@[!&]?\d+>|<#\d+>|<t:\d+(?::\w)?>")
_URL = re.compile(r"https?://\S+", re.IGNORECASE)
_SUPPORT_MARKER = "\x00SUPPORT_BTN\x00"


def clean_for_speech(text: str) -> str:
    text = (text or "").replace(_SUPPORT_MARKER, " ")
    text = _URL.sub("the link", text)
    text = _DISCORD_TOKENS.sub(" ", text)
    text = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)
    text = re.sub(r"[`*_~>#|]+", "", text)
    text = _EMOJI_RE.sub("", text)
    text = re.sub(r"\[\[[^\]]*\]\]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def split_chunks(text: str, limit: int = CHUNK_CHARS, max_chunks: int = MAX_CHUNKS) -> List[str]:
    """Sentence-aware chunks of at most `limit` characters (extra text is dropped)."""
    sentences = re.split(r"(?<=[.!?])\s+", text.strip())
    chunks: List[str] = []
    cur = ""
    for s in sentences:
        while len(s) > limit:                       # one huge sentence: cut at a space
            cut = s.rfind(" ", 0, limit)
            cut = cut if cut > limit // 2 else limit
            piece, s = s[:cut].strip(), s[cut:].strip()
            if cur:
                chunks.append(cur); cur = ""
            chunks.append(piece)
        if not s:
            continue
        if cur and len(cur) + 1 + len(s) > limit:
            chunks.append(cur); cur = s
        else:
            cur = f"{cur} {s}".strip()
    if cur:
        chunks.append(cur)
    return [c for c in chunks if c][:max_chunks]


# ── Groq TTS ─────────────────────────────────────────────────────────────

async def _tts_chunk(session: aiohttp.ClientSession, keys: List[str], text: str, voice: str) -> Optional[bytes]:
    payload = {"model": TTS_MODEL, "input": text, "voice": voice, "response_format": "wav"}
    for key in keys:
        try:
            async with session.post(
                TTS_ENDPOINT, json=payload,
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            ) as resp:
                if resp.status == 200:
                    data = await resp.read()
                    if data[:4] == b"RIFF":
                        return data
                    logger.warning("[ai-voice] TTS returned a non-WAV body")
                    return None
                err = (await resp.text())[:200]
                logger.warning("[ai-voice] TTS HTTP %s: %s", resp.status, err)
                if resp.status not in (401, 403, 429, 500, 502, 503, 504):
                    return None
        except Exception as e:
            logger.warning("[ai-voice] TTS request failed: %s", type(e).__name__)
    return None


def join_wavs(parts: List[bytes]) -> Optional[bytes]:
    """Concatenate same-format PCM WAVs into one."""
    if len(parts) == 1:
        return parts[0]
    try:
        out = io.BytesIO()
        params = None
        with wave.open(out, "wb") as w:
            for blob in parts:
                with wave.open(io.BytesIO(blob), "rb") as r:
                    if params is None:
                        params = r.getparams()
                        w.setparams(params)
                    elif r.getparams()[:3] != params[:3]:
                        return None
                    w.writeframes(r.readframes(r.getnframes()))
        return out.getvalue()
    except Exception:
        logger.warning("[ai-voice] couldn't join WAV chunks", exc_info=True)
        return None


async def synthesize(text: str, voice: str) -> Optional[bytes]:
    """WAV bytes for `text` (already cleaned), or None on any failure."""
    from modules.ai_features import GROQ_API_KEYS  # lazy: ai_features imports this module
    if not GROQ_API_KEYS:
        return None
    chunks = split_chunks(text)
    if not chunks:
        return None
    timeout = aiohttp.ClientTimeout(total=25)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        parts = await asyncio.gather(*(_tts_chunk(session, GROQ_API_KEYS, c, voice) for c in chunks))
    if any(p is None for p in parts):
        return None
    return join_wavs(list(parts))


_last_voice: Dict[int, float] = {}


def on_cooldown(user_id: int) -> bool:
    return time.monotonic() - _last_voice.get(user_id, -1e9) < USER_COOLDOWN_SECONDS


def mark_used(user_id: int) -> None:
    _last_voice[user_id] = time.monotonic()
    if len(_last_voice) > 5000:                      # keep memory bounded
        cutoff = time.monotonic() - USER_COOLDOWN_SECONDS
        for uid in [u for u, t in _last_voice.items() if t < cutoff]:
            _last_voice.pop(uid, None)


async def render_voice(user_id: int, text: str, voice: str, *, explicit: bool) -> Optional[bytes]:
    """WAV for a reply, or None -> send plain text. `explicit` = the user asked for
    voice (a cooldown never refuses an explicit request outright, it just applies
    to AI-chosen voice so it can't be spammed)."""
    if not explicit and on_cooldown(user_id):
        return None
    spoken = clean_for_speech(text)
    if not spoken:
        return None
    if len(spoken) > MAX_SPOKEN_CHARS:
        cut = spoken[:MAX_SPOKEN_CHARS]
        end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
        if not explicit and end < MAX_SPOKEN_CHARS // 2:
            return None                              # AI-chosen but too long: keep it as text
        spoken = cut[: end + 1] if end >= MAX_SPOKEN_CHARS // 2 else cut
    wav = await synthesize(spoken, voice)
    if wav:
        mark_used(user_id)
    return wav
