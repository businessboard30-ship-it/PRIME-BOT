# path: api/apply_boost.py

"""
Starts a real Paystack charge for purchased listing boosts instead of
crediting boost_count directly off a client-trusted POST (see git history
for the old do-not-wire-payment-yet stub this replaces — that existed only
so the boost-purchase UI, app/servers/_BoostModal.tsx, had working "apply"
logic to demo against).

Flow now matches the rest of this repo's Paystack checkouts
(discover_category_upgrade being the closest shape — see
database.create_discover_category_payment / api/paystack_webhook.py):
  1. This endpoint initializes a Paystack transaction for
     `amount * PRICE_PER_BOOST_USD` and inserts a 'pending' row in
     listing_boost_payments keyed by the reference Paystack returns.
  2. It responds with {"status": "pending", "authorization_url", "reference"}
     — the client redirects the browser to authorization_url to pay.
  3. api/paystack_webhook.py's charge.success handler (payment_type ==
     'listing_boost') marks that row 'paid' and credits boost_count via
     database.complete_listing_boost_payment — this endpoint never touches
     boost_count itself anymore.

Charged in USD directly (no live FX lookup) — this is a public,
unauthenticated web endpoint with no Discord interaction/locale to key a
currency choice off, so it keeps the same behavior for every visitor
rather than adding utils/currency's exchangerate.host round-trip as a new
point of failure here.

POST body: {"guild_id": "...", "clone_id": <int|null>, "amount": <int>,
            "email": "..."}
amount must be a positive integer, capped at MAX_BOOSTS_PER_REQUEST so a
malformed request can't try to charge an absurd amount. email is required
— Paystack needs one to initialize a transaction, and there's no logged-in
user here to source it from.
"""
import json
import logging
import asyncio
import re
from http.server import BaseHTTPRequestHandler

from database import db
from payments import paystack

logger = logging.getLogger(__name__)

MAX_BOOSTS_PER_REQUEST = 500
PRICE_PER_BOOST_USD = 0.12  # 10 -> $1.20, 20 -> $2.40, 50 -> $6.00, custom -> amount * this
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


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
        email = str(body.get("email") or "").strip()

        if not guild_id:
            self._json(400, {"status": "error", "message": "Missing guild_id"})
            return
        if not isinstance(amount, int) or amount <= 0:
            self._json(400, {"status": "error", "message": "amount must be a positive integer"})
            return
        if amount > MAX_BOOSTS_PER_REQUEST:
            self._json(400, {"status": "error", "message": f"amount exceeds {MAX_BOOSTS_PER_REQUEST}"})
            return
        if not email or not _EMAIL_RE.match(email):
            self._json(400, {"status": "error", "message": "A valid email is required to start checkout"})
            return

        price_usd = round(amount * PRICE_PER_BOOST_USD, 2)
        amount_minor_units = round(price_usd * 100)

        payment_result = paystack.initialize_payment(
            email,
            amount_minor_units,
            0,  # no logged-in user_id on this public endpoint
            f"ListingBoost_{guild_id}",
            payment_type="listing_boost",
            extra_metadata={"guild_id": guild_id, "clone_id": clone_id, "amount": amount},
            currency="USD",
        )

        if not payment_result or payment_result.get("status") != "success":
            logger.error(f"[v0] apply_boost: Paystack initialize failed for guild {guild_id}: {payment_result!r}")
            self._json(502, {"status": "error", "message": "Couldn't start checkout right now — please try again shortly"})
            return

        reference = payment_result["reference"]

        async def _run():
            await db.create_listing_boost_payment(guild_id, clone_id, reference, amount)

        try:
            asyncio.run(_run())
        except Exception as e:
            logger.error(f"[v0] apply_boost: failed to log pending payment {reference}: {e}")
            self._json(500, {"status": "error", "message": "Internal error"})
            return

        self._json(200, {
            "status": "pending",
            "authorization_url": payment_result["authorization_url"],
            "reference": reference,
            "amount": amount,
            "price_usd": price_usd,
        })

    def log_message(self, format, *args):
        logger.debug(f"[v0] apply_boost: {format % args}")
