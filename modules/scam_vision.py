# path: modules/scam_vision.py

"""
Scam Shield, second stage: ask Google Gemini whether an image is a scam.

Why: the hash rules in modules/scam_shield.py only catch images that look like a screenshot we already
saw. The casino-giveaway scammers regenerate their pictures (new fake popup, new fake phone, new celebrity),
so every new copy walks past the hashes. A vision model reads the picture, so it still catches them.

Cost / safety rules (the free Gemini tier has tight limits):
  * Only runs when hashes and text found nothing, and only for non-staff users (the cog decides).
  * Images are shrunk to <= 1024px JPEG before sending (fast + cheap).
  * Results are cached by file hash; near-copies of an image Gemini already flagged are caught from memory
    (perceptual hash) without another API call.
  * A per-minute and a per-day budget plus a short per-user limit. Over budget = skipped, never queued.
  * Fails open: any error, timeout, missing key or odd answer means "not a scam". It never deletes on doubt.
"""

from __future__ import annotations

import base64
import hashlib
import io
import logging
import os
import time
from collections import OrderedDict, deque
from typing import Deque, Dict, List, Optional, Tuple

import httpx

from modules import scam_shield as ss

logger = logging.getLogger(__name__)

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
DEFAULT_MODEL = "gemini-2.5-flash"      # gemini-1.5-flash has been shut down by Google
MAX_SIDE = 1024                         # px, longest side sent to Gemini
MIN_BYTES = 15_000                      # tiny images (emoji, icons, stickers) are never scam screenshots
MIN_SIDE = 300                          # px, same idea
MAX_DOWNLOAD_BYTES = 8_000_000
REQUEST_TIMEOUT = 12.0
LEARNED_DISTANCE = 6                    # Hamming distance for "same picture as one Gemini already flagged"
FREE_GUILD_DAILY = 10                   # AI checks per server per day (UTC) on a free server
PREMIUM_GUILD_DAILY = 60                # same, for a Premium server
CACHE_MAX = 3000
LEARNED_MAX = 300

PROMPT = (
    "You are a scam-detection filter for a Discord server. Look at this uploaded image.\n"
    "Answer SCAM only if the image is clearly one of these:\n"
    "- fake crypto-casino / betting / gambling site screenshot used as bait (fake 'withdrawal accepted', "
    "'withdrawal success', payout, balance or winnings popups, USDT/BTC/ETH transfer receipts shown as proof, "
    "promo-code or bonus pages, wheel-of-fortune/free-spins promos)\n"
    "- fake giveaway or 'free money' post, often with a celebrity or streamer name or a fake X/Twitter/Instagram post, "
    "'thank you' proof photos, or a promo code to enter\n"
    "- fake Discord Nitro, Steam, Roblox, Robux, gift-card or in-game-item giveaway\n"
    "- crypto airdrop, 'send me X and get 2X back', fake investment/trading profit screenshot or fake wallet/exchange screen\n"
    "- phishing: fake login, fake verification / captcha page, fake support or security warning, QR-code bait\n"
    "Answer SAFE for everything else: normal photos, memes, game screenshots, art, selfies, chat screenshots, "
    "real-life receipts, and ordinary discussion of crypto or games.\n"
    "If you are not sure, answer SAFE.\n"
    "Reply with exactly one word: SAFE or SCAM."
)


class _State:
    cache: "OrderedDict[str, bool]" = OrderedDict()           # sha256 of original bytes -> is_scam
    learned: List[int] = []                                     # dHash of images Gemini called SCAM
    calls: Deque[float] = deque()                               # monotonic times of recent API calls
    day_key: str = ""
    day_count: int = 0
    blocked_until: float = 0.0                                  # circuit breaker after 429 / 5xx / network errors
    user_calls: Dict[Tuple[int, int], Deque[float]] = {}
    guild_used: Dict[Tuple[int, Optional[int]], Tuple[str, int]] = {}   # (guild, clone) -> (UTC day, checks used)
    premium: Dict[Tuple[int, Optional[int]], Tuple[float, bool]] = {}   # 60s cache of "is this server Premium"
    last_ok: Optional[bool] = None                              # result of the most recent Gemini call
    last_detail: str = ""
    last_at: float = 0.0                                        # wall-clock time (time.time())


