# path: discord_bot/dm_send.py

"""
Sends a Discord DM directly via the REST API, rather than requiring a live
discord.py Client/gateway connection.

Same rationale as discord_bot/role_grant.py: api/paystack_webhook.py runs
as a stateless serverless function with no running discord.py Client in
memory to call user.send() on. Opening a DM channel and posting to it via
plain REST works from any process, live bot or webhook.

Best-effort only: a closed-DMs user or a user who shares no mutual server
with the bot are both expected, non-fatal failures — callers should log
and move on, not treat this as the only path to reconciling a payment
(the payment itself should already be durably marked paid before this is
attempted).
"""

import logging
import aiohttp

logger = logging.getLogger(__name__)

DISCORD_API_BASE = "https://discord.com/api/v10"
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=10)


async def dm_user(user_id: int, content: str, bot_token: str) -> bool:
    """Returns True on success, False otherwise. bot_token must be the
    token of a bot that shares a server (or a prior DM channel) with
    user_id — for a clone-scoped payment, pass that clone's own decrypted
    token, not the main bot's."""
    if not bot_token or not user_id:
        logger.warning("[discord] dm_user called with missing bot_token or user_id")
        return False

    headers = {"Authorization": f"Bot {bot_token}", "Content-Type": "application/json"}
    try:
        async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT) as session:
            async with session.post(
                f"{DISCORD_API_BASE}/users/@me/channels",
                headers=headers, json={"recipient_id": user_id}
            ) as resp:
                if resp.status not in (200, 201):
                    body = await resp.text()
                    logger.warning(f"[discord] Couldn't open DM with {user_id}: HTTP {resp.status} {body}")
                    return False
                channel = await resp.json()
                channel_id = channel["id"]

            async with session.post(
                f"{DISCORD_API_BASE}/channels/{channel_id}/messages",
                headers=headers, json={"content": content}
            ) as resp:
                if resp.status in (200, 201):
                    return True
                body = await resp.text()
                logger.warning(f"[discord] Couldn't DM {user_id}: HTTP {resp.status} {body}")
                return False
    except (aiohttp.ClientError, TimeoutError) as e:
        logger.warning(f"[discord] Network error DMing {user_id}: {e}")
        return False


async def dm_user_with_buttons(user_id: int, content: str, buttons: list[dict], bot_token: str) -> bool:
    """Same as dm_user() but attaches an action row of link/custom_id
    buttons, sent as a raw REST message components payload — needed
    because callers like api/selar_submit.py run in the serverless/
    Railway API process (no live discord.py Client/gateway connection to
    build a discord.ui.View on), same constraint as plain dm_user().

    Buttons whose custom_id matches a discord.ui.DynamicItem template
    registered in the live bot process (discord_bot/bot.py's
    add_dynamic_items) work exactly like any other persistent button once
    posted this way — Discord routes the click to whichever process is
    connected to the gateway, which reconstructs the item from custom_id
    alone, never from anything this process held in memory.

    Each dict in `buttons`: {"label": str, "style": int, "custom_id": str}
    (style 1=primary, 2=secondary, 3=success, 4=danger) or
    {"label": str, "style": 5, "url": str} for a link button."""
    if not bot_token or not user_id:
        logger.warning("[discord] dm_user_with_buttons called with missing bot_token or user_id")
        return False

    headers = {"Authorization": f"Bot {bot_token}", "Content-Type": "application/json"}
    components = [{"type": 1, "components": [
        {"type": 2, "style": b["style"], "label": b["label"],
         **({"custom_id": b["custom_id"]} if "custom_id" in b else {}),
         **({"url": b["url"]} if "url" in b else {})}
        for b in buttons
    ]}]
    try:
        async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT) as session:
            async with session.post(
                f"{DISCORD_API_BASE}/users/@me/channels",
                headers=headers, json={"recipient_id": user_id}
            ) as resp:
                if resp.status not in (200, 201):
                    body = await resp.text()
                    logger.warning(f"[discord] Couldn't open DM with {user_id}: HTTP {resp.status} {body}")
                    return False
                channel = await resp.json()
                channel_id = channel["id"]

            async with session.post(
                f"{DISCORD_API_BASE}/channels/{channel_id}/messages",
                headers=headers, json={"content": content, "components": components}
            ) as resp:
                if resp.status in (200, 201):
                    return True
                body = await resp.text()
                logger.warning(f"[discord] Couldn't DM {user_id} with buttons: HTTP {resp.status} {body}")
                return False
    except (aiohttp.ClientError, TimeoutError) as e:
        logger.warning(f"[discord] Network error DMing {user_id} with buttons: {e}")
        return False
