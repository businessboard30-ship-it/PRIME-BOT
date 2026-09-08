# path: api/apply_boost.py

"""
Applies purchased boosts to a listing's boost_count (see database.py's
add_listing_boosts and the "trending" sort in _LISTING_SORTS — each boost
counts the same weight as one vote).

*** PAYMENT IS NOT WIRED IN FRONT OF THIS ENDPOINT YET. ***
Right now this trusts whatever `amount` the client sends and applies it
immediately — it exists so the boost-purchase UI (app/servers/_BoostModal.tsx)
has real, working "apply" logic to demo against, per an explicit
do-not-wire-payment-yet request. Before this goes live:
  1. Create a pending charge (Paystack/Stripe — see payments.py /
     api/paystack_webhook.py for the pattern already used elsewhere in
     this repo) for `amount * PRICE_PER_BOOST` instead of crediting boosts
     directly.
  2. Only call db.add_listing_boosts from the payment webhook's success
     handler, keyed off that charge, never from a client-triggered POST.
  3. Delete/lock down this endpoint's direct-apply path.

POST body: {"guild_id": "...", "clone_id": <int|null>, "amount": <int>}
amount must be a positive integer, capped at MAX_BOOSTS_PER_REQUEST so a
malformed or malicious request can't push a listing's boost_count to an
absurd value while payment verification is still unwired.
"""
import json
import logging
import asyncio
from http.server import BaseHTTPRequestHandler

from database import db

logger = logging.getLogger(__name__)

MAX_BOOSTS_PER_REQUEST = 500
PRICE_PER_BOOST_USD = 0.12  # 10 -> $1.20, 20 -> $2.40, 50 -> $6.00, custom -> amount * this


class handler(BaseHTTPRequestHandler):

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
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

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b"{}"
            body = json.loads(raw or b"{}")
        except Exception:
            self._json(400, {"status": "error", "message": "Invalid JSON body"})
            return

        guild_id = str(body.get("guild_id") or "").strip()
        clone_id = body.get("clone_id")
        amount = body.get("amount")

        if not guild_id:
            self._json(400, {"status": "error", "message": "Missing guild_id"})
            return
        if not isinstance(amount, int) or amount <= 0:
            self._json(400, {"status": "error", "message": "amount must be a positive integer"})
            return
        if amount > MAX_BOOSTS_PER_REQUEST:
            self._json(400, {"status": "error", "message": f"amount exceeds {MAX_BOOSTS_PER_REQUEST}"})
            return

        async def _run():
            return await db.add_listing_boosts(guild_id, clone_id, amount)

        try:
            new_total = asyncio.run(_run())
        except Exception as e:
            logger.error(f"[v0] apply_boost error: {e}")
            self._json(500, {"status": "error", "message": "Internal error"})
            return

        self._json(200, {"status": "ok", "boost_count": new_total, "applied": amount})

    def log_message(self, format, *args):
        logger.debug(f"[v0] apply_boost: {format % args}")
