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
            return

        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8", errors="replace")
        if "json" in (self.headers.get("Content-Type") or ""):
            try:
                fields = {k: str(v) for k, v in json.loads(body).items()}
            except ValueError:
                self._reply(400, "bad json")
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

    def log_message(self, format, *args):
        pass
