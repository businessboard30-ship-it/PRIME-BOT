# path: api/topgg_webhook.py

"""Top.gg vote webhook receiver.

Set the webhook URL in your Top.gg bot dashboard (Webhooks) to
https://<host>/api/topgg_webhook and put the secret in TOPGG_WEBHOOK_SECRET:

  - v1 webhooks: the "whs_..." secret. Requests carry an
    `x-topgg-signature: t=<unix>,v1=<hmac_sha256_hex>` header, where the HMAC
    is over "<t>.<raw body>".
  - v0 legacy webhooks: the shared Authorization string you typed into the
    bot edit form; it arrives verbatim in the `Authorization` header.

A verified vote gives the voter a global XP boost (config.TOPGG_VOTE_MULTIPLIER
for config.TOPGG_VOTE_HOURS). Everything unverified is rejected with 403 and
never touches the database.
"""

import asyncio
import hashlib
import hmac
import json
import logging
import time
from http.server import BaseHTTPRequestHandler
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

# Top.gg's retries back off to ~8s (v1) / ~17 min (v0); a generous window keeps
# retried deliveries working while still stopping long-term replay of a
# captured request.
SIGNATURE_TOLERANCE_SECONDS = 600
MAX_BODY_BYTES = 64 * 1024


def verify_v1_signature(raw_body: bytes, header: str, secret: str,
                        now: Optional[float] = None) -> bool:
    """Checks an `x-topgg-signature` header ("t=<unix>,v1=<hex>")."""
    if not secret or not header:
        return False
    try:
        parts = dict(p.strip().split("=", 1) for p in header.split(","))
        timestamp = parts["t"]
        received = parts["v1"]
        ts_int = int(timestamp)
    except (KeyError, ValueError):
        return False
    if abs((time.time() if now is None else now) - ts_int) > SIGNATURE_TOLERANCE_SECONDS:
        return False
    expected = hmac.new(secret.encode(), timestamp.encode() + b"." + raw_body,
                        hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected.encode(), received.encode())


def authenticate(headers, raw_body: bytes, secret: str, now: Optional[float] = None) -> bool:
    """v1 signature when that header is present, otherwise the v0 Authorization
    header. If a signature header is present the v0 path is NOT tried, so a
    bad signature can't be rescued by a guessable Authorization value."""
    if not secret:
        return False
    signature = headers.get("x-topgg-signature")
    if signature:
        return verify_v1_signature(raw_body, signature, secret, now=now)
    auth = headers.get("Authorization")
    if auth:
        return hmac.compare_digest(auth.encode(), secret.encode())
    return False


def extract_vote(payload: dict) -> Tuple[str, Optional[int], Optional[str]]:
    """Returns (kind, discord_user_id, vote_id) where kind is "vote", "test" or
    "ignore". Understands the v1 payload ({"type": "vote.create", "data": ...})
    and the v0 one ({"type": "upvote", "user": "<discord id>"})."""
    if not isinstance(payload, dict):
        return "ignore", None, None
    ptype = payload.get("type")

    if ptype == "vote.create":
        data = payload.get("data") or {}
        user = data.get("user") or {}
        project = data.get("project") or {}
        # Only Discord bot votes carry a Discord ID we can boost.
        if project.get("platform") not in (None, "discord"):
            return "ignore", None, None
        uid = user.get("platform_id")
        vote_id = data.get("id")
        try:
            return "vote", int(uid), (str(vote_id) if vote_id is not None else None)
        except (TypeError, ValueError):
            return "ignore", None, None

    if ptype == "upvote":
        try:
            return "vote", int(payload.get("user")), None
        except (TypeError, ValueError):
            return "ignore", None, None

    if ptype in ("webhook.test", "test"):
        return "test", None, None

    return "ignore", None, None


class handler(BaseHTTPRequestHandler):
    def _reply(self, code: int, msg: str):
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(msg.encode())

    def do_POST(self):
        import config
        from database import db

        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY_BYTES:
            self._reply(400, "bad length")
            return
        raw_body = self.rfile.read(length)

        if not config.TOPGG_WEBHOOK_SECRET:
            logger.error("[topgg] TOPGG_WEBHOOK_SECRET is not set; rejecting vote webhook")
            self._reply(403, "forbidden")
            return
        if not authenticate(self.headers, raw_body, config.TOPGG_WEBHOOK_SECRET):
            logger.warning("[topgg] webhook rejected: bad signature/authorization")
            self._reply(403, "forbidden")
            return

        try:
            payload = json.loads(raw_body.decode("utf-8", errors="replace"))
        except ValueError:
            self._reply(400, "bad json")
            return

        kind, user_id, vote_id = extract_vote(payload)
        if kind == "test":
            logger.info("[topgg] test webhook received OK")
            self._reply(200, "ok")
            return
        if kind != "vote" or user_id is None:
            self._reply(200, "ignored")
            return

        try:
            row = asyncio.run(db.record_topgg_vote(user_id, config.TOPGG_VOTE_HOURS, vote_id))
        except Exception:
            logger.exception("[topgg] failed to record vote for %s", user_id)
            self._reply(500, "error")  # 5xx -> Top.gg retries
            return
        logger.info("[topgg] vote %s for user %s", "recorded" if row else "duplicate", user_id)
        self._reply(200, "ok" if row else "duplicate")

    def log_message(self, format, *args):
        pass
