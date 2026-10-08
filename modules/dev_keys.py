# path: modules/dev_keys.py
"""Developer mode: bring your own AI key (Anthropic, Groq, OpenAI). Pure rules plus server-to-provider calls.

Rules this module enforces:
- A key is an API key the person pasted. It is validated with ONE free call (list models), encrypted by the caller
  with secret_manager, and never returned, logged or put in an error message.
- Calls go from this server to the provider only. Nothing here stores message text.
- Every error that leaves this module is a fixed, safe sentence (never the provider's body, never the key).
"""
import re

import aiohttp

# id -> display + endpoints. `chat_model` is the model used for the person's own key.
PROVIDERS = {
    "anthropic": {"label": "Claude (Anthropic)", "models_url": "https://api.anthropic.com/v1/models",
                  "chat_url": "https://api.anthropic.com/v1/messages", "chat_model": "claude-sonnet-5-5"},
    "groq": {"label": "Groq", "models_url": "https://api.groq.com/openai/v1/models",
             "chat_url": "https://api.groq.com/openai/v1/chat/completions", "chat_model": "llama-3.3-70b-versatile"},
    "openai": {"label": "OpenAI", "models_url": "https://api.openai.com/v1/models",
               "chat_url": "https://api.openai.com/v1/chat/completions", "chat_model": "gpt-4o-mini"},
}
MAX_CONNECTIONS = len(PROVIDERS)           # one key per provider, so this is the per-user cap
KEY_MIN, KEY_MAX = 20, 300
_KEY_RE = re.compile(r"^[A-Za-z0-9_\-\.]+$")
_ANTHROPIC_VERSION = "2023-06-01"
GRACE_DAYS = 30                            # stored keys are deleted this long after the Developer plan ends
CHAT_MAX_TOKENS = 1200
HTTP_TIMEOUT = 45


def clean_provider(raw):
    p = str(raw or "").strip().lower()
    return p if p in PROVIDERS else None


def clean_key(raw):
    """Returns (key, error). Format check only; the provider decides whether it really works."""
    if not isinstance(raw, str):
        return None, "Paste your API key."
    key = raw.strip()
    if not (KEY_MIN <= len(key) <= KEY_MAX) or not _KEY_RE.match(key):
        return None, "That doesn't look like an API key."
    return key, None


def last4(key: str) -> str:
    return key[-4:]


def public_view(row: dict) -> dict:
    """The ONLY shape of a connection that may leave the server: provider, label, last 4, dates."""
    p = row.get("provider")
    return {"provider": p, "label": (PROVIDERS.get(p) or {}).get("label", p), "last4": row.get("last4") or "",
            "added_at": _iso(row.get("created_at")), "updated_at": _iso(row.get("updated_at"))}


def _iso(v):
    return v.isoformat() if hasattr(v, "isoformat") else (str(v) if v else None)


def _headers(provider: str, key: str) -> dict:
    if provider == "anthropic":
        return {"x-api-key": key, "anthropic-version": _ANTHROPIC_VERSION, "content-type": "application/json"}
    return {"Authorization": f"Bearer {key}", "content-type": "application/json"}


async def validate(provider: str, key: str):
    """One free call (list models). Returns (ok, error). Never raises, never echoes the key or the provider body."""
    cfg = PROVIDERS[provider]
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
            async with s.get(cfg["models_url"], headers=_headers(provider, key)) as r:
                status = r.status
    except Exception:
        return False, f"Couldn't reach {cfg['label']} to check the key. Try again in a moment."
    if status == 200:
        return True, None
    if status in (401, 403):
        return False, f"{cfg['label']} didn't accept that key."
    return False, f"{cfg['label']} couldn't verify the key right now. Try again in a moment."


def build_payload(provider: str, system: str, messages: list) -> dict:
    cfg = PROVIDERS[provider]
    if provider == "anthropic":
        return {"model": cfg["chat_model"], "max_tokens": CHAT_MAX_TOKENS, "system": system, "messages": messages}
    return {"model": cfg["chat_model"], "max_tokens": CHAT_MAX_TOKENS, "temperature": 0.7,
            "messages": [{"role": "system", "content": system}] + messages}


def extract_text(provider: str, data) -> str:
    try:
        if provider == "anthropic":
            return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text").strip()
        return (((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
    except Exception:
        return ""


async def chat(provider: str, key: str, system: str, messages: list) -> str:
    """One call to the person's own provider. Raises RuntimeError with a SAFE message on any failure."""
    cfg = PROVIDERS[provider]
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT)) as s:
            async with s.post(cfg["chat_url"], headers=_headers(provider, key),
                              json=build_payload(provider, system, messages)) as r:
                status = r.status
                data = await r.json(content_type=None) if status == 200 else None
    except Exception:
        raise RuntimeError(f"Couldn't reach {cfg['label']}. Try again in a moment.") from None
    if status in (401, 403):
        raise RuntimeError(f"{cfg['label']} rejected your key. Replace it in Keys.")
    if status == 429:
        raise RuntimeError(f"{cfg['label']} says you're over your own limit. Try again later.")
    if status != 200 or not isinstance(data, dict):
        raise RuntimeError(f"{cfg['label']} couldn't answer right now. Try again in a moment.")
    text = extract_text(provider, data)
    if not text:
        raise RuntimeError("The AI didn't return an answer. Try rephrasing.")
    return text[:6000]
