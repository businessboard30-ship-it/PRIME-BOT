"""DiscordThings (dsc.sh) vote check: GET /bots/{id}/check-vote?userId=... -> {"voted", "lastVote", "nextVote"}.

Uses the same API token as the stats sync (DTHINGS_API_KEY, or the token the owner added through the DM modal).
Never raises: returns one of the status strings below so callers can decide how to fail.
"""
import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

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


def parse_vote_time(value) -> Optional[datetime]:
    """DiscordThings' lastVote as a UTC datetime. Accepts ISO-8601 text or epoch seconds/milliseconds; None if absent/unreadable."""
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        if isinstance(value, (int, float)) or (isinstance(value, str) and value.strip().replace(".", "", 1).isdigit()):
            n = float(value)
            if n <= 0:
                return None
            if n > 1e11:                      # milliseconds
                n /= 1000.0
            return datetime.fromtimestamp(n, tz=timezone.utc)
        d = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None


# dsc.sh rate limits (1/min per endpoint, 30/min per token): remember each answer for a minute and keep requests apart.
_CACHE_SECONDS = 60.0
_MIN_GAP_SECONDS = 2.0
_cache: dict = {}
_gate: Optional[asyncio.Lock] = None
_last_request_at = 0.0


async def check_vote_info(user_id: int) -> Tuple[str, Optional[datetime]]:
    """(status, when they last voted). The time is only set when status == VOTED and DiscordThings sent a readable one."""
    global _gate, _last_request_at
    now = time.monotonic()
    hit = _cache.get(int(user_id))
    if hit and now - hit[0] < _CACHE_SECONDS and hit[1] in (VOTED, NOT_VOTED):
        return hit[1], hit[2]
    token = await _token()
    if not token:
        return NO_TOKEN, None
    url = f"{API}/bots/{config.DTHINGS_BOT_ID}/check-vote"
    if _gate is None:
        _gate = asyncio.Lock()
    try:
        async with _gate:
            wait = _MIN_GAP_SECONDS - (time.monotonic() - _last_request_at)
            if wait > 0:
                await asyncio.sleep(wait)
            _last_request_at = time.monotonic()
            timeout = aiohttp.ClientTimeout(total=TIMEOUT_SECONDS)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, params={"userId": str(int(user_id))},
                                       headers={"Authorization": token}) as r:
                    if r.status != 200:
                        logger.warning("[dthings-vote] check-vote HTTP %s", r.status)
                        return ERROR, None
                    data = await r.json(content_type=None)
    except Exception as e:
        logger.warning("[dthings-vote] check-vote failed: %s", e)
        return ERROR, None
    voted = isinstance(data, dict) and data.get("voted") is True
    last = parse_vote_time(data.get("lastVote")) if voted else None
    status = VOTED if voted else NOT_VOTED
    _cache[int(user_id)] = (time.monotonic(), status, last)
    if len(_cache) > 5000:
        for k in sorted(_cache, key=lambda k: _cache[k][0])[:2500]:
            _cache.pop(k, None)
    return status, last


async def check_vote(user_id: int) -> str:
    return (await check_vote_info(user_id))[0]


async def sync_vote(user_id: int) -> Tuple[str, bool]:
    """Verify the vote with DiscordThings and record it in the shared vote table, so the XP boost, the leaderboard
    boost marker and the bump unlock all see it exactly as they saw a top.gg vote. Returns (status, newly_recorded).

    The boost runs TOPGG_VOTE_HOURS from the vote's real time (not from the claim), and each vote is recorded once,
    so pressing the button again never extends it and a vote can't unlock a second bump."""
    status, last = await check_vote_info(user_id)
    if status != VOTED:
        return status, False
    hours = float(config.TOPGG_VOTE_HOURS)
    now = datetime.now(timezone.utc)
    if last is not None:
        if last > now:                                   # clock skew: never in the future
            last = now
        if now - last >= timedelta(hours=hours):         # an old vote: nothing left to boost
            return NOT_VOTED, False
        vote_id = f"dthings:{int(last.timestamp())}"
    else:                                                # no readable time: allow one grant per boost window
        vote_id = f"dthings:window:{int(now.timestamp() // (hours * 3600))}"
    try:
        from database import db
        row = await db.record_topgg_vote(int(user_id), hours, vote_id, last)
    except Exception:
        logger.exception("[dthings-vote] couldn't record the vote for %s", user_id)
        return ERROR, False
    return VOTED, row is not None
