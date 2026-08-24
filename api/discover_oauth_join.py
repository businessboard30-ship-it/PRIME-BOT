"""
Discover Players — OAuth2 "click-to-join" invite landing page.

Flow: /discoverplayers invite generates a link to this endpoint with
?state=<one-time token>, where the state is bound to a category_id (see
database.create_discover_oauth_state). With no ?code yet, we redirect the
visitor into Discord's own OAuth2 consent screen (identify scope only — we
never ask for anything beyond "who is this"). Discord redirects back here
with a one-time code; we exchange it for the user's Discord id and join
them to that category server-side, same cap/already-a-member checks as the
/discoverplayers join slash command use (join_discover_category is the single
shared code path for both).

The slash-command fallback (/discoverplayers join code:<code>) always keeps
working even if OAuth isn't configured (DISCORD_OAUTH_CLIENT_ID unset) or
the visitor declines Discord's consent screen — this page just degrades to
telling them to use it.
"""

import asyncio
import logging
import secrets
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, urlencode

import requests

from config import DISCORD_OAUTH_CLIENT_ID, DISCORD_OAUTH_CLIENT_SECRET, DISCORD_OAUTH_REDIRECT_URI
from database import db

logger = logging.getLogger(__name__)

DISCORD_AUTHORIZE_URL = "https://discord.com/api/oauth2/authorize"
DISCORD_TOKEN_URL = "https://discord.com/api/oauth2/token"
DISCORD_API_BASE = "https://discord.com/api/v10"

_initialized = False


def _page(body: str) -> bytes:
    return f"<html><body style='font-family:sans-serif;padding:2rem;max-width:640px;margin:auto'>{body}</body></html>".encode()


def _fallback_html(code: str, reason: str) -> str:
    return (
        f"<h2>{reason}</h2>"
        f"<p>You can still join from Discord — run this command there:</p>"
        f"<p><code>/discoverplayers join code:{code}</code></p>"
    )


async def _handle(query: dict) -> tuple[int, str]:
    global _initialized
    if not _initialized:
        from init_system import initialize_system
        await initialize_system()
        _initialized = True

    code_param = query.get("code", [None])[0]
    state = query.get("state", [None])[0]
    error = query.get("error", [None])[0]

    if not state:
        return 400, "<h2>Missing invite link parameters.</h2>"

    # This endpoint is reached two different ways, both using a `state`
    # query param, so branch on which leg we're in rather than the name:
    #  1. First load, straight from the invite link — `state` is the
    #     category's own invite_code (human-shareable, stable).
    #  2. Callback from Discord after consent — `state` is the one-time
    #     OAuth token we minted in leg 1 and handed to Discord ourselves.
    if code_param is None and error is None:
        cat = await db.get_discover_category_by_code(state)
        if not cat:
            return 404, "<h2>This invite link is invalid or has expired.</h2>"
        if not DISCORD_OAUTH_CLIENT_ID:
            return 200, _fallback_html(state, "One-click join isn't set up yet.")

        oauth_state = secrets.token_urlsafe(24)
        await db.create_discover_oauth_state(oauth_state, cat["id"])
        params = {
            "client_id": DISCORD_OAUTH_CLIENT_ID,
            "redirect_uri": DISCORD_OAUTH_REDIRECT_URI,
            "response_type": "code",
            "scope": "identify",
            "state": oauth_state,
        }
        redirect_url = f"{DISCORD_AUTHORIZE_URL}?{urlencode(params)}"
        return 302, redirect_url  # caller sends this as a Location header, not a body

    if error:
        # User declined consent — state here is our OAuth token; pop it to
        # find the category so the fallback message can show the real
        # shareable invite code, not the one-time token.
        category_id = await db.pop_discover_oauth_state(state)
        cat = await db.get_discover_category(category_id) if category_id else None
        code_for_fallback = cat["invite_code"] if cat else "?"
        return 200, _fallback_html(code_for_fallback, "Sign-in was cancelled.")

    # Second leg: state here is the OAuth state we minted above.
    category_id = await db.pop_discover_oauth_state(state)
    if category_id is None:
        return 400, "<h2>This sign-in link expired or was already used. Go back to Discord and run /discoverplayers invite again.</h2>"

    cat = await db.get_discover_category(category_id)
    if not cat:
        return 404, "<h2>This category no longer exists.</h2>"

    try:
        token_resp = requests.post(
            DISCORD_TOKEN_URL,
            data={
                "client_id": DISCORD_OAUTH_CLIENT_ID,
                "client_secret": DISCORD_OAUTH_CLIENT_SECRET,
                "grant_type": "authorization_code",
                "code": code_param,
                "redirect_uri": DISCORD_OAUTH_REDIRECT_URI,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=10,
        )
        token_resp.raise_for_status()
        access_token = token_resp.json()["access_token"]

        identity_resp = requests.get(
            f"{DISCORD_API_BASE}/users/@me",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
        )
        identity_resp.raise_for_status()
        discord_user = identity_resp.json()
        user_id = int(discord_user["id"])
    except (requests.RequestException, KeyError, ValueError):
        logger.exception("Discord OAuth exchange failed for discover category %s", category_id)
        return 500, _fallback_html(cat["invite_code"], "Something went wrong signing you in.")

    result = await db.join_discover_category(category_id, user_id)
    username = discord_user.get("username", "there")

    if result == "joined":
        return 200, f"<h2>✅ You're in, {username}!</h2><p>You've joined <b>{cat['name']}</b>. Head back to Discord to browse other members.</p>"
    if result == "already_member":
        return 200, f"<h2>You're already in {cat['name']}</h2><p>No action needed — head back to Discord.</p>"
    if result == "full":
        return 200, f"<h2>{cat['name']} is full</h2><p>This category has hit its member cap. Ask the category creator to upgrade it.</p>"
    return 404, "<h2>This category no longer exists.</h2>"


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)

        try:
            status, body = asyncio.run(_handle(query))
        except Exception:
            logger.exception("discover_oauth_join callback error")
            status, body = 500, "<h2>Something went wrong. Please try again.</h2>"

        if status == 302:
            self.send_response(302)
            self.send_header("Location", body)
            self.end_headers()
            return

        self.send_response(status)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(_page(body))
