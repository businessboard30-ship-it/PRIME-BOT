"""DiscordThings (dsc.sh) vote check: GET /bots/{id}/check-vote?userId=... -> {"voted", "lastVote", "nextVote"}.

Uses the same API token as the stats sync (DTHINGS_API_KEY, or the token the owner added through the DM modal).
Never raises: returns one of the status strings below so callers can decide how to fail.
"""
import logging

import aiohttp

import config

logger = logging.getLogger(__name__)

API = "https://dsc.sh/api"
TIMEOUT_SECONDS = 10

VOTED = "voted"
NOT_VOTED = "not_voted"
ERROR = "error"            # DiscordThings unreachable / refused: try again
NO_TOKEN = "no_token"      # nothing configured, so the vote can't be verified


async def _token() -> str:
    if config.DTHINGS_API_KEY:
        return config.DTHINGS_API_KEY
    try:
        from discord_bot import listing_tokens
        return await listing_tokens.load_token("dthings", int(config.DTHINGS_BOT_ID))
    except Exception:
        logger.exception("[dthings-vote] couldn't load the stored token")
        return ""


async def check_vote(user_id: int) -> str:
    token = await _token()
    if not token:
        return NO_TOKEN
    url = f"{API}/bots/{config.DTHINGS_BOT_ID}/check-vote"
    try:
        timeout = aiohttp.ClientTimeout(total=TIMEOUT_SECONDS)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, params={"userId": str(int(user_id))},
                                   headers={"Authorization": token}) as r:
                if r.status != 200:
                    logger.warning("[dthings-vote] check-vote HTTP %s", r.status)
                    return ERROR
                data = await r.json(content_type=None)
    except Exception as e:
        logger.warning("[dthings-vote] check-vote failed: %s", e)
        return ERROR
    return VOTED if isinstance(data, dict) and data.get("voted") is True else NOT_VOTED
