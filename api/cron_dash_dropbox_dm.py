"""
Cron-triggered sender for Drop Box messages the owner chose to push by DM.

Queue lives in dash_dropbox_dm (see database.py). Same conventions as
api/cron_discord_owner_broadcast.py: small batch per tick, short delay between DMs,
hard wall-clock budget, Authorization: Bearer <CRON_SECRET> (or ?secret=).

Failure policy: a closed-DM / unreachable user (403/404) is logged and marked failed,
final. Only rate limits (429), Discord 5xx and network errors are retried, and at most
MAX_ATTEMPTS times in total. Nothing is ever retried forever.
"""
import json
import asyncio
import logging
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

import aiohttp

from config import CRON_SECRET, DISCORD_BOT_TOKEN
from database import db
from utils import dash_schema as S
from api.cron_discord_owner_broadcast import _dm_user

logger = logging.getLogger(__name__)

BATCH_SIZE = 25
DM_DELAY_SECONDS = 0.4
WALL_CLOCK_BUDGET_SECONDS = 20
MAX_ATTEMPTS = 3
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=10)


def is_retryable(error: str) -> bool:
    """True for transient failures: rate limit, Discord 5xx, network. Closed DMs are final."""
    if error.startswith("network_error"):
        return True
    try:
        code = int(error.rsplit(":", 1)[1])
    except (IndexError, ValueError):
        return False
    return code == 429 or code >= 500


async def run_pending_dropbox_dms() -> dict:
    totals = {"sent": 0, "failed": 0, "retry": 0}
    if not DISCORD_BOT_TOKEN:
        return {**totals, "error": "no_bot_token"}
    deadline = asyncio.get_event_loop().time() + WALL_CLOCK_BUDGET_SECONDS
    batch = await db.dropbox_dm_claim(BATCH_SIZE)
    async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT) as session:
        for i, row in enumerate(batch):
            if asyncio.get_event_loop().time() >= deadline:
                # Not attempted: hand the claim back without costing an attempt.
                await db.dropbox_dm_release([r["id"] for r in batch[i:]])
                break
            text = S.format_dropbox_dm(row["title"], row["body"], row["kind"])
            error = await _dm_user(session, DISCORD_BOT_TOKEN, int(row["user_id"]), text)
            if error is None:
                await db.dropbox_dm_finish(row["id"])
                totals["sent"] += 1
            else:
                retry = is_retryable(error)
                await db.dropbox_dm_finish(row["id"], error=error, retry=retry, max_attempts=MAX_ATTEMPTS)
                if retry and row["attempts"] < MAX_ATTEMPTS:
                    totals["retry"] += 1
                else:
                    totals["failed"] += 1
                    logger.info("[dropbox dm] giving up on user %s for message %s: %s", row["user_id"], row["message_id"], error)
            await asyncio.sleep(DM_DELAY_SECONDS)
    return totals


async def run_message_notices() -> dict:
    """Opt-in only (off by default): one generic Discord DM per person per hour when they have unread dashboard messages.
    The DM never contains a name or any message text. Closed DMs switch the notice off for that person."""
    totals = {"sent": 0, "failed": 0}
    if not DISCORD_BOT_TOKEN:
        return {**totals, "error": "no_bot_token"}
    from modules import admin_controls, member_msg
    if member_msg.SWITCH in await admin_controls.current_switches():
        return {**totals, "skipped": "messaging_off"}
    deadline = asyncio.get_event_loop().time() + 10
    async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT) as session:
        for uid in await db.msg_notify_claim(BATCH_SIZE):
            if asyncio.get_event_loop().time() >= deadline:
                break
            error = await _dm_user(session, DISCORD_BOT_TOKEN, int(uid), member_msg.NOTICE_TEXT)
            if error is None:
                totals["sent"] += 1
            else:
                totals["failed"] += 1
                if not is_retryable(error):
                    await db.msg_dm_disable(uid)
            await asyncio.sleep(DM_DELAY_SECONDS)
    return totals


class handler(BaseHTTPRequestHandler):

    def _authorized(self) -> bool:
        if not CRON_SECRET:
            return False
        if self.headers.get("Authorization", "") == f"Bearer {CRON_SECRET}":
            return True
        return parse_qs(urlparse(self.path).query).get("secret", [""])[0] == CRON_SECRET

    def _send(self, code: int, payload: dict):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode())

    def do_GET(self):
        self._handle()

    def do_POST(self):
        self._handle()

    def _handle(self):
        if not self._authorized():
            return self._send(401, {"status": "error", "message": "Unauthorized"})
        try:
            out = asyncio.run(run_pending_dropbox_dms())
            try:
                out["msg_notice"] = asyncio.run(run_message_notices())
            except Exception as e:
                logger.error("[cron_dash_dropbox_dm] message notices failed: %s", type(e).__name__)
            self._send(200, {"status": "ok", **out})
        except Exception as e:
            logger.error("[cron_dash_dropbox_dm] error: %s", e)
            self._send(500, {"status": "error", "message": str(e)})