_s = _State()


async def _is_premium(guild_id: int, clone_id: Optional[int]) -> bool:
    """Premium lookup, cached for 60s. Any problem means free (the smaller cap), never an error."""
    key = (guild_id, clone_id)
    hit = _s.premium.get(key)
    now = time.monotonic()
    if hit and hit[0] > now:
        return hit[1]
    try:
        from database import db
        value = bool(await db.is_guild_premium_active(guild_id, clone_id))
    except Exception:
        return False
    if len(_s.premium) > 3000:
        _s.premium.clear()
    _s.premium[key] = (now + 60, value)
    return value


async def guild_cap(guild_id: int, clone_id: Optional[int] = None) -> Tuple[int, bool]:
    """(daily AI-check cap for this server, is it Premium). Env vars SCAM_VISION_FREE_DAILY and
    SCAM_VISION_PREMIUM_DAILY change the numbers without a code change."""
    premium = await _is_premium(guild_id, clone_id)
    cap = _int_env("SCAM_VISION_PREMIUM_DAILY", PREMIUM_GUILD_DAILY) if premium \
        else _int_env("SCAM_VISION_FREE_DAILY", FREE_GUILD_DAILY)
    return cap, premium


def guild_used_today(guild_id: int, clone_id: Optional[int] = None) -> int:
    day, n = _s.guild_used.get((guild_id, clone_id), ("", 0))
    return n if day == _today() else 0


def _spend_guild(guild_id: int, clone_id: Optional[int]) -> None:
    if len(_s.guild_used) > 5000:
        _s.guild_used.clear()
    _s.guild_used[(guild_id, clone_id)] = (_today(), guild_used_today(guild_id, clone_id) + 1)


def _note(ok: bool, detail: str) -> None:
    _s.last_ok, _s.last_detail, _s.last_at = ok, detail[:200], time.time()


def status_text() -> str:
    """One line for the owner panel: is the AI scan actually working right now?"""
    if not api_key():
        return "no API key set"
    if _s.last_ok is None:
        return "no scan yet since the last restart (press Test AI scan)"
    ago = int(time.time() - _s.last_at)
    when = f"{ago // 3600}h ago" if ago >= 3600 else f"{ago // 60}m ago" if ago >= 60 else "just now"
    return f"✅ working (last call {when})" if _s.last_ok else f"❌ ERROR {when}: {_s.last_detail}"


def _int_env(name: str, default: int) -> int:
    try:
        return max(0, int(os.getenv(name, "") or default))
    except ValueError:
        return default


def api_key() -> str:
    """Scam Shield's own key (SCAM_VISION_API_KEY) so its quota is separate from the AI features;
    falls back to GEMINI_API_KEY when no separate key is set."""
    return (os.getenv("SCAM_VISION_API_KEY", "") or os.getenv("GEMINI_API_KEY", "")).strip()


def model() -> str:
    return os.getenv("SCAM_VISION_MODEL", "").strip() or DEFAULT_MODEL


def available() -> bool:
    """True when a key is configured and the owner hasn't switched the AI scan off."""
    return bool(api_key()) and ss.vision_enabled()


def worth_checking(size: int, width: Optional[int] = None, height: Optional[int] = None) -> bool:
    if size < MIN_BYTES or size > MAX_DOWNLOAD_BYTES:
        return False
    if width and height and min(width, height) < MIN_SIDE:
        return False
    return True


# ── budgets ───────────────────────────────────────────────────────────────

def _today() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


