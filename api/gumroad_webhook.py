# path: api/gumroad_webhook.py

"""Gumroad Ping receiver. Set the Ping URL in Gumroad (Settings -> Advanced)
to https://<host>/api/gumroad_webhook?secret=<GUMROAD_WEBHOOK_SECRET>."""

import asyncio
import hmac
import json
import logging
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

logger = logging.getLogger(__name__)


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
