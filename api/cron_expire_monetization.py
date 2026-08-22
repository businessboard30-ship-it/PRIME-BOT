"""
Cron-triggered endpoint that sweeps for lapsed clone monetization
subscriptions (the CLONE_MONETIZATION_FEE_GHS/month fee that unlocks a
clone owner connecting their own Paystack/Stripe key and setting their own
prices — see handlers/clone_bot.py and config.PRICE_REGISTRY).

Any clone whose subscription expired gets auto-reverted: payment provider
back to 'main' and custom prices wiped back to registry defaults. This is
the safe default — payments keep flowing through the main bot's account
(still netting the owner their commission split via commission_tracking)
instead of leaving a stale key or price in place indefinitely.

Auth: same pattern as api/cron_autopost.py / api/cron_broadcast.py — either
header "Authorization: Bearer <CRON_SECRET>" (what Vercel Cron sends
automatically when CRON_SECRET is set) or query param ?secret=<CRON_SECRET>.
Run this once a day; expiry granularity doesn't need finer than that.
"""
import json
import asyncio
import logging
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

from config import CRON_SECRET
from database import db

logger = logging.getLogger(__name__)


class handler(BaseHTTPRequestHandler):

    def _authorized(self) -> bool:
        if not CRON_SECRET:
            return False
        auth_header = self.headers.get("Authorization", "")
        if auth_header == f"Bearer {CRON_SECRET}":
            return True
        query = parse_qs(urlparse(self.path).query)
        return query.get("secret", [""])[0] == CRON_SECRET

    def do_GET(self):
        self._handle()

    def do_POST(self):
        self._handle()

    def _handle(self):
        if not self._authorized():
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "error", "message": "Unauthorized"}).encode())
            return

        try:
            reverted = asyncio.run(db.expire_monetization_subscriptions())
            logger.info(f"[v0] cron_expire_monetization reverted clone_ids={reverted}")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "ok", "reverted": reverted}).encode())
        except Exception as e:
            logger.error(f"[v0] cron_expire_monetization error: {e}")
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "error", "message": str(e)}).encode())

    def log_message(self, format, *args):
        logger.debug(f"[v0] cron_expire_monetization: {format % args}")


if __name__ == '__main__':
    print(asyncio.run(db.expire_monetization_subscriptions()))