def _budget_ok(guild_id: int, user_id: int) -> bool:
    now = time.monotonic()
    if now < _s.blocked_until:
        return False
    rpm = _int_env("SCAM_VISION_RPM", 12)
    daily = _int_env("SCAM_VISION_DAILY", 250)
    while _s.calls and now - _s.calls[0] > 60:
        _s.calls.popleft()
    if len(_s.calls) >= rpm:
        return False
    day = _today()
    if _s.day_key != day:
        _s.day_key, _s.day_count = day, 0
    if _s.day_count >= daily:
        return False
    key = (guild_id, user_id)
    if len(_s.user_calls) > 2000:
        _s.user_calls.clear()
    q = _s.user_calls.setdefault(key, deque())
    while q and now - q[0] > 600:
        q.popleft()
    if len(q) >= _int_env("SCAM_VISION_PER_USER", 4):      # per user per 10 minutes
        return False
    q.append(now)
    return True


def _spend() -> None:
    _s.calls.append(time.monotonic())
    _s.day_count += 1


def _remember(key: str, is_scam: bool) -> None:
    _s.cache[key] = is_scam
    _s.cache.move_to_end(key)
    while len(_s.cache) > CACHE_MAX:
        _s.cache.popitem(last=False)


def _learn(data: bytes) -> None:
    h = ss.dhash(data)
    if h is not None and all(ss.hamming(h, x) > 1 for x in _s.learned):
        _s.learned.append(h)
        del _s.learned[:-LEARNED_MAX]


def known_copy(data: bytes) -> bool:
    """Same picture (or a re-compressed / resized copy) as one Gemini already flagged this session."""
    if not _s.learned:
        return False
    h = ss.dhash(data)
    return h is not None and any(ss.hamming(h, x) <= LEARNED_DISTANCE for x in _s.learned)


# ── image prep ────────────────────────────────────────────────────────────

def prepare(data: bytes) -> Optional[bytes]:
    """Shrink to <= MAX_SIDE and re-encode as JPEG. None if it isn't a readable image."""
    try:
        from PIL import Image
        with Image.open(io.BytesIO(data)) as im:
            im.seek(0)
            im = im.convert("RGB")
            im.thumbnail((MAX_SIDE, MAX_SIDE))
            out = io.BytesIO()
            im.save(out, "JPEG", quality=80)
            return out.getvalue()
    except Exception:
        return None


# ── Gemini call ───────────────────────────────────────────────────────────

def parse_verdict(text: str) -> Optional[bool]:
    """'SCAM' -> True, 'SAFE' -> False, anything else -> None (treated as not a scam)."""
    t = (text or "").strip().upper()
    words = [w.strip(".,:;!*`\"' ") for w in t.split()]
    if not words:
        return None
    if words[0] == "SCAM":
        return True
    if words[0] == "SAFE":
        return False
    if "SCAM" in words and "SAFE" not in words and len(words) <= 4:
        return True
    return None


