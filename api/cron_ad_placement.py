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
from modules.ad_links import split_ad_links
from utils.crypto import secret_manager

logger = logging.getLogger(__name__)

DISCORD_API_BASE = "https://discord.com/api/v10"
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=10)

# Re-post each active ad into a bump channel at most once per window —
# run this cron on any schedule you like (every minute is fine), the
# cooldown is what actually paces it out to a 6-hourly auto-bump.
AD_PLACEMENT_COOLDOWN_SECONDS = 6 * 60 * 60

# Same teal as a server listing's card (discord_bot/cogs/bump.py's
# _bump_embed), so an ad sits in the bump channel looking like one of the
# cards around it instead of a loose block of text.
CARD_COLOR = 0x22B3A4
CARD_DESCRIPTION_MAX = 500  # same cap _bump_embed uses for listings
_CARD_FOOTER = "📢 Sponsored  •  Want your ad here? Use /ad submit or 📣 Advertise with us in your join DM"


def _ad_card(ad: dict, image_url: str | None = None) -> tuple[dict, list]:
    """(embed dict, link-button component rows) — an ad shaped like a server
    card: teal embed, company as the title, headline in bold over the body,
    hosted image, a Sponsored footer, and the link button row underneath.
    Every link in the ad becomes a button, so nothing in the text is left for
    Discord to expand into a big preview."""
    (title, description), buttons = split_ad_links(
        [ad["ad_title"], ad["ad_description"]], target_url=ad.get("target_url"),
    )
    body = "\n".join(part for part in (f"**{title}**" if title else "", description) if part)
    if len(body) > CARD_DESCRIPTION_MAX:
        body = body[:CARD_DESCRIPTION_MAX - 1].rstrip() + "…"
    embed = {
        "title": f"📢 {ad['company_name']}"[:256],
        "description": body or "\u200b",
        "color": CARD_COLOR,
        "footer": {"text": _CARD_FOOTER},
    }
    if image_url:
        embed["image"] = {"url": image_url}
    components = []
    if buttons:
        components = [{
            "type": 1,
            "components": [{"type": 2, "style": 5, "label": label, "url": url} for label, url in buttons],
        }]
    return embed, components


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


async def resolve_ad_image_url_rest(session: aiohttp.ClientSession, token: str, ad: dict) -> str | None:
    """Fresh (non-expired) URL of an ad's hosted image over plain REST. Kept
    here, not in discord_bot/, so this serverless cron doesn't import
    discord.py. Use the MAIN bot's token: it has to see the hosting channel."""
    channel_id, message_id = ad.get("image_channel_id"), ad.get("image_message_id")
    if not channel_id or not message_id:
        return None
    url = f"https://discord.com/api/v10/channels/{channel_id}/messages/{message_id}"
    try:
        async with session.get(url, headers={"Authorization": f"Bot {token}"}) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
        atts = data.get("attachments") or []
        return atts[0]["url"] if atts else None
    except (aiohttp.ClientError, TimeoutError, KeyError, ValueError):
        return None


async def _post(session: aiohttp.ClientSession, token: str, channel_id: int, embed: dict,
                components: list | None = None) -> bool:
    url = f"{DISCORD_API_BASE}/channels/{channel_id}/messages"
    headers = {"Authorization": f"Bot {token}"}
    payload = {"embeds": [embed]}
    if components:
        payload["components"] = components
    try:
        async with session.post(url, headers=headers, json=payload) as resp:
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
    # One ad per channel per run: with several approved ads due at once they'd
    # otherwise land back-to-back in every bump channel. A channel skipped here
    # is still due for the next ad on the next run (per-ad cooldowns are
    # untouched), so the ads take turns instead of stacking.
    posted_channels: set = set()

    async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT) as session:
        for ad in ads:
            targets = await db.get_unplaced_channels_for_ad(
                ad["id"], all_channels, cooldown_seconds=AD_PLACEMENT_COOLDOWN_SECONDS,
            )
            targets = [t for t in targets if t["bump_channel_id"] not in posted_channels]
            if not targets:
                continue
            # One fresh (non-expired) URL per ad per run, via the main bot's token.
            image_url = await resolve_ad_image_url_rest(session, DISCORD_BOT_TOKEN, ad) if ad.get("image_message_id") else None
            embed, components = _ad_card(ad, image_url)
            for target in targets:
                clone_id = target["clone_id"]
                if clone_id not in token_cache:
                    token_cache[clone_id] = await _token_for(clone_id)
                token = token_cache[clone_id]
                if not token:
                    failed += 1
                    continue
                ok = await _post(session, token, target["bump_channel_id"], embed, components)
                if ok:
                    posted_channels.add(target["bump_channel_id"])
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
