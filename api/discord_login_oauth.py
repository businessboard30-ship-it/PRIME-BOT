# FULL PATH: PRIME-BOT-main/api/discord_login_oauth.py

"""
"Sign in with Discord" for the landing page.

There's still no user-account system in this repo (see discord_dashboard.py's
docstring) — this endpoint doesn't add one. It automates the thing a server
admin would otherwise do by typing /automod dashboard into Discord: prove
who they are via Discord's own OAuth2 "identify guilds" scope, then hand
back the exact same capability-token dashboard links get_or_create_
dashboard_token already mints for the slash command. The token is still the
credential either way — this just saves a trip into Discord to fetch it.

Three legs, all on this one path (mirrors server_listing_vote_oauth.py's
shape):

Leg 1 (no ?code, no ?session): GET /api/discord_login_oauth
  -> mint a one-time oauth state, redirect into Discord's consent screen.

Leg 2 (callback, ?code&?state): exchange code for identity + the user's
  guild list (with per-guild permissions Discord itself computed), keep
  only guilds where (a) the user has Manage Server or Administrator, and
  (b) PRIME-BOT is actually in that guild right now (discord_guilds table —
  never trust "is the bot here" to anything Discord's OAuth response says,
  since that scope doesn't even tell us that). Mint/reuse a dashboard token
  for each, stash the result as a session row, redirect to the dashboard
  site's /login/servers?session=<id>.

Leg 3 (?session only): the /login/servers page can't reach Discord's token
  endpoint itself (that needs the client secret), so it calls back here to
  read the session row this handler already computed. Returns JSON, not a
  redirect.

Same trust rule as everywhere else in this codebase: the signed-in user's
id/guilds/permissions are ALWAYS whatever Discord's own API handed back
after the token exchange, never anything read from the query string.
"""

import asyncio
import json
import logging
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, urlencode

import aiohttp

from config import (
    DISCORD_OAUTH_CLIENT_ID,
    DISCORD_OAUTH_CLIENT_SECRET,
    DISCORD_LOGIN_OAUTH_REDIRECT_URI,
    DASHBOARD_BASE_URL,
)
from database import db

logger = logging.getLogger(__name__)

DISCORD_AUTHORIZE_URL = "https://discord.com/api/oauth2/authorize"
DISCORD_TOKEN_URL = "https://discord.com/api/oauth2/token"
DISCORD_API_BASE = "https://discord.com/api/v10"

# Discord permission bitfield flags relevant here (see Discord's
# "Permissions" docs) — a user can manage a guild's bot config if either is
# set, same check /automod dashboard's _require_perm("manage_guild") makes
# server-side for the slash command.
PERM_ADMINISTRATOR = 0x8
PERM_MANAGE_GUILD = 0x20

_initialized = False


def _redirect_to_login_error(message: str) -> str:
    return f"{DASHBOARD_BASE_URL}/login/servers?error={urlencode({'': message})[1:]}"


async def _handle(query: dict) -> tuple[int, str]:
    global _initialized
    if not _initialized:
        from init_system import initialize_system
        await initialize_system()
        _initialized = True

    code_param = query.get("code", [None])[0]
    state = query.get("state", [None])[0]
    error = query.get("error", [None])[0]
    session_param = query.get("session", [None])[0]

    # Leg 3: /login/servers fetching its own already-computed results.
    if session_param and code_param is None and state is None:
        payload = await db.get_login_session(session_param)
        if payload is None:
            return 404, json.dumps({"status": "error", "message": "That sign-in link expired. Sign in again."})
        return 200, json.dumps({"status": "ok", "guilds": payload})

    # Leg 1: fresh click from the landing page's "Sign in with Discord" button.
    if code_param is None and error is None and state is None:
        if not DISCORD_OAUTH_CLIENT_ID:
            return 200, "<h2>Sign-in isn't set up yet.</h2>"
        import secrets as _secrets
        oauth_state = _secrets.token_urlsafe(24)
        await db.create_login_oauth_state(oauth_state)
        params = {
            "client_id": DISCORD_OAUTH_CLIENT_ID,
            "redirect_uri": DISCORD_LOGIN_OAUTH_REDIRECT_URI,
            "response_type": "code",
            "scope": "identify guilds",
            "state": oauth_state,
        }
        return 302, f"{DISCORD_AUTHORIZE_URL}?{urlencode(params)}"

    if error:
        return 302, _redirect_to_login_error("Sign-in was cancelled.")

    if not state:
        return 400, "<h2>Missing sign-in state.</h2>"

    ok = await db.pop_login_oauth_state(state)
    if not ok:
        return 302, _redirect_to_login_error("That sign-in link expired. Try again.")

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
                    "redirect_uri": DISCORD_LOGIN_OAUTH_REDIRECT_URI,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            ) as token_resp:
                token_resp.raise_for_status()
                access_token = (await token_resp.json())["access_token"]

            async with session.get(
                f"{DISCORD_API_BASE}/users/@me/guilds",
                headers={"Authorization": f"Bearer {access_token}"},
            ) as guilds_resp:
                guilds_resp.raise_for_status()
                my_guilds = await guilds_resp.json()
    except (aiohttp.ClientError, asyncio.TimeoutError, KeyError, ValueError):
        logger.exception("Discord OAuth exchange failed for login")
        return 302, _redirect_to_login_error("Something went wrong signing you in.")

    # Keep only guilds this Discord user can actually manage.
    manageable = []
    for g in my_guilds:
        try:
            perms = int(g.get("permissions", 0))
        except (TypeError, ValueError):
            continue
        if g.get("owner") or perms & PERM_ADMINISTRATOR or perms & PERM_MANAGE_GUILD:
            manageable.append(g)

    if not manageable:
        session_id = await db.create_login_session([])
        return 302, f"{DASHBOARD_BASE_URL}/login/servers?session={session_id}"

    guild_ids = [int(g["id"]) for g in manageable]
    active_clones = await db.get_active_clone_for_guilds(guild_ids)

    results = []
    for g in manageable:
        gid = int(g["id"])
        if gid not in active_clones:
            continue  # PRIME-BOT isn't in this guild — nothing to link to.
        clone_id = active_clones[gid]
        token = await db.get_or_create_dashboard_token(gid, clone_id=clone_id)
        icon_url = (
            f"https://cdn.discordapp.com/icons/{gid}/{g['icon']}.png"
            if g.get("icon") else None
        )
        results.append({
            "guild_id": str(gid),
            "guild_name": g.get("name", "Unknown server"),
            "guild_icon_url": icon_url,
            "token": token,
            "clone_id": clone_id,
        })

    session_id = await db.create_login_session(results)
    return 302, f"{DASHBOARD_BASE_URL}/login/servers?session={session_id}"


class handler(BaseHTTPRequestHandler):
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)

        try:
            status, body = asyncio.run(_handle(query))
        except Exception:
            logger.exception("discord_login_oauth callback error")
            status, body = 500, json.dumps({"status": "error", "message": "Something went wrong. Please try again."})

        if status == 302:
            self.send_response(302)
            self.send_header("Location", body)
            self._cors()
            self.end_headers()
            return

        self.send_response(status)
        content_type = "application/json" if body.strip().startswith(("{", "[")) else "text/html"
        self.send_header("Content-Type", content_type)
        self._cors()
        self.end_headers()
        self.wfile.write(body.encode())
