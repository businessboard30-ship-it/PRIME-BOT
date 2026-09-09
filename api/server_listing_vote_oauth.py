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
            logger.info("[vote-oauth] leg1 rejected: no guild_id in query %r", query)
            return 400, "<h2>Missing listing to vote for.</h2>"
        if not DISCORD_OAUTH_CLIENT_ID:
            logger.warning("[vote-oauth] leg1 rejected: DISCORD_OAUTH_CLIENT_ID not configured")
            return 200, "<h2>Voting sign-in isn't set up yet.</h2>"

        guild_id = int(guild_id_param)
        clone_id = int(clone_id_param) if clone_id_param else None
        oauth_state = secrets.token_urlsafe(24)
        await db.create_vote_oauth_state(oauth_state, guild_id, clone_id)
        logger.info("[vote-oauth] leg1 started: guild=%s clone_id=%s state=%s", guild_id, clone_id, oauth_state)
        params = {
            "client_id": DISCORD_OAUTH_CLIENT_ID,
            "redirect_uri": SERVER_LISTING_VOTE_OAUTH_REDIRECT_URI,
            "response_type": "code",
            "scope": "identify",
            "state": oauth_state,
        }
        return 302, f"{DISCORD_AUTHORIZE_URL}?{urlencode(params)}"

    if error:
        logger.info("[vote-oauth] leg2: Discord returned error=%s state=%s", error, state)
        return 302, _redirect_to_servers(None, "Sign-in was cancelled.", ok=False)

    if not state:
        logger.info("[vote-oauth] leg2 rejected: no state in query %r", query)
        return 400, "<h2>Missing sign-in state.</h2>"

    popped = await db.pop_vote_oauth_state(state)
    if popped is None:
        # This is one of the likeliest silent-failure causes: the oauth
        # state row is short-lived, so a slow Discord consent screen, a
        # double-click of "Vote", or the callback firing twice can all
        # land here — the vote page will say "expired, try again" but
        # nothing before this logged WHY, so a real expiry looked
        # identical to a bug. Logging the state here turns "vote didn't
        # count" into a greppable fact instead of a guess.
        logger.warning("[vote-oauth] leg2: state %s not found/already used — vote NOT cast", state)
        return 302, _redirect_to_servers(None, "That sign-in link expired. Try voting again.", ok=False)

    guild_id, clone_id = popped["guild_id"], popped["clone_id"]
    logger.info("[vote-oauth] leg2: state %s resolved to guild=%s clone_id=%s", state, guild_id, clone_id)

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
                identity = await identity_resp.json()
                voter_id = int(identity["id"])
                # global_name (the newer display name) falls back to the
                # legacy username when a user hasn't set one — either way
                # this is only ever used to label the leaderboard, never
                # trusted for anything auth-related (voter_id is).
                voter_username = identity.get("global_name") or identity.get("username")
    except (aiohttp.ClientError, asyncio.TimeoutError, KeyError, ValueError):
        logger.exception(
            "[vote-oauth] leg2: Discord OAuth exchange FAILED for guild=%s — vote NOT cast", guild_id,
        )
        return 302, _redirect_to_servers(guild_id, "Something went wrong signing you in.", ok=False)

    logger.info("[vote-oauth] leg2: identity resolved voter=%s (%s) for guild=%s", voter_id, voter_username, guild_id)
    newly_voted = await db.cast_server_listing_vote(guild_id, clone_id, voter_id, voter_username)
    logger.info(
        "[vote-oauth] leg2 complete: guild=%s voter=%s newly_voted=%s", guild_id, voter_id, newly_voted,
    )
    if newly_voted:
        msg = "Thanks for voting!"
    else:
        # 12h cooldown, not a permanent one-vote-ever block (see
        # cast_server_listing_vote) — tell them when they can come back.
        remaining = await db.get_vote_cooldown_remaining(guild_id, voter_id)
        if remaining is not None:
            hours = max(1, int(remaining.total_seconds() // 3600))
            msg = f"You already voted for this server — come back in about {hours}h."
        else:
            msg = "You already voted for this server."
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
