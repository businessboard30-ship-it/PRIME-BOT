# path: utils/verify_captcha.py

"""
Helpers for the Cloudflare Turnstile join-verification flow.

Flow:
  1. Member clicks "I'm not a bot" in Discord -> bot builds a signed, short-lived
     challenge token (sign_challenge) and sends a link to the Cloudflare Pages
     page: <TURNSTILE_PAGES_URL>/?t=<token>
  2. The page renders the Turnstile widget and POSTs {token, cf_token} to
     /api/verify_captcha on the backend.
  3. The backend checks the HMAC + expiry (parse_challenge), asks Cloudflare to
     validate the widget response (verify_turnstile, secret key stays on the
     backend), and records ONE pass per challenge nonce.
  4. The bot picks the pass up and swaps Unverified -> Verified.

The challenge is signed so nobody can mint a pass for another user/guild, and
the nonce makes every challenge single-use.
"""

import base64
import hashlib
import hmac
import json
import logging
import secrets
import time
from typing import Optional

logger = logging.getLogger(__name__)

CHALLENGE_TTL_SECONDS = 600
SITEVERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sig(secret: str, body: str) -> str:
    return hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()


def sign_challenge(secret: str, guild_id: int, user_id: int, clone_id: Optional[int],
                   now: Optional[float] = None, ttl: int = CHALLENGE_TTL_SECONDS) -> str:
    payload = {
        "g": str(guild_id), "u": str(user_id), "c": clone_id,
        "n": secrets.token_hex(12), "e": int((now if now is not None else time.time()) + ttl),
    }
    body = _b64e(json.dumps(payload, separators=(",", ":")).encode())
    return f"v1.{body}.{_sig(secret, body)}"


def parse_challenge(secret: str, token: str, now: Optional[float] = None) -> Optional[dict]:
    """Returns {guild_id, user_id, clone_id, nonce} or None if forged/expired/garbled."""
    if not secret or not token:
        return None
    try:
        version, body, sig = token.split(".")
        if version != "v1" or not hmac.compare_digest(sig, _sig(secret, body)):
            return None
        data = json.loads(_b64d(body))
        if int(data["e"]) < (now if now is not None else time.time()):
            return None
        clone_id = data.get("c")
        return {
            "guild_id": int(data["g"]), "user_id": int(data["u"]),
            "clone_id": int(clone_id) if clone_id is not None else None,
            "nonce": str(data["n"]),
        }
    except Exception:
        return None


async def verify_turnstile(secret_key: str, cf_token: str, remote_ip: Optional[str] = None) -> bool:
    """Server-side validation of a Turnstile widget response. Fails closed."""
    if not secret_key or not cf_token or len(cf_token) > 2048:
        return False
    import aiohttp
    form = {"secret": secret_key, "response": cf_token}
    if remote_ip:
        form["remoteip"] = remote_ip
    try:
        timeout = aiohttp.ClientTimeout(total=8)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(SITEVERIFY_URL, data=form) as resp:
                data = await resp.json(content_type=None)
        return bool(data.get("success"))
    except Exception:
        logger.warning("turnstile siteverify failed", exc_info=True)
        return False
