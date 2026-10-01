# path: api/gumroad_webhook.py

"""Gumroad Ping receiver. Set the Ping URL in Gumroad (Settings -> Advanced)
to https://<host>/api/gumroad_webhook?secret=<GUMROAD_WEBHOOK_SECRET>."""

import asyncio
import hmac
import json
import logging
import time
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

logger = logging.getLogger(__name__)
_last_forbidden_alert = 0.0

# Owner-panel failure recording (modules/admin_money.py). Best-effort only: if
# the module can't even be imported, the webhook must behave exactly as before.
try:
    from modules.admin_money import record_failure_sync
except Exception:  # pragma: no cover
    def record_failure_sync(*a, **k):
        return False

# process_gumroad_ping results that mean "a sale did not unlock cleanly" (all
# but "unlock failed" are HTTP 200, so Gumroad never retries them). Benign
# results such as "ok", "already processed" or "test ping ignored" are not here.
_FAILURE_RESULTS = frozenset({
    "unlock failed", "product mismatch", "underpaid", "sale not verified",
    "unknown reference", "unknown subscription", "no reference",
})


class handler(BaseHTTPRequestHandler):
    def _reply(self, code: int, msg: str):
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(msg.encode())

    def do_POST(self):
        import config
        qs = parse_qs(urlparse(self.path).query)
        given = (qs.get("secret") or [""])[0]
        if not config.GUMROAD_WEBHOOK_SECRET or not hmac.compare_digest(given, config.GUMROAD_WEBHOOK_SECRET):
            logger.warning("[gumroad] ping rejected: missing/wrong secret in Ping URL")
            global _last_forbidden_alert
            if time.time() - _last_forbidden_alert > 900:  # at most one alert per 15 min
                _last_forbidden_alert = time.time()
                from gumroad_payments import _alert_owner
                try:
                    asyncio.run(_alert_owner(
                        "\u26a0\ufe0f A Gumroad ping was REJECTED (missing/wrong secret). "
                        "Fix the Ping URL in Gumroad -> Settings -> Advanced: "
                        "https://<api-host>/api/gumroad_webhook?secret=<GUMROAD_WEBHOOK_SECRET>. "
                        "Sales in the meantime were not unlocked."))
                except Exception:
                    logger.exception("[gumroad] forbidden alert failed")
            self._reply(403, "forbidden")
            record_failure_sync("gumroad", "bad_secret", detail="ping rejected: missing/wrong secret (403)")
            return

        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8", errors="replace")
        if "json" in (self.headers.get("Content-Type") or ""):
            try:
                fields = {k: str(v) for k, v in json.loads(body).items()}
            except ValueError:
                self._reply(400, "bad json")
                record_failure_sync("gumroad", "bad_json", detail="ping body was not valid JSON (400)")
                return
        else:
            fields = {k: v[0] for k, v in parse_qs(body, keep_blank_values=True).items()}

        from gumroad_payments import process_gumroad_ping
        try:
            code, msg = asyncio.run(process_gumroad_ping(fields))
        except Exception:
            logger.exception("[gumroad] webhook processing crashed")
            code, msg = 500, "error"
        self._reply(code, msg)
        if msg in _FAILURE_RESULTS or msg == "error" or code >= 500:
            record_failure_sync(
                "gumroad", msg,
                reference=fields.get("url_params[reference]") or fields.get("reference"),
                detail=f"sale {fields.get('sale_id', '?')} · product {fields.get('product_name', '?')} "
                       f"· price {fields.get('price', '?')} cents · http {code}")

    def log_message(self, format, *args):
        pass
