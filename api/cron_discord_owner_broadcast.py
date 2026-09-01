"""
OPTIONAL cron-triggered endpoint that DMs out pending owner broadcasts,
queued by /ownerbroadcast in discord_bot/cogs/clone_admin.py.

Why this is a stateless serverless endpoint rather than a loop inside the
always-on gateway process: recipients span the main bot AND every
independent `python -m discord_bot.bot --clone-id N` clone process (see
discord_bot/clone_manager.py's docstring) — no single running process has
a live connection to every one of those users. Opening a DM channel and
posting via each clone's own REST token works regardless of which gateway
processes happen to be up, same approach as
api/cron_discord_announcements.py.

Sends in small batches per invocation (BATCH_SIZE per clone) with a short
delay between DMs to stay well under Discord's per-route rate limits —
wire this to run every minute or so via an external scheduler (Vercel
Cron, cron-job.org, etc.). A broadcast to a few thousand users will simply
take several cron ticks to fully land; that's expected, not a bug.

Auth: header "Authorization: Bearer <CRON_SECRET>" or query param ?secret=,
same convention as api/cron_discord_announcements.py.
"""
import json
import asyncio
import logging
from http.server import BaseHTTPRequestHandler
from typing import Optional
from urllib.parse import urlparse, parse_qs

import aiohttp

from config import CRON_SECRET, DISCORD_BOT_TOKEN, DISCORD_OWNER_BRAND_NAME
from database import db
from utils.crypto import secret_manager

# Deliberately NOT importing discord_bot.cogs._views_direct_paid here — this
# serverless function talks to Discord over raw REST (see module docstring)
# and every other api/ handler avoids pulling in the discord.py package for
# that reason. Duplicating just the custom_id string shape instead; it MUST
# stay identical to direct_paid_custom_id() in _views_direct_paid.py, which
# is what the gateway process's persistent DynamicItem actually matches
# against on click.


def _direct_paid_custom_id(payment_type: str) -> str:
    return f"direct_pay:{payment_type}"

logger = logging.getLogger(__name__)

DISCORD_API_BASE = "https://discord.com/api/v10"
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=10)
BATCH_SIZE_PER_CLONE = 25
# How many clones' worth of recipients we claim per tick. Claiming far more
# than we can actually send (the old code claimed 500 total regardless of
# how many clones that spanned) left the excess sitting claimed-but-idle
# until the 2-minute abandoned-claim window expired, needlessly slowing
# down how fast a broadcast drains — see release_owner_broadcast_recipient_claims.
MAX_CLONES_PER_TICK = 8
CLAIM_LIMIT = BATCH_SIZE_PER_CLONE * MAX_CLONES_PER_TICK
DM_SEND_DELAY_SECONDS = 0.4  # stays comfortably under Discord's DM rate limit


async def _token_for(clone_id):
    """None -> main bot's token. Otherwise decrypts the clone's own stored
    token, since only a clone's own token can DM someone who only ever
    interacted with that clone (they may not share the main bot at all)."""
    if clone_id is None:
        return DISCORD_BOT_TOKEN
    clone = await db.get_discord_clone(clone_id)
    if not clone or clone.get("status") != "active":
        return None
    return secret_manager.decrypt(clone["bot_token_encrypted"])


