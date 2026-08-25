# FULL PATH: PRIME-BOT-main/api/bump_oauth.py

"""
/bump bot ownership-verification OAuth callback.

Flow: BumpBotIdModal (discord_bot/cogs/_views_bump.py) fetches the bot's
name/icon from Discord's public RPC endpoint, stashes it plus a one-time
`state` token in bump_oauth_states, and hands the user a link button to
this endpoint. Same two-leg shape as api/discover_oauth_join.py:
  1. First load (no ?code) — redirect into Discord's OAuth consent screen.
  2. Callback from Discord — exchange the code for the signed-in user's id,
     then finalize the bot listing with THAT id as verified_owner_id.

We never trust an invite URL from user input — bump_finalize_bot_listing
always derives it from the RPC-fetched application_id server-side.
"""

import asyncio
import logging
import secrets
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse, urlencode

import aiohttp

from config import DISCORD_OAUTH_CLIENT_ID, DISCORD_OAUTH_CLIENT_SECRET, BUMP_OAUTH_REDIRECT_URI, BOT_TOKEN, DISCORD_CLONE_ADMIN_IDS
from database import db

logger = logging.getLogger(__name__)

DISCORD_AUTHORIZE_URL = "https://discord.com/api/oauth2/authorize"
DISCORD_TOKEN_URL = "https://discord.com/api/oauth2/token"
DISCORD_API_BASE = "https://discord.com/api/v10"

_initialized = False


def _notify_admins_of_pending(listing: dict, icon_url: str | None) -> None:
    """Schedules a best-effort DM to bot owners so a pending submission
    doesn't just sit invisible in the database — this runs in
    api_server.py's process, which has no live gateway session, so it hits
    Discord's REST API directly with the bot token rather than going
    through discord.py.

    Fire-and-forget via asyncio.create_task rather than `await`ed inline:
    api_server.py now runs every request's coroutine on ONE shared event
    loop (see its module docstring), so a blocking call here would freeze
    every other in-flight request on the server, not just this one — this
    used to be `requests` (synchronous) for exactly that reason it never
    mattered before each request got its own throwaway loop/thread. Now it
    matters a lot, hence aiohttp + create_task instead of blocking the
    caller on possibly-slow DMs to every admin."""
    asyncio.create_task(_notify_admins_of_pending_async(listing, icon_url))


async def _notify_admins_of_pending_async(listing: dict, icon_url: str | None) -> None:
    """Sent as a full card (bot info) with Approve/Reject buttons rather
    than a plain-text nudge, so an admin can act right from the DM
    without first running /bumpadmin review. The buttons use static
    custom_ids (bump:approve:<id> / bump:reject:<id>) matched by
    DynamicBumpApproveButton/DynamicBumpRejectButton in
    discord_bot/cogs/bump.py — since this message is built and sent
    entirely over raw REST with no live discord.py View object, only a
    statically-registered DynamicItem in the gateway process (bot.py)
    can ever handle a click on it, restart or not.

    Runs as a background task (see _notify_admins_of_pending above), each
    admin's DM capped at a short 4s timeout so one slow/hanging admin
    can't hold this up indefinitely."""
    if not BOT_TOKEN or BOT_TOKEN == "your_token_here":
        return
    headers = {"Authorization": f"Bot {BOT_TOKEN}", "Content-Type": "application/json"}

    embed = {
        "title": f"🤖 {listing['name']}",
        "description": listing.get("description") or "*no description*",
        "color": 0xF1C40F,
        "fields": [
            {"name": "Submitted by", "value": f"<@{listing['verified_owner_id']}> (OAuth-verified)", "inline": False},
            {"name": "Owning server", "value": f"Guild ID: {listing['guild_id']}", "inline": True},
            {"name": "Application ID", "value": str(listing["application_id"]), "inline": True},
            {"name": "Invite", "value": listing["invite_url"], "inline": False},
        ],
    }
    if icon_url:
        embed["thumbnail"] = {"url": icon_url}

    components = [{
        "type": 1,
        "components": [
            {"type": 2, "style": 3, "label": "Approve", "emoji": {"name": "✅"}, "custom_id": f"bump:approve:{listing['id']}"},
            {"type": 2, "style": 4, "label": "Reject", "emoji": {"name": "🚫"}, "custom_id": f"bump:reject:{listing['id']}"},
        ],
    }]

    timeout = aiohttp.ClientTimeout(total=4)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for admin_id in DISCORD_CLONE_ADMIN_IDS:
            try:
                async with session.post(
                    f"{DISCORD_API_BASE}/users/@me/channels", headers=headers,
                    json={"recipient_id": admin_id},
                ) as dm:
                    dm.raise_for_status()
                    channel_id = (await dm.json())["id"]
                async with session.post(
                    f"{DISCORD_API_BASE}/channels/{channel_id}/messages", headers=headers,
                    json={"embeds": [embed], "components": components},
                ) as resp:
                    resp.raise_for_status()
            except (aiohttp.ClientError, asyncio.TimeoutError):
                logger.warning("Failed to DM admin %s about pending bump listing %s", admin_id, listing["id"])


