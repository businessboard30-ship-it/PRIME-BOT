"""
OPTIONAL cron-triggered endpoint that sends due Discord scheduled
announcements (queued by /announce in discord_bot/cogs/automation.py).

Why this exists as a stateless serverless endpoint rather than a loop
inside the always-on gateway process: a guild's announcements can be
configured on the main bot OR on any number of clones, and this repo's
Discord clone processes are independent `python -m discord_bot.bot
--clone-id N` processes (see discord_bot/clone_manager.py's docstring) —
there's no single process that's guaranteed to be running for every
guild/clone combination at send time. Posting via Discord's REST API with
the row's own clone's token (same pattern as discord_bot/role_grant.py)
works regardless of which gateway processes happen to be up.

Auth: header "Authorization: Bearer <CRON_SECRET>" or query param ?secret=,
same convention as api/cron_broadcast.py — wire this to an external
scheduler (Vercel Cron, cron-job.org, etc.) hitting this URL every minute
or so. Nothing calls it automatically; scheduled announcements simply won't
send until something does.
"""
import json
import asyncio
import logging
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

import aiohttp

from config import CRON_SECRET, DISCORD_BOT_TOKEN
from database import db
from utils.crypto import secret_manager

logger = logging.getLogger(__name__)

DISCORD_API_BASE = "https://discord.com/api/v10"
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=10)


async def _token_for(clone_id):
    """None -> main bot's token. Otherwise decrypts the clone's own stored
    token — the main bot's token can't post into a guild only a clone is
    actually a member of."""
    if clone_id is None:
        return DISCORD_BOT_TOKEN
    clone = await db.get_discord_clone(clone_id)
    if not clone:
        logger.warning(f"[cron_discord_announcements] clone_id={clone_id} not found, skipping its announcements")
        return None
    return secret_manager.decrypt(clone["bot_token_encrypted"])


async def _send_announcement(session: aiohttp.ClientSession, token: str, channel_id: int, message: str) -> bool:
    url = f"{DISCORD_API_BASE}/channels/{channel_id}/messages"
    headers = {"Authorization": f"Bot {token}"}
    try:
        async with session.post(url, headers=headers, json={"content": message}) as resp:
            if resp.status in (200, 201):
                return True
            body = await resp.text()
            logger.warning(f"[cron_discord_announcements] Failed to post to channel {channel_id}: HTTP {resp.status} {body}")
            return False
    except (aiohttp.ClientError, TimeoutError) as e:
        logger.warning(f"[cron_discord_announcements] Network error posting to channel {channel_id}: {e}")
        return False


async def run_due_announcements() -> dict:
    due = await db.get_due_announcements()
    sent, failed = 0, 0
    async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT) as session:
        for row in due:
            token = await _token_for(row["clone_id"])
            if not token:
                failed += 1
                continue
            ok = await _send_announcement(session, token, row["channel_id"], row["message"])
            if ok:
                await db.mark_announcement_sent(row["id"])
                sent += 1
            else:
                failed += 1
    return {"due": len(due), "sent": sent, "failed": failed}


class handler(BaseHTTPRequestHandler):

    def _authorized(self) -> bool:
        if not CRON_SECRET:
            return False
        auth_header = self.headers.get("Authorization", "")
        if auth_header == f"Bearer {CRON_SECRET}":
            return True
        query = parse_qs(urlparse(self.path).query)
        return query.get("secret", [""])[0] == CRON_SECRET

    def do_GET(self):
        self._handle()

    def do_POST(self):
        self._handle()

    def _handle(self):
        if not self._authorized():
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "error", "message": "Unauthorized"}).encode())
            return

        try:
            result = asyncio.run(run_due_announcements())
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "ok", **result}).encode())
        except Exception as e:
            logger.error(f"[v0] cron_discord_announcements error: {e}")
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "error", "message": str(e)}).encode())
