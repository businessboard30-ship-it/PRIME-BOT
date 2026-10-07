# path: api/verify_captcha.py

"""
Backend half of the Cloudflare Turnstile join verification.

POST /api/verify_captcha   {"token": "<signed challenge>", "cf_token": "<turnstile response>"}

Called by the static page in captcha-pages/ (Cloudflare Pages). Validates the
signed challenge, validates the Turnstile response with Cloudflare using
TURNSTILE_SECRET_KEY (never shipped to the browser), and records a single-use
pass. The bot applies the role swap when it sees the pass.
"""
import asyncio
import json
import logging
from http.server import BaseHTTPRequestHandler

import config
from database import db
from utils.verify_captcha import parse_challenge, verify_turnstile

logger = logging.getLogger(__name__)

MAX_BODY_BYTES = 8 * 1024
_initialized = False


class handler(BaseHTTPRequestHandler):

    def _cors(self):
        # Only the Pages site may call this from a browser. Falls back to "*" only
        # when no Pages URL is configured yet; the signed challenge + Turnstile
        # check are what actually protect the endpoint.
        origin = (config.TURNSTILE_PAGES_URL or "").rstrip("/")
        self.send_header("Access-Control-Allow-Origin", origin or "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Vary", "Origin")

    def _json(self, status: int, payload: dict):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self._cors()
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode())

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_POST(self):
        if not config.TURNSTILE_SECRET_KEY or not config.VERIFY_SIGNING_SECRET:
            self._json(503, {"ok": False, "message": "Captcha verification isn't configured yet."})
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            if length > MAX_BODY_BYTES:
                raise ValueError("too large")
            body = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            self._json(400, {"ok": False, "message": "Invalid request."})
            return

        challenge = parse_challenge(config.VERIFY_SIGNING_SECRET, str(body.get("token") or ""))
        if challenge is None:
            self._json(400, {"ok": False, "message": "This link is invalid or expired. Click the verify button in Discord again."})
            return

        fwd = (self.headers.get("CF-Connecting-IP") or self.headers.get("X-Forwarded-For") or "").split(",")[0].strip()

        async def _run():
            global _initialized
            if not _initialized:
                from init_system import initialize_system
                await initialize_system()
                _initialized = True
            ok = await verify_turnstile(config.TURNSTILE_SECRET_KEY, str(body.get("cf_token") or ""), fwd or None)
            if not ok:
                return 403, {"ok": False, "message": "Captcha check failed. Please try again."}
            fresh = await db.create_verification_pass(
                challenge["nonce"], challenge["guild_id"], challenge["clone_id"], challenge["user_id"]
            )
            if not fresh:
                return 409, {"ok": False, "message": "This link was already used."}
            return 200, {"ok": True, "message": "Verified! You can head back to Discord."}

        try:
            status, payload = asyncio.run(_run())
        except Exception:
            logger.exception("verify_captcha failed")
            status, payload = 500, {"ok": False, "message": "Something went wrong. Please try again."}
        self._json(status, payload)
