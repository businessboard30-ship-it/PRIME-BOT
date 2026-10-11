# path: discord_bot/cogs/cat.py

"""
Cute cat pictures: ONE slash command, /cat, plus the Cat button on the join DM.

Images come from The Cat API (thecatapi.com), with Cataas (cataas.com) as the
fallback when it is down or slow. Both are free third-party services, so:
  * only https URLs on those two hosts are ever shown (a bad or odd response can't
    point the embed at some other site),
  * the last few good URLs are kept in memory, so a cat still appears if both
    services are down at the same moment,
  * each person gets one cat every few seconds, so the button/command can't be
    used to hammer the APIs.

Deliberately ONE top-level slash command: Discord caps a bot at 100 GLOBAL slash
commands and this project has crash-looped at that ceiling before (see
custom_role.py). setup() below therefore catches CommandLimitReached: if there is
no slot left, /cat is skipped and logged, the bot still starts, and the join DM
Cat button (which only needs fetch_cat_url) keeps working.
"""

import logging
import random
import time
from collections import deque
from urllib.parse import urlparse

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

import config

logger = logging.getLogger(__name__)

CAT_API_URL = "https://api.thecatapi.com/v1/images/search"
CATAAS_URL = "https://cataas.com/cat?json=true"
ALLOWED_HOSTS = ("thecatapi.com", "cataas.com")
COOLDOWN_SECONDS = 5.0
_TIMEOUT = aiohttp.ClientTimeout(total=6)

_recent = deque(maxlen=50)          # last good URLs, used only if both services fail
_last_use: dict = {}                # user id -> monotonic time of their last cat


def safe_image_url(url) -> bool:
    """https only, and the host must be one of the two services (or a subdomain, e.g. cdn2.thecatapi.com)."""
    try:
        u = urlparse(str(url))
    except ValueError:
        return False
    host = (u.hostname or "").lower()
    return u.scheme == "https" and any(host == h or host.endswith("." + h) for h in ALLOWED_HOSTS)


def cooldown_left(user_id: int) -> float:
    """Seconds this person still has to wait (0 means go ahead, and records the use)."""
    now = time.monotonic()
    left = COOLDOWN_SECONDS - (now - _last_use.get(user_id, -1e9))
    if left > 0:
        return left
    _last_use[user_id] = now
    if len(_last_use) > 5000:
        for k in [k for k, t in _last_use.items() if now - t > COOLDOWN_SECONDS]:
            _last_use.pop(k, None)
    return 0.0


async def _get_json(session, url, headers=None):
    async with session.get(url, headers=headers or {}, timeout=_TIMEOUT) as r:
        if r.status != 200:
            return None
        return await r.json(content_type=None)


async def _from_thecatapi(session):
    headers = {"x-api-key": config.CAT_API_KEY} if getattr(config, "CAT_API_KEY", "") else None
    data = await _get_json(session, CAT_API_URL, headers)
    if isinstance(data, list) and data and isinstance(data[0], dict):
        return data[0].get("url")
    return None


async def _from_cataas(session):
    data = await _get_json(session, CATAAS_URL)
    if not isinstance(data, dict):
        return None
    if data.get("url"):
        return data["url"]
    cat_id = data.get("id") or data.get("_id")
    if cat_id and str(cat_id).replace("-", "").replace("_", "").isalnum():
        return f"https://cataas.com/cat/{cat_id}"
    return None


async def fetch_cat_url(session=None):
    """A cat picture URL, or None if nothing is available. Never raises."""
    own = session is None
    if own:
        session = aiohttp.ClientSession()
    try:
        for source in (_from_thecatapi, _from_cataas):
            try:
                url = await source(session)
            except Exception:
                logger.warning("[cat] %s failed", source.__name__, exc_info=True)
                continue
            if url and safe_image_url(url):
                _recent.append(url)
                return url
    finally:
        if own:
            await session.close()
    return random.choice(list(_recent)) if _recent else None


def cat_embed(url: str) -> discord.Embed:
    embed = discord.Embed(title="🐱 Here's a cat", color=discord.Color.from_rgb(255, 182, 193))
    embed.set_image(url=url)
    embed.set_footer(text="Pictures from thecatapi.com and cataas.com")
    return embed


async def send_cat(interaction: discord.Interaction) -> None:
    """Shared by /cat and the join DM Cat button. The caller has NOT responded yet."""
    wait = cooldown_left(interaction.user.id)
    if wait > 0:
        await interaction.response.send_message(f"😺 Easy there, another cat in {wait:.0f}s.", ephemeral=True)
        return
    await interaction.response.defer(thinking=True)
    url = await fetch_cat_url()
    if not url:
        await interaction.followup.send("😿 I couldn't find a cat right now. Try again in a moment.", ephemeral=True)
        return
    await interaction.followup.send(embed=cat_embed(url))


class CatCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="cat", description="A random cute cat picture 🐱")
    async def cat(self, interaction: discord.Interaction):
        await send_cat(interaction)


async def setup(bot):
    try:
        await bot.add_cog(CatCog(bot))
    except app_commands.CommandLimitReached:
        logger.warning("[cat] no global slash-command slot left: /cat skipped (the join DM Cat button still works)")
