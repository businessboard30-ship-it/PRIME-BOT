# path: modules/ai_redact.py

"""Phase 7: never let a secret travel through the AI. `redact()` replaces credentials with [redacted] in (a) what a person types
to the AI (so it is not sent to the model provider or saved in chat history), (b) what the AI replies, and (c) agent task text.

Two layers: well-known credential SHAPES (Discord bot tokens, GitHub/Groq/OpenAI-style keys, AWS ids, URLs with passwords,
Bearer tokens, Fernet keys), and the EXACT values of this bot's own secrets read from the environment. Pure text in, text out;
never raises (a failure returns the text unchanged rather than breaking chat)."""

import os
import re

MASK = "[redacted]"
_PATTERNS = [re.compile(p) for p in (
    r"\b[A-Za-z0-9_-]{23,28}\.[A-Za-z0-9_-]{6,7}\.[A-Za-z0-9_-]{27,}\b",          # Discord bot token
    r"\bmfa\.[A-Za-z0-9_-]{60,}\b",                                                # old Discord user token
    r"\bgh[pousr]_[A-Za-z0-9]{30,}\b", r"\bgithub_pat_[A-Za-z0-9_]{40,}\b",         # GitHub
    r"\bgsk_[A-Za-z0-9]{20,}\b",                                                   # Groq
    r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{20,}\b", r"\b[sr]k_(?:live|test)_[A-Za-z0-9]{16,}\b", r"\bwhsec_[A-Za-z0-9]{16,}\b",
    r"\bAKIA[0-9A-Z]{16}\b",                                                       # AWS access key id
    r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b",                                           # Slack
    r"\b[A-Za-z0-9_-]{43}=(?![A-Za-z0-9_=-])",                                     # Fernet / base64 32-byte key
)]
_URL_CREDS = re.compile(r"\b([a-zA-Z][a-zA-Z0-9+.-]*://)[^\s:@/]+:[^\s@/]+@")
_BEARER = re.compile(r"(?i)\b(bearer|authorization:)\s+[A-Za-z0-9._~+/=-]{16,}")
_SECRET_ENV = re.compile(r"(?i)(KEY|TOKEN|SECRET|PASSWORD|PASSWD|DATABASE_URL|DSN)")
_MIN_SECRET_LEN = 12


def _known_secrets():
    out = []
    for name, val in os.environ.items():
        if val and len(val) >= _MIN_SECRET_LEN and _SECRET_ENV.search(name):
            out.append(val)
    return sorted(set(out), key=len, reverse=True)


def redact(text):
    if not isinstance(text, str) or len(text) < 8:
        return text
    try:
        out = text
        for secret in _known_secrets():
            if secret in out:
                out = out.replace(secret, MASK)
        out = _URL_CREDS.sub(lambda m: m.group(1) + MASK + "@", out)
        out = _BEARER.sub(lambda m: f"{m.group(1)} {MASK}", out)
        for p in _PATTERNS:
            out = p.sub(MASK, out)
        return out
    except Exception:
        return text


def contains_secret(text) -> bool:
    return isinstance(text, str) and redact(text) != text
