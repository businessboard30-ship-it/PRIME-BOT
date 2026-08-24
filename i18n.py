"""
i18n.py — lightweight translation layer for static bot UI text.

Design:
- Static strings (menus, buttons, disclaimers, error messages) live in
  locales/<code>.json and are looked up with t(key, lang).  These are
  human-written/reviewed, not machine-translated at runtime, so they stay
  fast and reliable even if an AI provider is down.
- Dynamic content (AI chat/recommendations from groq_service.py) is instead
  steered at generation time via language_instruction(lang), which appends
  a "respond in <language>" directive to the prompt sent to the model. See
  groq_service.py for usage.

Adding a language:
1. Copy locales/en.json to locales/<code>.json and translate the values.
2. Add the code + native display name to SUPPORTED_LANGUAGES below.
That's it — the /language picker and t() pick it up automatically.

Note: only the strings above are covered by static locale files so far.
Rolling this out to every handler's text is a larger, incremental job —
this module is meant to make that mechanical (import t, replace the
hardcoded string, add the key to each locale file).
"""

import json
import os
import logging

logger = logging.getLogger(__name__)

LOCALES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "locales")

DEFAULT_LANGUAGE = "en"

# code -> native display name (shown in the /language picker)
SUPPORTED_LANGUAGES = {
    "en": "English",
    "fr": "Français",
    "it": "Italiano",
    "ru": "Русский",
    "pt": "Português",
    "es": "Español",
    "ar": "العربية",
    "de": "Deutsch",
    "sw": "Kiswahili",
    "ja": "日本語",
}

# Language name to feed the LLM for dynamic translation (English name reads
# more reliably as a model instruction than the native name in every case).
LLM_LANGUAGE_NAME = {
    "en": "English",
    "fr": "French",
    "it": "Italian",
    "ru": "Russian",
    "pt": "Portuguese",
    "es": "Spanish",
    "ar": "Arabic",
    "de": "German",
    "sw": "Swahili",
    "ja": "Japanese",
}

_locales_cache = {}


