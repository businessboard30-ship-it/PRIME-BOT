# path: api/public_stats.py

"""
Public, read-only stats for the marketing site's stats strip and status page
(website/assets/site.js). No auth, no per-user data: just the number of
servers the bots are currently in. Answers are cached in-process for a few
minutes so a busy site can't hammer the database.

GET /api/public_stats -> {"servers": 123, "commands": 87?, "status": "ok"}
"commands" is only included when PUBLIC_COMMAND_COUNT is set.
"""
import asyncio
import json
import logging
import os
import time
from http.server import BaseHTTPRequestHandler

logger = logging.getLogger(__name__)

CACHE_SECONDS = 300
_cache = {"at": 0.0, "body": None}
_initialized = False


async def _count_servers() -> int:
    global _initialized
    if not _initialized:
        from init_system import initialize_system
        await initialize_system()
        _initialized = True
    from database import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn:
        n = await conn.fetchval("SELECT COUNT(DISTINCT guild_id) FROM discord_guilds WHERE left_at IS NULL")
    return int(n or 0)


def build_body(servers: int, commands_env: str = "") -> dict:
    body = {"status": "ok", "servers": int(servers)}
    try:
        c = int(str(commands_env).strip())
        if c > 0:
            body["commands"] = c
    except (TypeError, ValueError):
        pass
    return body


async def _handle() -> tuple[int, str]:
    now = time.time()
    if _cache["body"] and now - _cache["at"] < CACHE_SECONDS:
        return 200, _cache["body"]
    body = json.dumps(build_body(await _count_servers(), os.getenv("PUBLIC_COMMAND_COUNT", "")))
    _cache.update(at=now, body=body)
    return 200, body


class handler(BaseHTTPRequestHandler):
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        try:
            status, body = asyncio.run(_handle())
        except Exception:
            logger.exception("public_stats error")
            status, body = 500, json.dumps({"status": "error"})
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "public, max-age=120")
        self._cors()
        self.end_headers()
        self.wfile.write(body.encode())
