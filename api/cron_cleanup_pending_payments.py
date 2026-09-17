"""
Cron-triggered endpoint that sweeps payment_logs for 'pending' checkouts
older than 72 hours and marks them 'expired'. A checkout that's been
sitting in 'pending' for days was abandoned (user opened the payment link
and never finished, or bounced off Paystack/Selar) — it should stop
inflating /admin revenue's "Pending checkouts" count forever instead of
sitting there indefinitely.

Auth: same pattern as every other api/cron_*.py in this repo — either
header "Authorization: Bearer <CRON_SECRET>" (what Vercel Cron sends
automatically when CRON_SECRET is set) or query param ?secret=<CRON_SECRET>.
Run this every few hours; 72h granularity doesn't need finer than that.

NOTE: as of writing, vercel.json has no "crons" entries at all, so this
(and every other api/cron_*.py file) only runs when something external
actually calls it. Register it in vercel.json (see that file) or point an
external scheduler (Vercel Cron / cron-job.org / a GitHub Actions
schedule) at this endpoint's deployed URL.
"""
import json
import asyncio
import logging
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

from config import CRON_SECRET
from database import db

logger = logging.getLogger(__name__)

PENDING_EXPIRY_HOURS = 72


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
            expired = asyncio.run(db.expire_old_pending_payments(PENDING_EXPIRY_HOURS))
            logger.info(f"[v0] cron_cleanup_pending_payments expired={expired} rows")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "ok", "expired": expired}).encode())
        except Exception as e:
            logger.error(f"[v0] cron_cleanup_pending_payments error: {e}")
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "error", "message": str(e)}).encode())

    def log_message(self, format, *args):
        logger.debug(f"[v0] cron_cleanup_pending_payments: {format % args}")


if __name__ == '__main__':
    print(asyncio.run(db.expire_old_pending_payments(PENDING_EXPIRY_HOURS)))
