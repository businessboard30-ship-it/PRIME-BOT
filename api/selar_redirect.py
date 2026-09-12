# path: api/selar_redirect.py

"""
Where Selar sends the buyer's browser back to after checkout completes —
payments_manual.py bakes this URL (with reference/payment_type/buyer_id/
target ids + an HMAC signature) into the `redirect_url` query param of
every Selar pay link it generates, so Selar hands it straight back once
payment finishes.

This is the ONLY intended entry point into the web /unlock page: a request
here without a signature that verifies is bounced to /unlock?state=invalid
rather than /unlock?state=pending, and the frontend treats those two states
differently (see app/unlock/unlock-status.tsx). Guessing a reference format
alone (they're visible in the Selar dashboard's buyer-email column) is not
enough to reach a usable /unlock link — the signature can only have been
produced by payments_manual.py's own POW_SECRET_KEY-backed signing
(utils/selar_signing.py), never by a client.

GET params: reference, payment_type, buyer_id, sig, and optionally
guild_id and/or clone_id (whichever one payments_manual.py signed with —
never both unset, never both set).
"""
import logging
import re
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, urlencode

from config import SITE_URL
from utils.selar_signing import verify_selar_target

logger = logging.getLogger(__name__)

_REFERENCE_RE = re.compile(r"^[A-Za-z0-9._:-]{6,160}$")
_PAYMENT_TYPE_RE = re.compile(r"^[a-z_]{1,50}$")


class handler(BaseHTTPRequestHandler):

    def do_GET(self):
        raw_path = self.path
        query = parse_qs(urlparse(raw_path).query)

        # TEMPORARY diagnostic — logs the exact raw request Selar sends on
        # redirect, unconditionally, before any validation. Purpose: confirm
        # once and for all whether Selar's post-checkout redirect actually
        # carries the reference/payment_type/buyer_id/sig/ts query params
        # payments_manual.py baked into the pay link's redirect_url, or
        # whether it fires with an empty/stripped query string instead.
        # DELETE this log line once that's confirmed either way — it's not
        # meant to stay in production (full raw path may end up in log
        # aggregators/monitoring you don't control).
        logger.warning(f"[selar-redirect][DIAGNOSTIC] raw incoming path: {raw_path!r}")

        def _one(key):
            values = query.get(key)
            return values[0].strip() if values else ""

        reference = _one("reference")
        payment_type = _one("payment_type")
        buyer_id_raw = _one("buyer_id")
        sig = _one("sig")
        ts_raw = _one("ts")
        guild_id_raw = _one("guild_id") or None
        clone_id_raw = _one("clone_id") or None

        valid = (
            _REFERENCE_RE.match(reference)
            and _PAYMENT_TYPE_RE.match(payment_type)
            and buyer_id_raw.isdigit()
            and ts_raw.isdigit()
            and (guild_id_raw is None or guild_id_raw.isdigit())
            and (clone_id_raw is None or clone_id_raw.isdigit())
        )
        if valid:
            buyer_id = int(buyer_id_raw)
            guild_id = int(guild_id_raw) if guild_id_raw else None
            clone_id = int(clone_id_raw) if clone_id_raw else None
            valid = verify_selar_target(reference, payment_type, buyer_id, guild_id, clone_id, int(ts_raw), sig)

        if not valid:
            logger.warning(f"[selar-redirect] rejected redirect for reference={reference!r} (bad/missing/expired signature)")
            self._redirect(f"{SITE_URL}/unlock?state=invalid")
            return

        unlock_params = {"reference": reference, "payment_type": payment_type, "buyer_id": buyer_id_raw, "sig": sig, "ts": ts_raw, "state": "pending"}
        if guild_id_raw:
            unlock_params["guild_id"] = guild_id_raw
        if clone_id_raw:
            unlock_params["clone_id"] = clone_id_raw
        self._redirect(f"{SITE_URL}/unlock?{urlencode(unlock_params)}")

    def _redirect(self, location: str):
        self.send_response(302)
        self.send_header("Location", location)
        self.end_headers()

    def log_message(self, format, *args):
        logger.debug(f"[selar-redirect] {format % args}")
