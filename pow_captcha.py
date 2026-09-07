# path: pow_captcha.py

"""
Self-hosted proof-of-work "captcha" for the server-listing submit form
(app/servers/submit/page.tsx -> api/server_listings.py POST). No external
vendor, no account, nothing new to host — this is a small HMAC-signed
challenge the backend hands itself, same trust shape as CRON_SECRET.

How it works (deliberately simple, not trying to be bulletproof against a
determined attacker — see api/cron_prune_dead_listings.py's docstring style
of "good enough for the actual threat" reasoning):
  1. GET a challenge: server picks a random salt + an expiry, signs
     (salt, difficulty, expires) with POW_SECRET_KEY, hands all of that to
     the client in the open (the signature is what makes it untamperable,
     not secrecy).
  2. Client brute-forces a nonce such that sha256(f"{salt}:{nonce}") starts
     with `difficulty` hex zeros — cheap for one legit form submission
     (well under a second at difficulty 4), expensive to do thousands of
     times per second the way a scraping bot would need to for the whole
     thing to be worth automating.
  3. Client sends the original challenge fields back alongside the nonce.
     Server re-checks the signature (proves this challenge was actually
     issued by us and hasn't expired) and re-hashes to confirm the nonce
     actually satisfies the difficulty.
"""
import hashlib
import hmac
import secrets
import time
from typing import Optional

from config import POW_SECRET_KEY

DEFAULT_DIFFICULTY = 4  # leading hex zeros required; ~0.1-1s to solve client-side
CHALLENGE_TTL_SECONDS = 5 * 60


def _sign(salt: str, difficulty: int, expires: int) -> str:
    msg = f"{salt}:{difficulty}:{expires}".encode()
    return hmac.new(POW_SECRET_KEY.encode(), msg, hashlib.sha256).hexdigest()


def issue_challenge(difficulty: int = DEFAULT_DIFFICULTY) -> dict:
    salt = secrets.token_hex(16)
    expires = int(time.time()) + CHALLENGE_TTL_SECONDS
    signature = _sign(salt, difficulty, expires)
    return {"salt": salt, "difficulty": difficulty, "expires": expires, "signature": signature}


def verify_solution(salt: str, difficulty: int, expires: int, signature: str, nonce: str) -> Optional[str]:
    """Returns None if valid, or a short reason string if not — callers just
    check `is None`, the reason is for logging/debugging, not shown to end
    users (a spammer doesn't need to know which check failed)."""
    if not POW_SECRET_KEY:
        # Misconfiguration, not a client problem — fail open rather than
        # locking out every real submitter because an env var is missing.
        return None
    if int(time.time()) > expires:
        return "expired"
    expected_sig = _sign(salt, difficulty, expires)
    if not hmac.compare_digest(expected_sig, signature):
        return "bad signature"
    digest = hashlib.sha256(f"{salt}:{nonce}".encode()).hexdigest()
    if not digest.startswith("0" * difficulty):
        return "solution does not satisfy difficulty"
    return None
