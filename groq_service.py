import aiohttp
import re
from typing import List, Optional
from datetime import datetime
import os

from i18n import language_instruction

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
# Optional second Groq key (separate account/project) — pure failover, not
# load-balanced. See _groq_post below: tries GROQ_API_KEY first and only
# falls through to this one on an error a different key could plausibly
# fix (auth/rate limit/server error/timeout).
GROQ_API_KEY_BACKUP = os.getenv("GROQ_API_KEY_BACKUP")
GROQ_API_KEYS = [k for k in (GROQ_API_KEY, GROQ_API_KEY_BACKUP) if k]
GROQ_ENDPOINT = "https://api.groq.com/openai/v1/chat/completions"

# Status codes worth retrying with the backup key — auth problems (this key
# specifically is bad/disabled), rate limits, and Groq-side server errors.
_GROQ_RETRYABLE_STATUSES = {401, 403, 429, 500, 502, 503, 504}


async def _groq_post(payload: dict, timeout_seconds: int = 10):
    """POSTs payload to GROQ_ENDPOINT, trying each key in GROQ_API_KEYS in
    order. Moves on to the next key only on a retryable status (see
    _GROQ_RETRYABLE_STATUSES) or a network-level exception — a normal 200
    or a non-retryable error status is returned immediately.

    Returns (status, data_or_None, error_text_or_None), same contract as
    modules.ai_features._groq_post."""
    last_status = None
    last_error = None
    for key in GROQ_API_KEYS:
        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    GROQ_ENDPOINT, json=payload, headers=headers,
                    timeout=aiohttp.ClientTimeout(total=timeout_seconds),
                ) as resp:
                    if resp.status == 200:
                        return 200, await resp.json(), None
                    error_text = await resp.text()
                    last_status, last_error = resp.status, error_text
                    if resp.status not in _GROQ_RETRYABLE_STATUSES:
                        return resp.status, None, error_text
        except Exception as e:
            last_status, last_error = None, f"{type(e).__name__}: {e}"
    return last_status, None, last_error

class GroqService:
    """AI service for anime recommendations and summaries using Groq API"""
    
    def __init__(self):
        self.model = "llama-3.1-70b-versatile"  # Active Groq model (mixtral-8x7b deprecated in Mar 2025)
        self.cache = {}
        self.cache_ttl = 86400  # 24 hours
    
    async def get_anime_recommendation(self, user_preferences: str, anime_watched: List[str], language: str = "en") -> str:
        """Get AI-powered anime recommendation based on user preferences.
        language: 2-letter code from i18n.SUPPORTED_LANGUAGES; the model is
        instructed to answer in that language (see i18n.language_instruction)."""
        if not GROQ_API_KEYS:
            return "Yo, AI recommendations are currently down. Try again later! 🤖"
        
        watched_list = ", ".join(anime_watched[-5:]) if anime_watched else "No anime watched yet"
        
        # Check cache first (keyed per language so translations don't collide)
        cache_key = f"rec_{language}_{hash(user_preferences + watched_list)}"
        cached = self._get_cache_key(cache_key)
        if cached:
            return cached
        
        prompt = f"""You are a cool, Gen Z anime expert assistant. Recommend anime based on these preferences:
        
User Preferences: {user_preferences}
Recently Watched: {watched_list}

Give a SHORT, CASUAL recommendation (2-3 sentences MAX). Use Gen Z slang, be chill about it.
Format: "[Anime Name] - why it slaps for you 🎬"

Keep it under 150 characters total.{language_instruction(language)}"""
        
        try:
            payload = {
                "model": self.model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.7,
                "max_tokens": 100,
            }
            status, data, _error = await _groq_post(payload, timeout_seconds=10)
            if status == 200:
                result = data["choices"][0]["message"]["content"].strip()
                self._set_cache(cache_key, result)
                return result
            else:
                return f"Recommendation failed. Try again! ({status})"
        except Exception as e:
            return f"Oops! AI is sleeping rn. Error: {str(e)[:30]}"
    
    async def get_anime_summary(self, anime_title: str, anime_description: str, language: str = "en") -> str:
        """Generate Gen Z-style summary of an anime.
        language: 2-letter code from i18n.SUPPORTED_LANGUAGES."""
        if not GROQ_API_KEYS:
            return "Summaries are offline atm! 😴"
        
        # Check cache first (keyed per language so translations don't collide)
        cache_key = f"sum_{language}_{hash(anime_title + anime_description)}"
        cached = self._get_cache_key(cache_key)
        if cached:
            return cached
        
        prompt = f"""You are a Gen Z anime expert. Summarize this anime in the most casual, trendy way:

Anime: {anime_title}
Description: {anime_description[:500]}

Write a SUPER SHORT summary (1 sentence, max 100 chars) using Gen Z slang.
Make it sound like you're texting a friend about it.
Use relevant emojis.

Example: "bro this anime is literally insane, the action hits different fr fr 🔥"

Just give the summary, nothing else.{language_instruction(language)}"""
        
        try:
            payload = {
                "model": self.model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.8,
                "max_tokens": 80,
            }
            status, data, _error = await _groq_post(payload, timeout_seconds=10)
            if status == 200:
                result = data["choices"][0]["message"]["content"].strip()
                self._set_cache(cache_key, result)
                return result
            else:
                return "Can't summarize rn! Try later 😅"
        except Exception as e:
            return f"Summary failed lol. {str(e)[:20]}"
    
    def _get_cache_key(self, key: str) -> Optional[str]:
        """Get value from cache if not expired"""
        if key in self.cache:
            entry = self.cache[key]
            if datetime.now().timestamp() - entry["timestamp"] < self.cache_ttl:
                return entry["value"]
        return None
    
    def _set_cache(self, key: str, value: str):
        """Store value in cache"""
        self.cache[key] = {
            "value": value,
            "timestamp": datetime.now().timestamp()
        }


