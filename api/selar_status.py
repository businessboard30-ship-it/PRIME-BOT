# path: api/selar_status.py

"""
Polled by app/unlock/unlock-status.tsx every few seconds so the buyer sees
"Approved"/"Rejected" show up live once an admin acts, instead of needing
to refresh. Deliberately read-only and unauthenticated beyond the
reference itself — status alone ('pending' / 'awaiting_review' /
'completed' / 'rejected') isn't sensitive, and this endpoint never accepts
a signature to bypass (unlike selar_submit.py, nothing here can move a
payment forward), so there is no tamper surface to guard against beyond
basic format validation.

GET params: reference
"""
import json
import logging
import asyncio
import re
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

from database import db

logger = logging.getLogger(__name__)

_REFERENCE_RE = re.compile(r"^[A-Za-z0-9._:-]{6,160}$")

# payment_logs.status values -> what the buyer should see. 'completed' is
# what UNLOCK_HANDLERS/db.mark_payment_paid leave behind on approval;
# 'rejected' is db.mark_manual_payment_rejected's terminal state; anything
# else (pending/awaiting_review) is still in flight.
_PUBLIC_STATUS = {
    "completed": "verified",
    "rejected": "rejected",
}


class handler(BaseHTTPRequestHandler):

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

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

    def do_GET(self):
        query = parse_qs(urlparse(self.path).query)
        reference = (query.get("reference") or [""])[0].strip()

        if not _REFERENCE_RE.match(reference):
            self._json(400, {"status": "error", "message": "Invalid reference"})
            return

        try:
            row = asyncio.run(db.get_payment_by_reference(reference))
        except Exception as e:
            logger.error(f"[selar-status] DB error looking up reference {reference}: {e}")
            self._json(500, {"status": "error", "message": "Internal error"})
            return

        if not row:
            self._json(200, {"status": "pending", "submitted": False})
            return

        raw_status = row.get("status", "pending")
        public_status = _PUBLIC_STATUS.get(raw_status, "pending")
        self._json(200, {
            "status": public_status,
            "submitted": raw_status in ("awaiting_review", "completed", "rejected"),
        })

    def log_message(self, format, *args):
        logger.debug(f"[selar-status] {format % args}")
