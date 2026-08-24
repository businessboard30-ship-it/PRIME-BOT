"""
Google Drive OAuth redirect handler.

Flow: /connect gdrive in Discord sends the user here via Google's own
consent screen -> Google redirects back to this endpoint with a one-time
code -> we exchange it for tokens, create/find the user's app-scoped
folder, and store only the encrypted tokens + folder id. No file
content ever passes through this endpoint or this app.
"""

import asyncio
import logging
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

from init_system import initialize_system
from database import db
from modules import gdrive_client

logger = logging.getLogger(__name__)

_initialized = False


async def _handle(query: dict) -> tuple[int, str]:
    global _initialized
    if not _initialized:
        await initialize_system()
        _initialized = True

    error = query.get("error", [None])[0]
    if error:
        return 400, f"<h2>Connection cancelled</h2><p>{error}</p>"

    code = query.get("code", [None])[0]
    state = query.get("state", [None])[0]
    if not code or not state:
        return 400, "<h2>Missing code or state.</h2>"

    user_id = await db.pop_gdrive_oauth_state(state)
    if not user_id:
        return 400, "<h2>This connection link expired or was already used.</h2><p>Run /connect gdrive again.</p>"

    try:
        tokens = await gdrive_client.exchange_code(code)
        if not tokens.get("refresh_token"):
            return 400, "<h2>Google didn't return a refresh token.</h2><p>Try disconnecting any prior access at myaccount.google.com/permissions and running /connect gdrive again.</p>"

        folder = await gdrive_client.get_or_create_app_folder(tokens["access_token"])
        await db.set_gdrive_connection(
            user_id, tokens["access_token"], tokens["refresh_token"], tokens["expires_at"],
            folder["id"], folder["name"],
        )
    except gdrive_client.GDriveError as e:
        logger.error(f"[v0] Google Drive connect failed for user {user_id}: {e}")
        return 500, f"<h2>Connection failed</h2><p>{e}</p>"

    return 200, (
        "<h2>✅ Google Drive connected</h2>"
        f"<p>Files you add to the <b>{folder['name']}</b> folder in your Drive will show up in "
        "<code>/movie search</code>. You can close this tab and go back to Discord.</p>"
    )


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)

        try:
            status, body = asyncio.run(_handle(query))
        except Exception as e:
            logger.error(f"[v0] oauth_gdrive callback error: {e}")
            status, body = 500, "<h2>Something went wrong. Please try again.</h2>"

        self.send_response(status)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(f"<html><body style='font-family:sans-serif;padding:2rem'>{body}</body></html>".encode())
