"""
Vote webhook — real verification for economy.py's /vote bonus (spec §4 open
question #1's "vote-gated bonus" interpretation).

/vote itself stays honor-system (a member can run it and see the bonus grant
without us ever confirming they actually clicked vote) — that gap is closed
here instead: top.gg and discordbotlist.com both POST to a webhook URL you
register on the bot's listing page every time someone votes, carrying an
Authorization header equal to a secret you set on that same page
(config.TOPGG_WEBHOOK_AUTH). Point the listing site at
<PUBLIC_BASE_URL>/api/vote_webhook and this becomes the verified path;
/vote in economy.py is left as-is since it's still a reasonable fallback for
listing sites this endpoint doesn't yet special-case.

Both top.gg and discordbotlist.com use materially the same shape:
  {"bot": "<bot_user_id>", "user": "<voter_user_id>", "type": "upvote", ...}
(discordbotlist.com nests under different keys in places — see _extract_ids
below for the small amount of normalization needed.) We deliberately don't
hard-fail on unrecognized extra fields; we only require bot id + user id.

Not guild-specific by design — see database.py's grant_vote_bonus_for_voter
docstring for how that's handled (credited in every guild the voter has
already touched the economy in, for the bot they voted for).
"""

import json
import logging
from http.server import BaseHTTPRequestHandler

import config
from database import db

logger = logging.getLogger(__name__)


def _extract_ids(payload: dict):
    """Returns (bot_user_id, voter_user_id) as ints, or (None, None) if the
    payload doesn't look like a vote event we recognize. Tries top.gg's flat
    shape first, then discordbotlist.com's."""
    bot_id = payload.get("bot") or payload.get("botID") or payload.get("id")
    user_id = payload.get("user") or payload.get("userID")
    if bot_id is None or user_id is None:
        return None, None
    try:
        return int(bot_id), int(user_id)
    except (TypeError, ValueError):
        return None, None


class VoteRejected(Exception):
    """Raised by process_vote for any client-error case; .status carries the
    HTTP status the caller should send. Kept separate from process_vote's
    return value (rather than encoding status in the return dict) so
    do_POST's error handling is a single except clause instead of a chain of
    `if result.get("error")` checks."""
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


async def process_vote(payload: dict, auth_header: str) -> dict:
    """Pure async core of the webhook, factored out of do_POST so it's
    testable without spinning up BaseHTTPRequestHandler I/O. Returns the
    JSON-able success body; raises VoteRejected for anything that isn't a
    200. Ignored-but-not-an-error cases (unrecognized vote `type`) are
    returned as a normal dict with status "ignored", same as do_POST always
    returned 200 for them."""
    if not config.TOPGG_WEBHOOK_AUTH:
        logger.warning("[v0] vote_webhook: rejected — TOPGG_WEBHOOK_AUTH is not configured")
        raise VoteRejected(401, "Vote webhook is not configured")

    if auth_header != config.TOPGG_WEBHOOK_AUTH:
        logger.warning("[v0] vote_webhook: rejected — bad Authorization header")
        raise VoteRejected(401, "Unauthorized")

    bot_id, voter_id = _extract_ids(payload)
    if bot_id is None or voter_id is None:
        raise VoteRejected(400, "Payload missing bot/user id")

    vote_type = payload.get("type", "upvote")
    if vote_type not in ("upvote", "vote", None):
        # top.gg also fires a "test" webhook type from its dashboard and
        # discordbotlist sends other event types on the same URL in some
        # setups — 200 them without granting anything so the listing site
        # doesn't treat a legitimate "unsupported event" as a delivery
        # failure and start retrying/disabling the webhook.
        return {"status": "ignored", "reason": f"unhandled type '{vote_type}'"}

    if bot_id == config.DISCORD_BOT_USER_ID:
        clone_id = None
    else:
        clone_id = await db.resolve_clone_id_by_bot_user_id(bot_id)
        if clone_id is None:
            logger.warning(f"[v0] vote_webhook: bot id {bot_id} doesn't match main bot or any known clone")
            raise VoteRejected(404, "Unknown bot id")

    credited = await db.grant_vote_bonus_for_voter(voter_id, clone_id)
    logger.info(f"[v0] vote_webhook: user {voter_id} voted for bot {bot_id} — credited in {len(credited)} guild(s)")
    return {"status": "ok", "credited_guilds": len(credited)}


class handler(BaseHTTPRequestHandler):

    def do_POST(self):
        import asyncio

        length = int(self.headers.get("Content-Length", 0))
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._json(400, {"status": "error", "message": "Invalid JSON body"})
            return

        try:
            result = asyncio.run(process_vote(payload, self.headers.get("Authorization", "")))
        except VoteRejected as e:
            self._json(e.status, {"status": "error", "message": e.message})
            return
        except Exception as e:
            logger.error(f"[v0] vote_webhook processing error: {e}")
            self._json(500, {"status": "error", "message": "Internal error"})
            return

        self._json(200, result)

    def _json(self, status: int, payload: dict):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode())
