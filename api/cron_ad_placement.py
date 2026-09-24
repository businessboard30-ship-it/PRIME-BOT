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

After each ad lands, the channel's 🔁 Bump button is re-posted below it (see
_refresh_bump_prompt) so it's never left scrolled up behind an ad.

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
from modules.ads_marketplace import get_active_ads, get_autobump_settings, format_interval
from modules.ad_links import split_ad_links
from utils.crypto import secret_manager

logger = logging.getLogger(__name__)

DISCORD_API_BASE = "https://discord.com/api/v10"
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=10)

# Default window: re-post each active ad into a bump channel at most once per
# window — run this cron on any schedule you like (every minute is fine), the
# cooldown is what actually paces it. The owner changes the window (and can
# turn auto-bump off) any time with /ad autobump; this is only the fallback.
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


# ── bump button follows the ad ───────────────────────────────────────────
# discord_bot/cogs/bump.py posts a standalone "🔁 Bump" message (a button whose
# custom_id is bump:prompt:<listing_id>) after every bump, and keeps exactly one
# per listing. An ad card landing in the channel pushes that button up the
# scroll, so after every ad we put a fresh copy of it back BELOW the ad. This
# stays REST-only like the rest of this file — the button itself keeps working
# because its custom_id is handled by the bot's DynamicItem, not by this cron.
BUMP_PROMPT_PREFIX = "bump:prompt:"
BUMP_HISTORY_SCAN = 50  # how far back to look for the existing prompt


def _prompt_listing_id(message: dict) -> int | None:
    for row in message.get("components") or []:
        for comp in row.get("components") or []:
            cid = comp.get("custom_id") or ""
            if cid.startswith(BUMP_PROMPT_PREFIX):
                try:
                    return int(cid[len(BUMP_PROMPT_PREFIX):])
                except ValueError:
                    return None
    return None


def _bump_prompt_components(listing_id: int) -> list:
    return [{
        "type": 1,
        "components": [{
            "type": 2, "style": 3, "label": "Bump", "emoji": {"name": "🔁"},
            "custom_id": f"{BUMP_PROMPT_PREFIX}{listing_id}",
        }],
    }]


async def _bot_user_id(session: aiohttp.ClientSession, token: str, cache: dict) -> int | None:
    if token in cache:
        return cache[token]
    try:
        async with session.get(f"{DISCORD_API_BASE}/users/@me", headers={"Authorization": f"Bot {token}"}) as resp:
            if resp.status != 200:
                return None
            cache[token] = int((await resp.json())["id"])
            return cache[token]
    except (aiohttp.ClientError, TimeoutError, KeyError, ValueError):
        return None


async def _refresh_bump_prompt(session: aiohttp.ClientSession, token: str, channel_id: int,
                               guild_id: int, clone_id, bot_ids: dict) -> bool:
    """Re-post the channel's Bump button below the ad that was just placed.
    Best-effort: never raises, and never affects the ad's own placement."""
    headers = {"Authorization": f"Bot {token}"}
    try:
        me = await _bot_user_id(session, token, bot_ids)
        if me is None:
            return False
        async with session.get(
            f"{DISCORD_API_BASE}/channels/{channel_id}/messages", params={"limit": BUMP_HISTORY_SCAN}, headers=headers,
        ) as resp:
            if resp.status != 200:
                # Can't read history -> can't find (or clean up) the old button,
                # so posting another would just leave two. Skip.
                return False
            history = await resp.json()

        # Newest-first. Our own bump prompts only (anyone can copy a custom_id).
        old = [(m, _prompt_listing_id(m)) for m in history if int((m.get("author") or {}).get("id", 0)) == me]
        old = [(m, lid) for m, lid in old if lid is not None]

        if old:
            newest, listing_id = old[0]
            content = newest.get("content") or "🔁 Tap the button below once the bump timer's up."
        else:
            # No prompt in recent history (scrolled away, or never bumped here):
            # fall back to this server's own listing.
            listing = await db.bump_get_listing(guild_id, clone_id)
            if not listing:
                return False
            listing_id = listing["id"]
            content = f"🔁 **{listing.get('name') or 'Your server'}** — anyone can bump it once the timer's up. Tap the button below."

        payload = {
            "content": content[:2000],
            "components": _bump_prompt_components(listing_id),
            "allowed_mentions": {"parse": []},  # the copied text mentions whoever bumped last
        }
        async with session.post(f"{DISCORD_API_BASE}/channels/{channel_id}/messages", headers=headers, json=payload) as resp:
            if resp.status not in (200, 201):
                logger.warning(f"[cron_ad_placement] couldn't re-post bump button in {channel_id}: HTTP {resp.status}")
                return False

        # New one is up — now retire the old ones so exactly one button is live.
        for message, _ in old:
            try:
                async with session.delete(
                    f"{DISCORD_API_BASE}/channels/{channel_id}/messages/{message['id']}", headers=headers,
                ) as resp:
                    if resp.status not in (200, 204, 404):
                        logger.warning(f"[cron_ad_placement] couldn't delete old bump button in {channel_id}: HTTP {resp.status}")
            except (aiohttp.ClientError, TimeoutError):
                pass
        return True
    except Exception as e:
        logger.warning(f"[cron_ad_placement] bump button refresh failed for channel {channel_id}: {e}")
        return False


async def run_ad_placements() -> dict:
    settings = await get_autobump_settings()
    if not settings["enabled"]:
        return {"ads": 0, "channels": 0, "placed": 0, "failed": 0, "skipped": "ad auto-bump is off"}
    cooldown_seconds = settings["interval_seconds"]
    logger.info(f"[cron_ad_placement] auto-bump on, repeating every {format_interval(cooldown_seconds)}")
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
    bot_ids: dict = {}  # token -> that bot's own user id

    async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT) as session:
        for ad in ads:
            targets = await db.get_unplaced_channels_for_ad(
                ad["id"], all_channels, cooldown_seconds=cooldown_seconds,
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
                    await _refresh_bump_prompt(
                        session, token, target["bump_channel_id"], target["guild_id"], clone_id, bot_ids,
                    )
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
