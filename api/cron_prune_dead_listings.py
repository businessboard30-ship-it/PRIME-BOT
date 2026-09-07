# path: api/cron_prune_dead_listings.py

"""
Cron-triggered endpoint that sweeps the server directory (server_listings)
for dead invite links and deletes those listings outright — there's no
"unlisted"/hidden state in this schema, and a listing nobody can join is
just clutter (see item #15 of the server-listing feature list: "auto-prune
listings whose invite link 404s").

Checks each listing's invite_url against Discord's public, unauthenticated
`GET /invites/{code}` endpoint (no bot token needed — this is the same
endpoint discord.gg link previews use). A 404/410 means the invite is dead;
anything else (200, or a transient error talking to Discord) leaves the
listing alone, since a network hiccup shouldn't delist someone.

Auth: same pattern as api/cron_expire_monetization.py — either header
"Authorization: Bearer <CRON_SECRET>" (what Vercel Cron sends automatically
when CRON_SECRET is set) or query param ?secret=<CRON_SECRET>. Run this
weekly; invite links don't go stale fast enough to need daily sweeps.
"""
import asyncio
import json
import logging
import re
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

import httpx

from config import CRON_SECRET
from database import db

logger = logging.getLogger(__name__)

_INVITE_CODE_RE = re.compile(r"(?:discord\.gg|discord\.com/invite)/([^/?\s]+)", re.IGNORECASE)


async def _invite_is_dead(client: httpx.AsyncClient, invite_url: str) -> bool:
    m = _INVITE_CODE_RE.search(invite_url)
    if not m:
        # Not even a recognizable invite shape — leave it for a human
        # rather than guessing; server_listings.py's POST validation should
        # have already stopped this from being saved in the first place.
        return False
    code = m.group(1)
    try:
        resp = await client.get(f"https://discord.com/api/v10/invites/{code}", timeout=8.0)
    except httpx.HTTPError:
        return False
    return resp.status_code in (404, 410)


class handler(BaseHTTPRequestHandler):

    def _authorized(self) -> bool:
        if not CRON_SECRET:
            return False
        auth_header = self.headers.get("Authorization", "")
        if auth_header == f"Bearer {CRON_SECRET}":
            return True
        query = parse_qs(urlparse(self.path).query)
        return query.get("secret", [""])[0] == CRON_SECRET

    def _json(self, status: int, payload: dict):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode())

    def do_GET(self):
        if not self._authorized():
            self._json(403, {"status": "error", "message": "Unauthorized"})
            return

        async def _run():
            listings = await db.get_all_listing_invites()
            removed = []
            async with httpx.AsyncClient() as client:
                for listing in listings:
                    if await _invite_is_dead(client, listing["invite_url"]):
                        await db.delete_server_listing(listing["guild_id"], listing["clone_id"])
                        removed.append(listing["guild_id"])
            return removed

        try:
            removed = asyncio.run(_run())
        except Exception as e:
            logger.error(f"[v0] cron_prune_dead_listings error: {e}")
            self._json(500, {"status": "error", "message": "Internal error"})
            return

        self._json(200, {"status": "ok", "removed_count": len(removed), "removed_guild_ids": removed})
