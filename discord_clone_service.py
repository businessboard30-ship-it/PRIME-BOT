"""
Discord bot-cloning support — equivalent of clone_service.py, but for
Discord tokens instead of Telegram ones.

Key architectural difference from the Telegram side (see
discord_bot/clone_manager.py for the full explanation): validating a token
here does NOT make the clone live. It only proves the token is real and
records enough metadata (bot user id/username, application id for the
invite link) to let clone_manager.py spawn a gateway process for it.

Every function here is a thin, honestly-labeled wrapper around one Discord
REST API call, following the same pattern as clone_service.py's `_call`.
"""
import logging
from typing import Dict, Any

import aiohttp

logger = logging.getLogger(__name__)

DISCORD_API_BASE = "https://discord.com/api/v10"
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=10)

# Full permission set covering every feature this codebase actually ships
# (moderation, automod, roles, welcome cards, announcements, reactions) —
# NOT just the premium-role minimum. A clone owner can still narrow this
# down from Discord's own invite-permission picker if they don't want the
# clone to have moderation powers in their server; this is just a complete,
# working-out-of-the-box default instead of one that silently breaks half
# the bot's commands with "Missing Permissions".
DEFAULT_INVITE_PERMISSIONS = (
    2       # KICK_MEMBERS
    | 4     # BAN_MEMBERS
    | 16    # MANAGE_CHANNELS
    | 64    # ADD_REACTIONS
    | 1024  # VIEW_CHANNEL
    | 2048  # SEND_MESSAGES
    | 8192  # MANAGE_MESSAGES
    | 16384  # EMBED_LINKS
    | 32768  # ATTACH_FILES
    | 65536  # READ_MESSAGE_HISTORY
    | 131072  # MENTION_EVERYONE (used by /announce)
    | 262144  # USE_EXTERNAL_EMOJIS
    | 268435456  # MANAGE_ROLES
    | 536870912  # MANAGE_WEBHOOKS
    | 1099511627776  # MODERATE_MEMBERS (timeout)
)


async def _get(token: str, path: str) -> Dict[str, Any]:
    """Raw authenticated GET against the Discord API. Always returns a dict
    with at least {"ok": bool} — never raises; network/HTTP failures fold
    into {"ok": False, "error": "..."} so callers can handle them uniformly
    (same contract as clone_service.py's _call)."""
    url = f"{DISCORD_API_BASE}{path}"
    headers = {"Authorization": f"Bot {token}"}
    try:
        async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT) as session:
            async with session.get(url, headers=headers) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    return {"ok": False, "error": f"HTTP {resp.status}: {body[:200]}"}
                data = await resp.json()
                return {"ok": True, "result": data}
    except (aiohttp.ClientError, TimeoutError) as e:
        return {"ok": False, "error": f"Network error calling Discord API: {e}"}


async def _mutate(method: str, token: str, path: str, json_body: Dict[str, Any]) -> Dict[str, Any]:
    """Raw authenticated PATCH/PUT against the Discord API — same
    ok/error/result contract as _get. Used by modules/discord_bot_manager.py
    for the BotFather-style edit commands (username, application
    description, slash-command sync)."""
    url = f"{DISCORD_API_BASE}{path}"
    headers = {"Authorization": f"Bot {token}"}
    try:
        async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT) as session:
            async with session.request(method, url, headers=headers, json=json_body) as resp:
                if resp.status not in (200, 201):
                    body = await resp.text()
                    return {"ok": False, "error": f"HTTP {resp.status}: {body[:200]}"}
                data = await resp.json()
                return {"ok": True, "result": data}
    except (aiohttp.ClientError, TimeoutError) as e:
        return {"ok": False, "error": f"Network error calling Discord API: {e}"}


async def validate_bot_token(token: str) -> Dict[str, Any]:
    """
    Validate a pasted Discord bot token by calling GET /users/@me (identifies
    the bot itself) and GET /oauth2/applications/@me (needed for the invite
    link's client_id).

    Returns:
        {"ok": True, "bot_user_id": ..., "bot_username": "...", "application_id": ...}
        {"ok": False, "error": "..."} on failure (bad token, revoked bot, network issue)
    """
    token = (token or "").strip()
    # A Discord bot token has two base64-ish segments separated by dots,
    # commonly starting with a bot-account-id-encoded prefix — the only
    # cheap sanity check worth doing client-side before spending a request.
    if not token or "." not in token:
        return {"ok": False, "error": "That doesn't look like a Discord bot token."}

    me = await _get(token, "/users/@me")
    if not me.get("ok"):
        return {"ok": False, "error": me.get("error", "Discord rejected this token.")}

    info = me["result"]
    if not info.get("bot"):
        return {"ok": False, "error": "That token belongs to a user account, not a bot."}

    app = await _get(token, "/oauth2/applications/@me")
    if not app.get("ok"):
        return {"ok": False, "error": app.get("error", "Could not fetch this bot's application info.")}

    return {
        "ok": True,
        "bot_user_id": int(info["id"]),
        "bot_username": info.get("username"),
        "application_id": int(app["result"]["id"]),
    }


async def set_default_install_params(
    token: str, permissions: int = DEFAULT_INVITE_PERMISSIONS
) -> Dict[str, Any]:
    """Configure this application's Default Install Settings (Guild Install)
    via PATCH /applications/@me, so Discord's own App Directory / Discover
    "Add to Server" button works correctly without the owner ever touching
    the Developer Portal.

    Why this matters: Discover's "Add to Server" button builds its invite
    from install_params.scopes. If that list is missing "bot" (empty/unset
    by default for a freshly-created application, or set by the owner to
    just ["applications.commands"]), Discord will authorize the app and
    register its slash commands WITHOUT ever adding the bot as a guild
    member — it looks like a successful install but the bot never shows up
    in the member list. Forcing "bot" into scopes here closes that gap for
    every clone, not just the ones we invite ourselves via build_invite_url.

    Best-effort: called right after token validation during clone
    registration. Failure here is non-fatal — the clone still works via
    build_invite_url's own hardcoded scopes, this only affects the Discover
    listing's own "Add to Server" flow — so callers should log a warning on
    {"ok": False} rather than aborting registration.
    """
    body = {
        "install_params": {
            "scopes": ["bot", "applications.commands"],
            "permissions": str(permissions),
        }
    }
    return await _mutate("PATCH", token, "/applications/@me", body)


def build_invite_url(application_id: int, permissions: int = DEFAULT_INVITE_PERMISSIONS) -> str:
    """Standard OAuth2 bot-install link — the owner opens this to add their
    newly-registered clone to whichever server(s) they run it in."""
    return (
        f"https://discord.com/api/oauth2/authorize"
        f"?client_id={application_id}&permissions={permissions}&scope=bot%20applications.commands"
    )
