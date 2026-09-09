# FULL PATH: PRIME-BOT-main/api/site_visits.py

"""
Backs the small "X site visits" banner (app/_VisitBanner.tsx). Public,
no auth, no per-visitor identity — just a single running total (see
site_visit_counter's comment in database.py's _create_tables).

One GET = one visit counted. Deliberately does the increment AND returns
the new total in the same request rather than a separate record/read pair,
since the banner only ever wants "count this page load and tell me the
number" — see db.record_site_visit.
"""
import asyncio
import logging
from http.server import BaseHTTPRequestHandler

from database import db

logger = logging.getLogger(__name__)

_initialized = False


async def _handle() -> tuple[int, str]:
    global _initialized
    if not _initialized:
        from init_system import initialize_system
        await initialize_system()
        _initialized = True

    import json
    count = await db.record_site_visit()
    return 200, json.dumps({"status": "ok", "count": count})


class handler(BaseHTTPRequestHandler):
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        import json
        try:
            status, body = asyncio.run(_handle())
        except Exception:
            logger.exception("site_visits error")
            status, body = 500, json.dumps({"status": "error", "message": "Internal error"})

        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self._cors()
        self.end_headers()
        self.wfile.write(body.encode())