async def _ask(jpeg: bytes) -> Optional[bool]:
    m = model()
    cfg: dict = {"temperature": 0, "maxOutputTokens": 16}
    if "2.5" in m:
        cfg["thinkingConfig"] = {"thinkingBudget": 0}          # one-word answer: no reasoning tokens
    body = {
        "contents": [{"role": "user", "parts": [
            {"text": PROMPT},
            {"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(jpeg).decode()}},
        ]}],
        "generationConfig": cfg,
        "safetySettings": [
            {"category": c, "threshold": "BLOCK_NONE"} for c in (
                "HARM_CATEGORY_HARASSMENT", "HARM_CATEGORY_HATE_SPEECH",
                "HARM_CATEGORY_SEXUALLY_EXPLICIT", "HARM_CATEGORY_DANGEROUS_CONTENT")
        ],
    }
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            # key goes in a header, not the URL, so it can't leak into logs
            resp = await client.post(GEMINI_URL.format(model=m), json=body,
                                     headers={"x-goog-api-key": api_key()})
    except Exception as e:
        logger.warning("[scam-vision] request failed (%s)", type(e).__name__)
        _s.blocked_until = time.monotonic() + 30
        _note(False, f"can't reach Gemini ({type(e).__name__})")
        return None
    if resp.status_code == 429 or resp.status_code >= 500:
        logger.warning("[scam-vision] Gemini answered %s; pausing AI scan for 60s", resp.status_code)
        _s.blocked_until = time.monotonic() + 60
        _note(False, "rate limit hit (429), free quota used up" if resp.status_code == 429
              else f"Gemini server error {resp.status_code}")
        return None
    if resp.status_code in (400, 401, 403, 404):
        logger.error("[scam-vision] Gemini rejected the request (%s): check GEMINI_API_KEY / SCAM_VISION_MODEL. %s",
                     resp.status_code, resp.text[:200])
        _s.blocked_until = time.monotonic() + 600
        why = {400: "bad request / key not valid", 401: "key rejected", 403: "key not allowed (check Google AI Studio)",
               404: f"model '{m}' not found (check SCAM_VISION_MODEL)"}[resp.status_code]
        _note(False, f"{why} ({resp.status_code})")
        return None
    try:
        data = resp.json()
        parts = (data.get("candidates") or [{}])[0].get("content", {}).get("parts", [])
        verdict = parse_verdict("".join(p.get("text", "") for p in parts))
        _note(True, "ok")
        return verdict
    except Exception:
        logger.debug("[scam-vision] couldn't read Gemini's answer", exc_info=True)
        _note(False, "Gemini answered but the reply couldn't be read")
        return None


async def is_scam_image(data: bytes, guild_id: int, user_id: int,
                        clone_id: Optional[int] = None) -> Optional[Tuple[str, str, Optional[int]]]:
    """Returns ('vision', reason, None) when the image is a scam, else None. Same shape as ss.match_image.
    Cache hits and copies of already-flagged images cost nothing and ignore the server cap; only a real
    Gemini call counts against the server's daily cap (free: 10, Premium: 60 by default)."""
    if not available():
        return None
    key = hashlib.sha256(data).hexdigest()
    if key in _s.cache:
        return ("vision", "AI scan: scam image (seen before)", None) if _s.cache[key] else None
    if known_copy(data):
        _remember(key, True)
        return "vision", "AI scan: copy of a scam image already flagged", None
    cap, _ = await guild_cap(guild_id, clone_id)
    if guild_used_today(guild_id, clone_id) >= cap:
        return None                                              # this server used up today's AI checks
    if not _budget_ok(guild_id, user_id):
        return None
    jpeg = prepare(data)
    if jpeg is None:
        return None
    _spend()
    _spend_guild(guild_id, clone_id)
    verdict = await _ask(jpeg)
    if verdict is None:
        return None                                              # unknown: not cached, never deletes
    _remember(key, verdict)
    if verdict:
        _learn(data)
        return "vision", "AI scan: scam image", None
    return None


async def self_test() -> Tuple[bool, str]:
    """Owner-panel health check: sends a small test picture to Gemini right now (ignores the budgets).
    Returns (working, message). Clears the error pause when it succeeds."""
    if not api_key():
        return False, "No API key set (SCAM_VISION_API_KEY or GEMINI_API_KEY)."
    try:
        from PIL import Image, ImageDraw
        im = Image.new("RGB", (600, 320), (18, 24, 38))
        d = ImageDraw.Draw(im)
        d.text((40, 60), "WITHDRAWAL ACCEPTED", fill=(40, 220, 120))
        d.text((40, 120), "Your withdrawal of $3,200.00 has been accepted", fill=(255, 255, 255))
        d.text((40, 180), "Promo code: DRAKE   FREE SPINS   USDT", fill=(255, 220, 60))
        out = io.BytesIO()
        im.save(out, "JPEG", quality=85)
    except Exception:
        return False, "Couldn't build the test picture (Pillow problem)."
    before = _s.last_at
    verdict = await _ask(out.getvalue())
    if _s.last_at == before or not _s.last_ok:
        return False, _s.last_detail or "No answer from Gemini."
    _s.blocked_until = 0.0
    if verdict is None:
        return True, "Gemini answered but not with SAFE/SCAM (it will be treated as not a scam)."
    return True, f"Gemini answered **{'SCAM' if verdict else 'SAFE'}** for the test picture."
