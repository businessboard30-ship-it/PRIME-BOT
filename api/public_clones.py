"""Read-only public directory of registered PRIME BOT Discord clones.

Only active clone identities and aggregate server counts are exposed. Never select
or serialize owner IDs, tokens, application secrets, or per-server records.
"""
import asyncio
import json
import logging
import time
from http.server import BaseHTTPRequestHandler

logger = logging.getLogger(__name__)
CACHE_SECONDS = 60
_cache = {"at": 0.0, "body": None}


async def _fetch_bots():
    from database import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn:
        # Safe/idempotent rollout for existing databases. Clone processes refresh
        # this field on_ready; until then the UI uses a neutral avatar fallback.
        await conn.execute(
            "ALTER TABLE discord_cloned_bots ADD COLUMN IF NOT EXISTS avatar_url TEXT"
        )
        rows = await conn.fetch(
            """
            SELECT c.clone_id, c.bot_username, c.bot_user_id, c.avatar_url,
                   (c.last_heartbeat >= NOW() - INTERVAL '2 minutes') AS online,
                   COUNT(DISTINCT g.guild_id) FILTER (WHERE g.left_at IS NULL)::int AS servers
            FROM discord_cloned_bots c
            LEFT JOIN discord_guilds g ON g.clone_id = c.clone_id
            WHERE c.status = 'active'
            GROUP BY c.clone_id, c.bot_username, c.bot_user_id, c.avatar_url,
                     c.last_heartbeat
            ORDER BY c.clone_id ASC
            LIMIT 1000
            """
        )
    bots = []
    for row in rows:
        bot_id = str(row["bot_user_id"] or "")
        # Discord's default-avatar CDN path is a safe fallback until a clone
        # process has published its current custom avatar URL.
        fallback = (
            f"https://cdn.discordapp.com/embed/avatars/{(int(bot_id) >> 22) % 6}.png"
            if bot_id.isdigit() else ""
        )
        bots.append({
            "name": row["bot_username"] or "Discord bot",
            "avatar_url": row["avatar_url"] or fallback,
            "servers": int(row["servers"] or 0),
            "online": bool(row["online"]),
        })
    return {"status": "ok", "bots": bots}


async def _handle():
    now = time.time()
    if _cache["body"] is not None and now - _cache["at"] < CACHE_SECONDS:
        return _cache["body"]
    body = json.dumps(await _fetch_bots(), separators=(",", ":"))
    _cache.update(at=now, body=body)
    return body


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            body = asyncio.run(_handle())
            status = 200
        except Exception:
            logger.exception("public_clones error")
            body = json.dumps({"status": "error", "bots": []})
            status = 500
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "public, max-age=60")
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))