# Global instance
groq_service = GroqService()


_PLACEHOLDER_RE = re.compile(r"\{[^{}]*\}")


async def translate_ui_string(text: str, target_language_name: str) -> str:
    """Translate a single UI template string for i18n.tr()/tr_sync().

    `target_language_name` is the English name of the language (e.g.
    "French"), matching i18n.LLM_LANGUAGE_NAME. Any {placeholder} tokens in
    `text` (Discord embed field values, usernames, numbers, etc. get
    .format()-ed in after translation) must come back byte-for-byte
    unchanged — the prompt instructs the model to leave them untouched, and
    the result is checked and rejected (falls back to the English original)
    if any placeholder was dropped, translated, or reordered in a way that
    breaks .format().

    Raises on missing API key / network / bad response so callers (i18n.tr)
    can decide how to fall back; this function never silently returns
    mistranslated or corrupted output.
    """
    if not GROQ_API_KEYS:
        raise RuntimeError("GROQ_API_KEY not configured")

    placeholders = _PLACEHOLDER_RE.findall(text)

    prompt = f"""Translate the following Discord bot UI string into {target_language_name}.

Rules:
- Output ONLY the translated string, nothing else — no quotes, no explanation, no preamble.
- Preserve Discord markdown exactly as-is (**bold**, *italic*, `code`, emoji).
- Preserve every {{placeholder}} token exactly as written, in the same order, character-for-character. Do not translate, rename, reorder, or remove them.
- Keep the tone casual/friendly to match the English original.

String to translate:
{text}"""

    payload = {
        "model": "llama-3.1-70b-versatile",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3,
        "max_tokens": 300,
    }
    status, data, error_text = await _groq_post(payload, timeout_seconds=15)
    if status != 200:
        raise RuntimeError(f"Groq translation request failed: HTTP {status}: {(error_text or '')[:200]}")
    translated = data["choices"][0]["message"]["content"].strip()

    # Strip stray wrapping quotes some models add despite the instruction.
    if len(translated) >= 2 and translated[0] == translated[-1] and translated[0] in "\"'":
        translated = translated[1:-1]

    # Placeholder integrity check — if the model mangled any {token}, don't
    # ship a translation that will raise (or silently drop data) at
    # .format() time; let the caller fall back to English instead.
    if _PLACEHOLDER_RE.findall(translated) != placeholders:
        raise ValueError(
            f"Placeholder mismatch translating to {target_language_name}: "
            f"expected {placeholders}, got {_PLACEHOLDER_RE.findall(translated)}"
        )

    return translated