def _page(body: str) -> bytes:
    return f"""<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; min-height: 100vh; display: flex; align-items: center; justify-content: center;
    background: radial-gradient(circle at 30% 20%, #1a1a1a 0%, #000000 60%);
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    color: #f2f2f2; padding: 24px;
  }}
  .card {{
    max-width: 460px; width: 100%; background: linear-gradient(160deg, #16161a 0%, #0a0a0c 100%);
    border: 1px solid #2a2a30; border-radius: 18px; padding: 36px 32px; text-align: center;
    box-shadow: 0 20px 60px rgba(0,0,0,0.6);
  }}
  .icon-wrap {{
    width: 88px; height: 88px; border-radius: 50%; margin: 0 auto 20px;
    background: linear-gradient(145deg, #2a2a30, #0e0e10); display: flex; align-items: center;
    justify-content: center; overflow: hidden; border: 2px solid #3a3a42;
  }}
  .icon-wrap img {{ width: 100%; height: 100%; object-fit: cover; }}
  .badge {{
    font-size: 40px; line-height: 1;
  }}
  h1 {{ font-size: 20px; margin: 0 0 6px; letter-spacing: 0.2px; }}
  h1 .brand {{ color: #b9b9c2; font-weight: 600; }}
  p {{ font-size: 14px; color: #a8a8b3; line-height: 1.6; margin: 6px 0; }}
  .status-pill {{
    display: inline-block; margin-top: 14px; padding: 6px 14px; border-radius: 999px;
    font-size: 12px; font-weight: 600; letter-spacing: 0.3px;
  }}
  .pill-approved {{ background: rgba(59, 201, 120, 0.15); color: #4ade80; border: 1px solid rgba(74,222,128,0.35); }}
  .pill-pending {{ background: rgba(250, 204, 21, 0.12); color: #facc15; border: 1px solid rgba(250,204,21,0.35); }}
  .pill-error {{ background: rgba(248, 113, 113, 0.12); color: #f87171; border: 1px solid rgba(248,113,113,0.35); }}
  .footer {{ margin-top: 22px; font-size: 11px; color: #55555f; text-transform: uppercase; letter-spacing: 1.5px; }}
  .footer b {{ color: #8a8aef; }}
</style>
</head>
<body>
  <div class="card">
    {body}
    <div class="footer">Verified via <b>Prime Bot</b> Network</div>
  </div>
</body>
</html>""".encode("utf-8")


