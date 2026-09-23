"""
OPTIONAL cron-triggered endpoint that places every currently-approved ad
(ad_submissions, status='approved' — see modules/ads_marketplace.py and
discord_bot/cogs/_views_join_dm.py's "Advertise with us" button /
discord_bot/cogs/ads_marketplace.py's /ad submit) into every clone's bump
channel, re-posting each one at most every 6 hours per ad/channel pair
(see database.py's ad_placements table and AD_PLACEMENT_COOLDOWN_SECONDS
below).

Why this crosses clone boundaries when bump.py's own listing fan-out
deliberately doesn't (see bump_guild_config's comment): a user-submitted
bump_listings row is that guild's own advertisement and has no business
appearing in a clone it was never invited to. An approved ad_submissions
row is the bot owner's own inventory — the same "your ad here" the owner
already offers via the join DM's Advertise button and /ad submit — so
every clone's bump channel is fair game for it.

Same reasoning as api/cron_discord_announcements.py for why this is a
stateless serverless endpoint rather than a loop in the gateway process:
each clone is its own independent `python -m discord_bot.bot --clone-id N`
process, and posting via Discord's REST API with each channel's own
clone token works regardless of which gateway processes are up.

Auth: header "Authorization: Bearer <CRON_SECRET>" or query param ?secret=.
Wire this to an external scheduler (Vercel Cron, cron-job.org, etc.).
Nothing calls it automatically — approved ads simply won't be placed into
bump channels until something hits this URL.
"""
import json
import asyncio
import logging
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

import aiohttp

from config import CRON_SECRET, DISCORD_BOT_TOKEN
from database import db
from modules.ads_marketplace import get_active_ads
from utils.crypto import secret_manager

logger = logging.getLogger(__name__)

DISCORD_API_BASE = "https://discord.com/api/v10"
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=10)

# Re-post each active ad into a bump channel at most once per window —
# run this cron on any schedule you like (every minute is fine), the
# cooldown is what actually paces it out to a 6-hourly auto-bump.
AD_PLACEMENT_COOLDOWN_SECONDS = 6 * 60 * 60

# Every placed ad carries this note so a reader knows this same slot is
# available to them too, and how to send anything a text field can't
# hold (video/image, extra assets) — via /feedback, which (unlike this
# modal-based flow) accepts an attachment.
_PLACEMENT_FOOTER = (
    "\n\n— Placed automatically in the combined join DM and every clone's bump "
    "channel. Want your own ad here? Use the 📣 **Advertise with us** button on "
    "your server's join DM, or `/ad submit`. Got a video, image, or other asset "
    "to include? Send it with `/feedback` (attachment field) and we'll add it."
)


def _ad_message(ad: dict) -> str:
    lines = [f"📣 **{ad['company_name']} — {ad['ad_title']}**", ad["ad_description"]]
    if ad.get("target_url") and ad["target_url"] != "N/A":
        lines.append(ad["target_url"])
    return "\n".join(lines) + _PLACEMENT_FOOTER


async def _token_for(clone_id):
    """None -> main bot's token. Otherwise decrypts the clone's own stored
    token — the main bot's token can't post into a guild only a clone is
    actually a member of."""
    if clone_id is None:
        return DISCORD_BOT_TOKEN
    clone = await db.get_discord_clone(clone_id)
    if not clone:
        logger.warning(f"[cron_ad_placement] clone_id={clone_id} not found, skipping its channels")
        return None
    return secret_manager.decrypt(clone["bot_token_encrypted"])


async def _post(session: aiohttp.ClientSession, token: str, channel_id: int, message: str) -> bool:
    url = f"{DISCORD_API_BASE}/channels/{channel_id}/messages"
    headers = {"Authorization": f"Bot {token}"}
    try:
        async with session.post(url, headers=headers, json={"content": message}) as resp:
            if resp.status in (200, 201):
                return True
            body = await resp.text()
            logger.warning(f"[cron_ad_placement] Failed to post to channel {channel_id}: HTTP {resp.status} {body}")
            return False
    except (aiohttp.ClientError, TimeoutError) as e:
        logger.warning(f"[cron_ad_placement] Network error posting to channel {channel_id}: {e}")
        return False


async def run_ad_placements() -> dict:
    ads = await get_active_ads()
    all_channels = await db.get_all_bump_channels()
    placed, failed = 0, 0

    if not ads or not all_channels:
        return {"ads": len(ads), "channels": len(all_channels), "placed": placed, "failed": failed}

    # Cache tokens per clone_id so a clone with many guilds only gets
    # decrypted once per run, not once per (ad, channel) pair.
    token_cache: dict = {}

    async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT) as session:
        for ad in ads:
            targets = await db.get_unplaced_channels_for_ad(
                ad["id"], all_channels, cooldown_seconds=AD_PLACEMENT_COOLDOWN_SECONDS,
            )
            message = _ad_message(ad)
            for target in targets:
                clone_id = target["clone_id"]
                if clone_id not in token_cache:
                    token_cache[clone_id] = await _token_for(clone_id)
                token = token_cache[clone_id]
                if not token:
                    failed += 1
                    continue
                ok = await _post(session, token, target["bump_channel_id"], message)
                if ok:
                    await db.record_ad_placement(ad["id"], target["bump_channel_id"], target["guild_id"])
                    placed += 1
                else:
                    failed += 1

    return {"ads": len(ads), "channels": len(all_channels), "placed": placed, "failed": failed}


class handler(BaseHTTPRequestHandler):

    def _authorized(self) -> bool:
        if not CRON_SECRET:
            return False
        auth_header = self.headers.get("Authorization", "")
        if auth_header == f"Bearer {CRON_SECRET}":
            return True
        query = parse_qs(urlparse(self.path).query)
        return query.get("secret", [""])[0] == CRON_SECRET

    def do_GET(self):
        self._handle()

    def do_POST(self):
        self._handle()

    def _handle(self):
        if not self._authorized():
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "error", "message": "Unauthorized"}).encode())
            return

        try:
            result = asyncio.run(run_ad_placements())
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "ok", **result}).encode())
        except Exception as e:
            logger.error(f"[v0] cron_ad_placement error: {e}")
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "error", "message": str(e)}).encode())