async def _dm_user(session: aiohttp.ClientSession, token: str, user_id: int, content: str,
                    image_url: Optional[str] = None, payment_button_type: Optional[str] = None) -> Optional[str]:
    """Returns None on success, or an error string on failure. A closed-DMs
    user (403) or a user who's left every mutual server (404 on channel
    open) are both expected, non-noisy failures — logged at debug, not
    warning, so a broadcast to thousands of users doesn't flood logs."""
    headers = {"Authorization": f"Bot {token}", "Content-Type": "application/json"}
    try:
        async with session.post(
            f"{DISCORD_API_BASE}/users/@me/channels",
            headers=headers, json={"recipient_id": user_id}
        ) as resp:
            if resp.status not in (200, 201):
                body = await resp.text()
                logger.debug(f"[cron_discord_owner_broadcast] Couldn't open DM with {user_id}: HTTP {resp.status} {body}")
                return f"open_dm_failed:{resp.status}"
            channel = await resp.json()
            channel_id = channel["id"]

        payload = {"content": content}
        if image_url:
            # An embed image (rather than re-uploading the file ourselves)
            # since we only kept Discord's CDN URL from the original
            # attachment — see the caveat on that URL's lifetime in
            # clone_admin.py's ownerbroadcast command.
            payload["embeds"] = [{"image": {"url": image_url}}]
        if payment_button_type:
            # type 1 = action row, type 2 = button, style 3 = success (green).
            # Caught by the gateway process's persistent _DirectPaidButton
            # DynamicItem (discord_bot/cogs/_views_direct_paid.py) — this
            # raw REST send never needs its own interaction handling.
            payload["components"] = [{
                "type": 1,
                "components": [{
                    "type": 2, "style": 3, "label": "✅ I've Paid",
                    "custom_id": _direct_paid_custom_id(payment_button_type),
                }],
            }]

        async with session.post(
            f"{DISCORD_API_BASE}/channels/{channel_id}/messages",
            headers=headers, json=payload
        ) as resp:
            if resp.status in (200, 201):
                return None
            body = await resp.text()
            logger.debug(f"[cron_discord_owner_broadcast] Couldn't DM {user_id}: HTTP {resp.status} {body}")
            return f"send_failed:{resp.status}"
    except (aiohttp.ClientError, TimeoutError) as e:
        return f"network_error:{e}"


def _format_message(raw_message: str) -> str:
    return f"📢 **Announcement from {DISCORD_OWNER_BRAND_NAME}**\n\n{raw_message}"


async def run_pending_owner_broadcasts() -> dict:
    broadcasts = await db.get_pending_owner_broadcasts()
    totals = {"broadcasts": len(broadcasts), "sent": 0, "failed": 0}

    async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT) as session:
        for broadcast in broadcasts:
            content = _format_message(broadcast["message"])
            recipients = await db.get_owner_broadcast_recipient_batch(broadcast["id"], limit=CLAIM_LIMIT)

            # Group this batch by clone_id so we resolve/decrypt each
            # clone's token once rather than once per recipient.
            by_clone: dict = {}
            for r in recipients:
                by_clone.setdefault(r["clone_id"], []).append(r)

            unattempted_ids = []
            for clone_id, clone_recipients in by_clone.items():
                token = await _token_for(clone_id)
                if not token:
                    for r in clone_recipients:
                        await db.mark_owner_broadcast_recipient_sent(r["id"], error="clone_inactive_or_missing")
                        totals["failed"] += 1
                    continue

                to_send, leftover = clone_recipients[:BATCH_SIZE_PER_CLONE], clone_recipients[BATCH_SIZE_PER_CLONE:]
                unattempted_ids.extend(r["id"] for r in leftover)

                for r in to_send:
                    error = await _dm_user(
                        session, token, r["user_id"], content,
                        broadcast.get("image_url"), broadcast.get("payment_button_type"),
                    )
                    await db.mark_owner_broadcast_recipient_sent(r["id"], error=error)
                    totals["sent" if error is None else "failed"] += 1
                    await asyncio.sleep(DM_SEND_DELAY_SECONDS)

            # Anything claimed this tick but not actually attempted (this
            # broadcast spanned more clones than we send per tick) goes
            # back into the pool immediately rather than sitting claimed
            # for up to 2 minutes — see release_owner_broadcast_recipient_claims.
            await db.release_owner_broadcast_recipient_claims(unattempted_ids)

            await db.finalize_owner_broadcast_if_done(broadcast["id"])

    return totals


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
            result = asyncio.run(run_pending_owner_broadcasts())
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "ok", **result}).encode())
        except Exception as e:
            logger.error(f"[cron_discord_owner_broadcast] error: {e}")
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "error", "message": str(e)}).encode())
