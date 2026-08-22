"""
Grants a Discord role directly via the REST API (PUT guild member role),
rather than requiring a live discord.py Client/gateway connection.

Why this exists: api/paystack_webhook.py runs as a stateless serverless
function (see its BaseHTTPRequestHandler pattern) — it does NOT have a
running discord.py Client sitting in memory to call member.add_roles() on.
Calling Discord's REST API directly with the bot token works from any
process, live bot or webhook, and is idempotent (PUTting a role a user
already has is a harmless no-op on Discord's side).
"""

import logging
import aiohttp
from urllib.parse import quote

from config import DISCORD_BOT_TOKEN

logger = logging.getLogger(__name__)

DISCORD_API_BASE = "https://discord.com/api/v10"
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=10)


async def grant_role(guild_id: int, user_id: int, role_id: int, reason: str = "", bot_token: str = None) -> bool:
    """Idempotent: adding a role a member already has succeeds (204) and
    changes nothing. Returns True on success (204), False otherwise —
    callers should log but not raise, since this is a best-effort grant
    (e.g. from the webhook, where the user's on_member_join handler is a
    second, independent path to the same end state).

    bot_token defaults to the main bot's DISCORD_BOT_TOKEN, but callers
    granting a role in a guild served by a CLONE must pass that clone's own
    token instead — the main bot is not necessarily even a member of a
    clone's guild, so its token can't grant roles there."""
    token = bot_token or DISCORD_BOT_TOKEN
    if not token:
        logger.warning("[discord] No bot token available — cannot grant role")
        return False
    if not guild_id or not user_id or not role_id:
        logger.warning(
            f"[discord] grant_role called with missing id(s): "
            f"guild_id={guild_id} user_id={user_id} role_id={role_id}"
        )
        return False

    url = f"{DISCORD_API_BASE}/guilds/{guild_id}/members/{user_id}/roles/{role_id}"
    headers = {
        "Authorization": f"Bot {token}",
        # Discord requires this header value to be ASCII/percent-encoded —
        # `reason` can contain arbitrary text (e.g. a /verify admin's typed
        # reason), so encode it rather than sending raw and risking aiohttp
        # rejecting the request outright on a non-ASCII reason.
        "X-Audit-Log-Reason": quote(reason or "premium_group_join payment verified"),
    }

    try:
        async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT) as session:
            async with session.put(url, headers=headers) as resp:
                if resp.status == 204:
                    logger.info(f"[discord] Granted role {role_id} to user {user_id} in guild {guild_id}")
                    return True
                body = await resp.text()
                logger.warning(
                    f"[discord] Failed to grant role {role_id} to user {user_id} in guild {guild_id}: "
                    f"HTTP {resp.status} {body}"
                )
                return False
    except (aiohttp.ClientError, TimeoutError) as e:
        # Network/timeout failure talking to Discord — not fatal here, this
        # is always a best-effort grant with an independent backstop
        # (on_member_join or a manual /verify), so log and move on rather
        # than letting this bubble up and disrupt webhook processing.
        logger.warning(f"[discord] Network error granting role {role_id} to user {user_id} in guild {guild_id}: {e}")
        return False
