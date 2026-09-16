# FULL PATH: PRIME-BOT-main/api/discord_login_oauth.py

"""
"Sign in with Discord" — identity only, used by the /unlock flow.

There's still no user-account system in this repo (see discord_dashboard.py's
docstring) — this endpoint doesn't add one. It's just enough of Discord's
OAuth2 "identify" scope to know which Discord account is confirming a
payment on /unlock.

The old guild-listing/dashboard-token-picker half of this endpoint (the
"identify guilds" scope, the manageable-guilds loop, listing tokens,
/login/servers) was dropped along with the server-listing directory — see
git history if any of that needs resurrecting. What's left is three legs,
all on this one path:

Leg 1 (no ?code, no ?session): GET /api/discord_login_oauth
  -> mint a one-time oauth state, redirect into Discord's consent screen.
  Accepts an optional ?return_to (validated below) so callers like /unlock
  can get bounced back to themselves afterward.

Leg 2 (callback, ?code&?state): exchange code for identity only, stash the
  result as a session row, redirect back to return_to (or the dashboard
  site's home) with ?session=<id> appended.

Leg 3 (?session only): a page that isn't allowed to talk to Discord's token
  endpoint itself (that needs the client secret) calls back here to read
  the session row this handler already computed. Returns JSON, not a
  redirect.

Same trust rule as everywhere else in this codebase: the signed-in user's
id is ALWAYS whatever Discord's own API handed back after the token
exchange, never anything read from the query string.

Leg 4 (DELETE ?session=...): real sign-out. There's still no cookie/auth
system here (see above) — "signed in" just means "holding a session id
that resolves to a row in discord_login_sessions" — so signing out means
deleting that row server-side, not merely dropping the id client-side.
Same session id reused after a DELETE behaves exactly like an expired one.
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
    UNLOCK_PAGES_BASE_URL,
)
from database import db

logger = logging.getLogger(__name__)

DISCORD_AUTHORIZE_URL = "https://discord.com/api/oauth2/authorize"
DISCORD_TOKEN_URL = "https://discord.com/api/oauth2/token"
DISCORD_API_BASE = "https://discord.com/api/v10"

_initialized = False


def _redirect_to_login_error(message: str) -> str:
    return f"{DASHBOARD_BASE_URL}/?error={urlencode({'': message})[1:]}"


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
    return_to_param = query.get("return_to", [None])[0]

    # Leg 3: a caller (e.g. /unlock) fetching its own already-computed result.
    if session_param and code_param is None and state is None:
        payload = await db.get_login_session(session_param)
        if payload is None:
            return 404, json.dumps({"status": "error", "message": "That sign-in link expired. Sign in again."})
        return 200, json.dumps({
            "status": "ok",
            "user": payload.get("user"),
        })

    # Leg 1: fresh click from a "Sign in with Discord" link.
    if code_param is None and error is None and state is None:
        if not DISCORD_OAUTH_CLIENT_ID:
            return 200, "<h2>Sign-in isn't set up yet.</h2>"
        import secrets as _secrets
        oauth_state = _secrets.token_urlsafe(24)
        # Only accept an in-app relative path (starts with '/', no '://')
        # as return_to — never redirect somewhere this handler didn't
        # already decide on itself, since a caller-supplied full URL here
        # would be an open redirect.
        safe_return_to = (
            return_to_param
            if return_to_param and return_to_param.startswith("/") and "://" not in return_to_param
            else None
        )
        await db.create_login_oauth_state(oauth_state, return_to=safe_return_to)
        params = {
            "client_id": DISCORD_OAUTH_CLIENT_ID,
            "redirect_uri": DISCORD_LOGIN_OAUTH_REDIRECT_URI,
            "response_type": "code",
            "scope": "identify",
            "state": oauth_state,
        }
        return 302, f"{DISCORD_AUTHORIZE_URL}?{urlencode(params)}"

    if error:
        return 302, _redirect_to_login_error("Sign-in was cancelled.")

    if not state:
        return 400, "<h2>Missing sign-in state.</h2>"

    popped_state = await db.pop_login_oauth_state(state)
    if not popped_state:
        return 302, _redirect_to_login_error("That sign-in link expired. Try again.")
    return_to = popped_state.get("return_to")

    def _session_redirect(session_id: str) -> str:
        # return_to is already restricted (above, at state-creation time)
        # to an in-app-relative path — never an arbitrary external origin.
        # /unlock specifically is additionally allowed to resolve against
        # UNLOCK_PAGES_BASE_URL instead of DASHBOARD_BASE_URL, since it may
        # be hosted standalone (e.g. on GitHub Pages). This is still not a
        # general open redirect: the base is one fixed, server-configured
        # value, never anything derived from the request itself.
        base = DASHBOARD_BASE_URL
        if return_to and return_to.startswith("/unlock") and UNLOCK_PAGES_BASE_URL:
            base = UNLOCK_PAGES_BASE_URL
        if return_to:
            sep = "&" if "?" in return_to else "?"
            return f"{base}{return_to}{sep}session={session_id}"
        return f"{base}/?session={session_id}"

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
                f"{DISCORD_API_BASE}/users/@me",
                headers={"Authorization": f"Bearer {access_token}"},
            ) as me_resp:
                me_resp.raise_for_status()
                me = await me_resp.json()
    except (aiohttp.ClientError, asyncio.TimeoutError, KeyError, ValueError):
        logger.exception("Discord OAuth exchange failed for login")
        return 302, _redirect_to_login_error("Something went wrong signing you in.")

    user_id = me.get("id")
    if me.get("avatar"):
        avatar_url = f"https://cdn.discordapp.com/avatars/{user_id}/{me['avatar']}.png?size=64"
    else:
        # No custom avatar set -> Discord's own default avatar, indexed
        # off the user id (new username system) same as Discord's client does.
        try:
            default_index = (int(user_id) >> 22) % 6
        except (TypeError, ValueError):
            default_index = 0
        avatar_url = f"https://cdn.discordapp.com/embed/avatars/{default_index}.png"
    user_info = {
        "id": user_id,
        "username": me.get("global_name") or me.get("username") or "Discord user",
        "avatar_url": avatar_url,
    }

    session_id = await db.create_login_session({"user": user_info})
    return 302, _session_redirect(session_id)


async def _handle_delete(query: dict) -> tuple[int, str]:
    global _initialized
    if not _initialized:
        from init_system import initialize_system
        await initialize_system()
        _initialized = True

    session_param = query.get("session", [None])[0]
    if not session_param:
        return 400, json.dumps({"status": "error", "message": "Missing session."})

    deleted = await db.delete_login_session(session_param)
    return 200, json.dumps({"status": "ok", "signed_out": deleted})


class handler(BaseHTTPRequestHandler):
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, DELETE, OPTIONS")

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_DELETE(self):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)

        try:
            status, body = asyncio.run(_handle_delete(query))
        except Exception:
            logger.exception("discord_login_oauth sign-out error")
            status, body = 500, json.dumps({"status": "error", "message": "Something went wrong. Please try again."})

        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self._cors()
        self.end_headers()
        self.wfile.write(body.encode())

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