def _card(icon_url: str | None, title: str, lines: list[str], pill: str | None = None) -> str:
    icon_html = f'<img src="{icon_url}" alt="">' if icon_url else '<span class="badge">🤖</span>'
    body = f'<div class="icon-wrap">{icon_html}</div><h1>{title}</h1>'
    body += "".join(f"<p>{line}</p>" for line in lines)
    if pill:
        cls, label = pill
        body += f'<div class="status-pill {cls}">{label}</div>'
    return body


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
        return 400, _card(None, "Link error", ["Missing verification link parameters."], ("pill-error", "ERROR"))

    if code_param is None and error is None:
        # Leg 1 — state here is the token BumpBotIdModal minted. Look it up
        # just to confirm it hasn't already expired/been used before we
        # bother sending the user to Discord's consent screen.
        pending = await db.pop_bump_oauth_state(state)
        if not pending:
            return 404, _card(None, "Link expired", [
                "This verification link is invalid or has expired.",
                "Go back to Discord and run <b>/bump bot</b> again.",
            ], ("pill-error", "EXPIRED"))

        # Re-mint under a fresh token bound to Discord's own OAuth round
        # trip, carrying the same payload — pop above already consumed the
        # original so a replay of leg 1 can't be reused.
        oauth_state = secrets.token_urlsafe(24)
        await db.create_bump_oauth_state(
            oauth_state, pending["guild_id"], pending["clone_id"], pending["invoker_id"],
            pending["application_id"], pending["bot_name"], pending["bot_icon_url"], pending["description"],
            pending["tags"], pending["existing_listing_id"],
        )
        params = {
            "client_id": DISCORD_OAUTH_CLIENT_ID,
            "redirect_uri": BUMP_OAUTH_REDIRECT_URI,
            "response_type": "code",
            "scope": "identify",
            "state": oauth_state,
        }
        return 302, f"{DISCORD_AUTHORIZE_URL}?{urlencode(params)}"

    if error:
        return 200, _card(None, "Sign-in cancelled", [
            "No worries — nothing was submitted.",
            "Go back to Discord and run <b>/bump bot</b> again if you want to retry.",
        ], ("pill-pending", "CANCELLED"))

    # Leg 2 — callback from Discord.
    pending = await db.pop_bump_oauth_state(state)
    if not pending:
        return 400, _card(None, "Link already used", [
            "This verification link expired or was already used.",
            "Go back to Discord and run <b>/bump bot</b> again.",
        ], ("pill-error", "EXPIRED"))

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
                    "redirect_uri": BUMP_OAUTH_REDIRECT_URI,
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
                verified_user_id = int((await identity_resp.json())["id"])
    except (aiohttp.ClientError, asyncio.TimeoutError, KeyError, ValueError):
        logger.exception("bump_oauth exchange failed for application %s", pending["application_id"])
        return 500, _card(pending.get("bot_icon_url"), "Sign-in failed", [
            "Something went wrong signing you in.",
            "Go back to Discord and run <b>/bump bot</b> again.",
        ], ("pill-error", "ERROR"))

    listing = await db.bump_finalize_bot_listing(
        guild_id=pending["guild_id"], clone_id=pending["clone_id"], created_by=pending["invoker_id"],
        application_id=pending["application_id"], name=pending["bot_name"], description=pending["description"],
        tags=pending["tags"], verified_owner_id=verified_user_id, listing_id=pending["existing_listing_id"],
    )

    if listing["status"] == "pending":
        _notify_admins_of_pending(listing, pending.get("bot_icon_url"))
        pill = ("pill-pending", "PENDING REVIEW")
        lines = [
            "Signed in and verified — nice work.",
            "It's queued for a quick review before it starts going out in bumps.",
            "You can close this tab and head back to Discord.",
        ]
    else:
        pill = ("pill-approved", "LIVE")
        lines = [
            "Signed in, verified, and live on the network.",
            "You can close this tab and head back to Discord.",
        ]

    return 200, _card(pending.get("bot_icon_url"), f"{pending['bot_name']} is submitted!", lines, pill)


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)

        try:
            status, body = asyncio.run(_handle(query))
        except Exception:
            logger.exception("bump_oauth callback error")
            status, body = 500, _card(None, "Something went wrong", ["Please go back to Discord and try again."], ("pill-error", "ERROR"))

        if status == 302:
            self.send_response(302)
            self.send_header("Location", body)
            self.end_headers()
            return

        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(_page(body))

    def log_message(self, fmt, *args):
        pass
