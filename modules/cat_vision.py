# path: modules/cat_vision.py

"""
AI check for new cat pictures, run ONCE per picture before it joins the shared pool (never per post).

After the sharpness / resolution filter (modules/cat_quality.py) passes, Gemini looks at the picture and answers
KEEP or SKIP. It SKIPs anything that isn't a clear, nice photo of a cat: no cat, a collage, a meme or text overlay,
a watermark, a cartoon, a badly cropped or out-of-focus subject, or a cat that looks hurt.

Fails open: no key, over budget, a Gemini error or an odd answer means KEEP (the quality filter already passed),
so a Gemini outage can never stop cats from appearing.
"""

from __future__ import annotations

import base64
import io
import logging
import os
import time
from collections import deque
from typing import Deque, Optional

import httpx

logger = logging.getLogger(__name__)

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
DEFAULT_MODEL = "gemini-2.5-flash"
MAX_SIDE = 768
REQUEST_TIMEOUT = 12.0

PROMPT = (
    "You pick pictures for a cute cat feature in a Discord bot. Look at this image.\n"
    "Answer KEEP only if it is a clear, sharp, good-looking photo of a real cat (or kitten) that is the main "
    "subject, well framed, with the cat's face or body clearly visible.\n"
    "Answer SKIP if: there is no cat; it is a collage, meme, screenshot, cartoon, drawing or has text, a logo or a "
    "watermark on it; the cat is tiny, cut off, hidden or out of focus; the photo is dark or very low quality; "
    "or the cat looks injured, sick or in distress.\n"
    "If you are not sure, answer KEEP.\n"
    "Reply with exactly one word: KEEP or SKIP."
)

_calls: Deque[float] = deque()
_day = ""
_day_count = 0
_blocked_until = 0.0


def api_key() -> str:
    return (os.getenv("CAT_VISION_API_KEY", "") or os.getenv("GEMINI_API_KEY", "")).strip()


def model() -> str:
    return os.getenv("CAT_VISION_MODEL", "").strip() or DEFAULT_MODEL


def _int_env(name: str, default: int) -> int:
    try:
        return max(0, int(os.getenv(name, "") or default))
    except ValueError:
        return default


def _budget_ok() -> bool:
    global _day, _day_count
    now = time.monotonic()
    if now < _blocked_until:
        return False
    while _calls and now - _calls[0] > 60:
        _calls.popleft()
    if len(_calls) >= _int_env("CAT_VISION_RPM", 8):
        return False
    today = time.strftime("%Y-%m-%d", time.gmtime())
    if _day != today:
        _day, _day_count = today, 0
    if _day_count >= _int_env("CAT_VISION_DAILY", 150):
        return False
    _calls.append(now)
    _day_count += 1
    return True


def parse_verdict(text: str) -> Optional[bool]:
    """'KEEP' -> True, 'SKIP' -> False, anything else -> None (treated as keep)."""
    words = [w.strip(".,:;!*`\"' ") for w in (text or "").upper().split()]
    if not words:
        return None
    if words[0] == "KEEP":
        return True
    if words[0] == "SKIP":
        return False
    return None


def _shrink(data: bytes) -> Optional[bytes]:
    try:
        from PIL import Image
        with Image.open(io.BytesIO(data)) as im:
            im = im.convert("RGB")
            im.thumbnail((MAX_SIDE, MAX_SIDE))
            out = io.BytesIO()
            im.save(out, "JPEG", quality=80)
            return out.getvalue()
    except Exception:
        logger.debug("swallowed exception in _shrink", exc_info=True)
        return None


async def approve(data: bytes) -> bool:
    """True = use this picture. False only when Gemini clearly says SKIP."""
    global _blocked_until
    if not api_key() or not _budget_ok():
        return True
    jpeg = _shrink(data)
    if jpeg is None:
        return True
    body = {
        "contents": [{"role": "user", "parts": [
            {"text": PROMPT},
            {"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(jpeg).decode()}}]}],
        "generationConfig": {"temperature": 0, "maxOutputTokens": 16,
                             **({"thinkingConfig": {"thinkingBudget": 0}} if "2.5" in model() else {})},
    }
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            resp = await client.post(GEMINI_URL.format(model=model()), json=body,
                                     headers={"x-goog-api-key": api_key()})
    except Exception as e:
        logger.warning("[cat-vision] request failed (%s)", type(e).__name__)
        _blocked_until = time.monotonic() + 30
        return True
    if resp.status_code != 200:
        logger.warning("[cat-vision] Gemini answered %s; skipping the AI check for a while", resp.status_code)
        _blocked_until = time.monotonic() + (60 if resp.status_code in (429, 500, 502, 503, 504) else 600)
        return True
    try:
        parts = (resp.json().get("candidates") or [{}])[0].get("content", {}).get("parts", [])
        verdict = parse_verdict("".join(p.get("text", "") for p in parts))
    except Exception:
        logger.debug("swallowed exception in approve", exc_info=True)
        return True
    if verdict is False:
        logger.info("[cat-vision] skipped a picture (not a clear, nice cat photo)")
        return False
    return True
