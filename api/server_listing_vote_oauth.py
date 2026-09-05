# FULL PATH: PRIME-BOT-main/api/server_listing_vote_oauth.py

"""
Vote sign-in for the public /servers directory.

Not a bot command — deliberately, since the bot is already at/over
Discord's 100 top-level-command cap (see server_listing.py's docstring).
Voting needs to know WHO is voting (one vote per person), and the only way
to get that without a slash command is the same "identify"-scope OAuth2
web flow already used by api/discover_oauth_join.py and api/bump_oauth.py —
so this file is a near-exact copy of discover_oauth_join.py's two-leg
shape, just swapping "join a category" for "record a vote".

Leg 1 (no ?code): GET /api/server_listing_vote_oauth?guild_id=<id>&clone_id=<id?>
  -> mint a one-time oauth state, redirect into Discord's consent screen.
Leg 2 (callback, ?code&?state): exchange code for identity, cast the vote,
  redirect back to the /servers page with a small result banner.

Same trust rule as everywhere else in this codebase: the voter's id is
ALWAYS the one Discord's own /users/@me handed back after the token
exchange, never anything read from the query string or request body.
"""

import asyncio
import logging
import secrets
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, urlencode

import aiohttp

from config import (
    DISCORD_OAUTH_CLIENT_ID,
    DISCORD_OAUTH_CLIENT_SECRET,
    SERVER_LISTING_VOTE_OAUTH_REDIRECT_URI,
    DASHBOARD_BASE_URL,
)
from database import db

logger = logging.getLogger(__name__)

DISCORD_AUTHORIZE_URL = "https://discord.com/api/oauth2/authorize"
DISCORD_TOKEN_URL = "https://discord.com/api/oauth2/token"
DISCORD_API_BASE = "https://discord.com/api/v10"

_initialized = False


def _redirect_to_servers(guild_id, message: str, ok: bool) -> str:
    params = {"vote": "ok" if ok else "err", "msg": message, "guild_id": guild_id or ""}
    return f"{DASHBOARD_BASE_URL}/servers?{urlencode(params)}"


async def _handle(query: dict) -> tuple[int, str]:
    global _initialized
    if not _initialized:
        from init_system import initialize_system
        await initialize_system()
        _initialized = True

    code_param = query.get("code", [None])[0]
    state = query.get("state", [None])[0]
    error = query.get("error", [None])[0]

    # Leg 1: fresh click from the vote button. guild_id/clone_id arrive as
    # plain query params here (not sensitive — same info shown publicly on
    # the listing card), but the OAUTH STATE we mint is what actually gets
    # trusted on the way back, not these.
    if code_param is None and error is None and state is None:
        guild_id_param = query.get("guild_id", [None])[0]
        clone_id_param = query.get("clone_id", [None])[0]
        if not guild_id_param:
            return 400, "<h2>Missing listing to vote for.</h2>"
        if not DISCORD_OAUTH_CLIENT_ID:
            return 200, "<h2>Voting sign-in isn't set up yet.</h2>"

        guild_id = int(guild_id_param)
        clone_id = int(clone_id_param) if clone_id_param else None
        oauth_state = secrets.token_urlsafe(24)
        await db.create_vote_oauth_state(oauth_state, guild_id, clone_id)
        params = {
            "client_id": DISCORD_OAUTH_CLIENT_ID,
            "redirect_uri": SERVER_LISTING_VOTE_OAUTH_REDIRECT_URI,
            "response_type": "code",
            "scope": "identify",
            "state": oauth_state,
        }
        return 302, f"{DISCORD_AUTHORIZE_URL}?{urlencode(params)}"

    if error:
        return 302, _redirect_to_servers(None, "Sign-in was cancelled.", ok=False)

    if not state:
        return 400, "<h2>Missing sign-in state.</h2>"

    popped = await db.pop_vote_oauth_state(state)
    if popped is None:
        return 302, _redirect_to_servers(None, "That sign-in link expired. Try voting again.", ok=False)

    guild_id, clone_id = popped["guild_id"], popped["clone_id"]

    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                DISCORD_TOKEN_URL,
                data={
                    "client_id": DISCORD_OAUTH_CLIENT_ID,
                    "client_secret": DISCORD_OAUTH_CLIENT_SECRET,
                    "grant_type": "authorization_code",
                    "code": code_param,
                    "redirect_uri": SERVER_LISTING_VOTE_OAUTH_REDIRECT_URI,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            ) as token_resp:
                token_resp.raise_for_status()
                access_token = (await token_resp.json())["access_token"]

            async with session.get(
                f"{DISCORD_API_BASE}/users/@me",
                headers={"Authorization": f"Bearer {access_token}"},
            ) as identity_resp:
                identity_resp.raise_for_status()
                voter_id = int((await identity_resp.json())["id"])
    except (aiohttp.ClientError, asyncio.TimeoutError, KeyError, ValueError):
        logger.exception("Discord OAuth exchange failed for server-listing vote (guild %s)", guild_id)
        return 302, _redirect_to_servers(guild_id, "Something went wrong signing you in.", ok=False)

    newly_voted = await db.cast_server_listing_vote(guild_id, clone_id, voter_id)
    msg = "Thanks for voting!" if newly_voted else "You already voted for this server."
    return 302, _redirect_to_servers(guild_id, msg, ok=True)


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)

        try:
            status, body = asyncio.run(_handle(query))
        except Exception:
            logger.exception("server_listing_vote_oauth callback error")
            status, body = 500, "<h2>Something went wrong. Please try again.</h2>"

        if status == 302:
            self.send_response(302)
            self.send_header("Location", body)
            self.end_headers()
            return

        self.send_response(status)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(body.encode())