def _load_locale(lang: str) -> dict:
    if lang in _locales_cache:
        return _locales_cache[lang]

    path = os.path.join(LOCALES_DIR, f"{lang}.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        logger.warning(f"[i18n] No locale file for '{lang}', falling back to {DEFAULT_LANGUAGE}")
        if lang == DEFAULT_LANGUAGE:
            data = {}
        else:
            data = _load_locale(DEFAULT_LANGUAGE)
    except Exception as e:
        logger.error(f"[i18n] Failed to load locale '{lang}': {e}")
        data = {}

    _locales_cache[lang] = data
    return data


def t(key: str, lang: str = DEFAULT_LANGUAGE, **kwargs) -> str:
    """
    Look up a static UI string by key for the given language, falling back
    to English and then to the raw key if nothing is found. Any kwargs are
    used to .format() the string (e.g. t("welcome_default", lang, name="Rin")).
    """
    lang = lang if lang in SUPPORTED_LANGUAGES else DEFAULT_LANGUAGE
    locale = _load_locale(lang)

    text = locale.get(key)
    if text is None and lang != DEFAULT_LANGUAGE:
        text = _load_locale(DEFAULT_LANGUAGE).get(key)
    if text is None:
        logger.warning(f"[i18n] Missing key '{key}' in all locales")
        return key

    try:
        return text.format(**kwargs)
    except (KeyError, IndexError) as e:
        logger.error(f"[i18n] Format error for key '{key}' ({lang}): {e}")
        return text


# ---------------------------------------------------------------------------
# AI-translated UI strings (tr / tr_sync)
#
# The curated t()/locales/<code>.json system above only covers a handful of
# hand-reviewed keys. Everything added in Phase 3/4 (economy, automation,
# automod, premium, dashboard) is written as plain English template strings
# in the cogs, e.g. tr("You need **{amount}** coins to do that.", lang,
# amount=500). Instead of hand-writing a short key + translating it in 9
# locale files by hand, these templates are translated by Groq:
#
#   1. Offline/batch: `python scripts/generate_ai_locales.py` scans the cogs
#      for tr(...) call sites, sends every distinct English template to
#      Groq once per supported language, and writes the results into
#      locales/ai/<lang>.json. This is the "generate the locale files now"
#      step — run it after adding/changing any tr() strings, and commit the
#      resulting locales/ai/*.json so normal runtime traffic never needs a
#      live translation call.
#   2. Runtime fallback: if a template shows up that isn't in
#      locales/ai/<lang>.json yet (new string shipped without re-running the
#      batch script, or the batch script hasn't been run at all), tr()
#      translates it live via Groq on first use, then caches the result
#      in-memory AND appends it to locales/ai/<lang>.json on disk so every
#      later call (any guild, any process restart) is a cache hit.
#
# {placeholders} inside the template are preserved verbatim by the
# translation prompt (Groq is told not to translate bracketed tokens), then
# filled in with .format(**kwargs) same as t().
# ---------------------------------------------------------------------------

import asyncio
import hashlib

AI_LOCALES_DIR = os.path.join(LOCALES_DIR, "ai")
os.makedirs(AI_LOCALES_DIR, exist_ok=True)

_ai_cache = {}  # lang -> {template_hash: translated_template}
_ai_cache_locks = {}  # lang -> asyncio.Lock, so concurrent requests for the
                       # same missing string don't fire duplicate API calls


def _template_hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def _load_ai_locale(lang: str) -> dict:
    if lang in _ai_cache:
        return _ai_cache[lang]
    path = os.path.join(AI_LOCALES_DIR, f"{lang}.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        data = {}
    except Exception as e:
        logger.error(f"[i18n] Failed to load AI locale '{lang}': {e}")
        data = {}
    _ai_cache[lang] = data
    return data


def _save_ai_locale(lang: str):
    path = os.path.join(AI_LOCALES_DIR, f"{lang}.json")
    tmp_path = path + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(_ai_cache.get(lang, {}), f, ensure_ascii=False, indent=2, sort_keys=True)
        os.replace(tmp_path, path)
    except Exception as e:
        logger.error(f"[i18n] Failed to persist AI locale '{lang}': {e}")


async def _translate_template_via_groq(text: str, lang: str) -> str:
    """Ask Groq to translate an English UI template into `lang`, preserving
    any {placeholder} tokens untouched. Returns `text` unchanged on any
    failure (missing key, network error, bad response) so callers always
    get something sendable instead of raising mid-command."""
    from groq_service import translate_ui_string  # local import: avoids a
    # circular import at module load time (groq_service doesn't import i18n
    # at the top level either, but this keeps load order irrelevant).

    try:
        return await translate_ui_string(text, LLM_LANGUAGE_NAME.get(lang, lang))
    except Exception as e:
        logger.error(f"[i18n] Live translation failed for '{lang}': {e}")
        return text


async def tr(text: str, lang: str = DEFAULT_LANGUAGE, **kwargs) -> str:
    """Translate-and-format an English UI template.

    Usage: await tr("You have **{balance}** coins.", lang, balance=500)

    - lang == 'en' (or unsupported): formats and returns immediately, no
      translation involved.
    - Cache hit (locales/ai/<lang>.json, from the batch script or an
      earlier live call): formats and returns immediately, no API call.
    - Cache miss: translates live via Groq, caches the result (memory +
      disk), then formats and returns it.
    """
    if lang == DEFAULT_LANGUAGE or lang not in SUPPORTED_LANGUAGES:
        return text.format(**kwargs) if kwargs else text

    key = _template_hash(text)
    cache = _load_ai_locale(lang)

    if key not in cache:
        lock = _ai_cache_locks.setdefault(lang, asyncio.Lock())
        async with lock:
            cache = _load_ai_locale(lang)  # re-check after acquiring lock
            if key not in cache:
                translated = await _translate_template_via_groq(text, lang)
                cache[key] = translated
                _ai_cache[lang] = cache
                _save_ai_locale(lang)

    template = cache.get(key, text)
    try:
        return template.format(**kwargs) if kwargs else template
    except (KeyError, IndexError) as e:
        logger.error(f"[i18n] tr() format error ({lang}) for '{text[:50]}...': {e}")
        return text.format(**kwargs) if kwargs else text


def tr_sync(text: str, lang: str = DEFAULT_LANGUAGE, **kwargs) -> str:
    """Non-awaiting variant: only ever reads the on-disk/in-memory cache
    (populated by the batch script or a previous `tr()` call) — never makes
    a live API call itself. Use this in sync contexts (e.g. building an
    embed's static field list) where you can't await; if the string isn't
    cached yet it silently falls back to English until something else
    (a batch run, or a later `tr()` call for the same text) fills it in."""
    if lang == DEFAULT_LANGUAGE or lang not in SUPPORTED_LANGUAGES:
        return text.format(**kwargs) if kwargs else text
    cache = _load_ai_locale(lang)
    template = cache.get(_template_hash(text), text)
    try:
        return template.format(**kwargs) if kwargs else template
    except (KeyError, IndexError):
        return text.format(**kwargs) if kwargs else text


def language_instruction(lang: str) -> str:
    """
    A short directive to append to LLM prompts so dynamically generated
    content (AI chat, recommendations, etc.) comes back in the user's
    chosen language. No-op (empty string) for English, since prompts are
    already written in English.
    """
    if lang == "en" or lang not in LLM_LANGUAGE_NAME:
        return ""
    return f"\n\nRespond only in {LLM_LANGUAGE_NAME[lang]}, regardless of what language this prompt is written in."
