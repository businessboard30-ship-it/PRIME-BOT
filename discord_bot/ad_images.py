"""Image support for sponsored ads (ad_submissions).

Discord attachment CDN URLs are signed and expire, so an ad's image is
re-posted once into the bot's image-hosting channel (the same channel
/welcome custombg uses — set with the owner-only /hostingchannel command or
the IMAGE_HOST_CHANNEL_ID env var). Only (channel_id, message_id) is stored;
a live URL is re-fetched from that message whenever the ad is displayed.
"""

import io
import logging

import discord

import config as bot_config
from database import db

logger = logging.getLogger(__name__)

AD_IMAGE_ALLOWED_CONTENT_TYPES = ("image/png", "image/jpeg", "image/jpg", "image/gif", "image/webp")
AD_IMAGE_MAX_BYTES = 8 * 1024 * 1024  # 8 MB


async def _host_channel(bot) -> discord.TextChannel | None:
    channel_id_str = await db.get_global_setting("image_host_channel_id")
    channel_id = int(channel_id_str) if channel_id_str and channel_id_str.isdigit() else bot_config.IMAGE_HOST_CHANNEL_ID
    if not channel_id:
        return None
    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except discord.HTTPException:
            return None
    return channel if isinstance(channel, discord.TextChannel) else None


async def upload_ad_image(bot, attachment: discord.Attachment, user: discord.abc.User, ad_id=None):
    """Validates + re-posts an ad image. Returns (channel_id, message_id, reason).
    On failure channel_id/message_id are None and reason says why."""
    content_type = (attachment.content_type or "").split(";")[0].strip().lower()
    if content_type not in AD_IMAGE_ALLOWED_CONTENT_TYPES:
        return None, None, f"that file isn't a png/jpeg/gif/webp image (got `{content_type or 'unknown type'}`)"
    if attachment.size > AD_IMAGE_MAX_BYTES:
        return None, None, f"that image is over the {AD_IMAGE_MAX_BYTES // (1024 * 1024)}MB limit"
    host = await _host_channel(bot)
    if host is None:
        return None, None, "image uploads aren't set up yet — the bot owner needs to run `/hostingchannel` in a channel first"
    try:
        data = await attachment.read()
        posted = await host.send(
            content=f"Ad image — ad `{ad_id if ad_id is not None else '?'}` from {user} (`{user.id}`)",
            file=discord.File(io.BytesIO(data), filename=attachment.filename),
        )
    except discord.HTTPException as e:
        logger.warning(f"[ads] couldn't upload ad image to hosting channel: {e}")
        return None, None, "couldn't upload that image right now — try again in a moment"
    return host.id, posted.id, None


async def resolve_ad_image_url(bot, ad: dict) -> str | None:
    """Fresh, non-expired URL for an ad's image, or None."""
    channel_id, message_id = ad.get("image_channel_id"), ad.get("image_message_id")
    if not channel_id or not message_id:
        return None
    try:
        channel = bot.get_channel(int(channel_id)) or await bot.fetch_channel(int(channel_id))
        message = await channel.fetch_message(int(message_id))
        return message.attachments[0].url if message.attachments else None
    except (discord.HTTPException, AttributeError) as e:
        logger.warning(f"[ads] couldn't resolve image for ad {ad.get('id')}: {e}")
        return None
