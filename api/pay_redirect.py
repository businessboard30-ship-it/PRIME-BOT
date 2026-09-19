# path: api/pay_redirect.py

"""GET /pay?t=<token> — the one 'Pay' button. Detects the visitor's country
from their IP and redirects to Paystack (Ghana) or Gumroad (everywhere else).

The bot stores the purchase details under the token (payments_manual.
start_geo_payment). Here we resolve the country, create the checkout for the
right provider, and redirect. Refreshing reuses the same checkout instead of
creating a duplicate. If the country lookup fails we fall back to a plain page
with both options (?r=gh / ?r=intl)."""

import asyncio
import json
import logging
import time
import urllib.request
from html import escape
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

logger = logging.getLogger(__name__)

_TTL = 3600


def _client_ip(h) -> str:
    fwd = h.headers.get("X-Forwarded-For") or ""
    if fwd:
        return fwd.split(",")[0].strip()
    return (h.headers.get("X-Real-IP") or h.client_address[0] or "").strip()


def _country_for_ip(ip: str):
    """Two-letter country code, or None if it can't be determined."""
    if not ip or ip.startswith(("127.", "10.", "192.168.", "::1")):
        return None
    try:
        req = urllib.request.Request(f"https://ipwho.is/{ip}?fields=success,country_code",
                                     headers={"User-Agent": "prime-bot"})
        with urllib.request.urlopen(req, timeout=3) as r:
            data = json.loads(r.read().decode())
        if data.get("success"):
            return (data.get("country_code") or "").upper() or None
    except Exception:
        logger.warning("[pay] geo lookup failed for %s", ip, exc_info=True)
    return None


class handler(BaseHTTPRequestHandler):
    def _html(self, code: int, body: str):
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode())

    def _redirect(self, url: str):
        self.send_response(302)
        self.send_header("Location", url)
        self.end_headers()

    def do_GET(self):
        import config  # noqa: F401  (ensures env/config is loaded)
        from database import db
        from payments_manual import create_checkout_for_intent

        qs = parse_qs(urlparse(self.path).query)
        token = (qs.get("t") or [""])[0]
        override = (qs.get("r") or [""])[0]
        if not token or len(token) > 64:
            self._html(400, "<h3>Invalid payment link.</h3>")
            return

        key = f"payintent:{token}"
        raw = asyncio.run(db.get_global_setting(key))
        if not raw:
            self._html(404, "<h3>This payment link has expired. Please start the purchase again in Discord.</h3>")
            return
        intent = json.loads(raw)
        if time.time() - float(intent.get("created", 0)) > _TTL:
            self._html(410, "<h3>This payment link has expired. Please start the purchase again in Discord.</h3>")
            return

        if override in ("gh", "intl"):
            region = "GH" if override == "gh" else "XX"
        else:
            region = _country_for_ip(_client_ip(self))
            if region is None:
                self._html(200, (
                    "<body style='font-family:sans-serif;text-align:center;padding:40px'>"
                    "<h3>Choose your checkout</h3>"
                    f"<p><a href='/pay?t={escape(token)}&r=gh'>🇬🇭 Ghana — Paystack</a></p>"
                    f"<p><a href='/pay?t={escape(token)}&r=intl'>🌍 International — Gumroad</a></p></body>"
                ))
                return

        resolved = intent.get("resolved") or {}
        url = resolved.get("GH" if region == "GH" else "XX")
        if not url:
            try:
                url = asyncio.run(create_checkout_for_intent(intent, region))
            except Exception:
                logger.exception("[pay] checkout creation crashed")
                url = None
            if not url:
                self._html(502, "<h3>Couldn't start checkout right now. Please try again shortly.</h3>")
                return
            resolved["GH" if region == "GH" else "XX"] = url
            intent["resolved"] = resolved
            asyncio.run(db.set_global_setting(key, json.dumps(intent)))
        self._redirect(url)

    def log_message(self, format, *args):
        pass
