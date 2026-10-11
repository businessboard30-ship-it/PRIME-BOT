# path: discord_bot/cogs/cat.py

"""
Cute cat pictures: the /cat slash command, /catsetup (opens the Cat setup wizard, same as the Cat button on the join DM)
and that Cat button.

Images come from The Cat API (thecatapi.com), a free third-party service, so:
  * only https URLs on that host are ever shown (a bad or odd response can't
    point the embed at some other site),
  * the last few good URLs are kept in memory, so a cat still appears if the
    service is down at the moment,
  * each person gets one cat every few seconds, so the button/command can't be
    used to hammer the APIs.

Deliberately only TWO top-level slash commands (each in its own cog, added one by one): Discord caps a bot at 100 GLOBAL slash
commands and this project has crash-looped at that ceiling before (see
custom_role.py). setup() below therefore catches CommandLimitReached: if there is
no slot left, the command that doesn't fit is skipped and logged, the bot still starts, and the join DM
Cat button (which only needs fetch_cat_url) keeps working.
"""

import asyncio
import logging
import random
import time
from collections import deque
from urllib.parse import urlparse

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
from modules import cat_pool, cat_quality, cat_reminder, cat_setup, cat_vision

logger = logging.getLogger(__name__)

CAT_API_URL = "https://api.thecatapi.com/v1/images/search"
ALLOWED_HOSTS = ("thecatapi.com",)
COOLDOWN_SECONDS = 5.0
_TIMEOUT = aiohttp.ClientTimeout(total=6)

# Shared pool (main bot + every clone): once it holds POOL_MIN good pictures, bots post pooled ones instead of
# calling the cat APIs; one request in GROW_ONE_IN still tops the pool up (until POOL_MAX).
CAT_API_BATCH_URL = CAT_API_URL + "?limit=5&size=full&mime_types=jpg,png"
POOL_MIN = 20
POOL_MAX = 1500           # pictures live in the image-hosting channel; sized so the busiest server (250-300 cats a day)
                         # can go ~5 days before it has seen them all
GROW_ONE_IN = 8
DOWNLOAD_TIMEOUT = aiohttp.ClientTimeout(total=12)
SPAWN_TICK_SECONDS = 30
BACKFILL_TICK_MINUTES = 5            # old-cat backfill: 15 items a tick, so even a big backlog drains in hours, gently
REMINDER_TICK_MINUTES = 10
REMINDER_PER_TICK = 5
SPAWN_PER_TICK = 60                 # servers handled per tick (Premium servers post as often as every ~7 minutes)

_recent = deque(maxlen=50)          # last good URLs, used only if both services fail
_last_use: dict = {}                # user id -> monotonic time of their last cat


def safe_image_url(url) -> bool:
    """https only, and the host must be The Cat API (or a subdomain, e.g. cdn2.thecatapi.com)."""
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


_fill_lock = asyncio.Lock()
_background: set = set()


async def _candidates_thecatapi(session) -> list:
    """Up to 5 pictures from ONE API call, big ones first. Pictures already known to be too small are dropped here."""
    headers = {"x-api-key": config.CAT_API_KEY} if getattr(config, "CAT_API_KEY", "") else None
    data = await _get_json(session, CAT_API_BATCH_URL, headers)
    out = []
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, dict) or not safe_image_url(item.get("url")):
            continue
        w, h = item.get("width"), item.get("height")
        if isinstance(w, int) and isinstance(h, int) and (min(w, h) < cat_quality.MIN_SHORT_SIDE
                                                           or max(w, h) < cat_quality.MIN_LONG_SIDE):
            continue
        out.append({"url": item["url"], "id": str(item.get("id") or ""), "pixels": (w or 0) * (h or 0)})
    out.sort(key=lambda c: -c["pixels"])
    return out[:5]


async def _download(session, url: str):
    """The picture's bytes (capped), or None. Only allow-listed hosts."""
    if not safe_image_url(url):
        return None
    async with session.get(url, timeout=DOWNLOAD_TIMEOUT) as r:
        if r.status != 200:
            return None
        buf = bytearray()
        async for chunk in r.content.iter_chunked(64 * 1024):
            buf += chunk
            if len(buf) > cat_quality.MAX_BYTES:
                return None
        return bytes(buf)


async def _grow_pool(bot, session):
    """ONE API call: download the candidates, keep EVERY one that passes the quality filter (the call is already
    paid for), save them to the shared pool, and return the sharpest one's link (None if nothing passed)."""
    for source, getter in (("thecatapi", _candidates_thecatapi),):
        try:
            cands = await getter(session)
        except Exception:
            logger.warning("[cat] %s failed", source, exc_info=True)
            continue
        try:
            known = await cat_pool.known_source_ids(source, [c["id"] for c in cands])
        except Exception:
            known = set()
        good = []
        for c in cands:
            if c["id"] and c["id"] in known:
                continue
            try:
                data = await _download(session, c["url"])
            except Exception:
                continue
            q = cat_quality.assess(data) if data else None
            if q is not None and q.ok:
                if not await cat_vision.approve(data):          # AI: must be a clear, nice photo of a cat
                    continue
                good.append((q, data, c))
            elif q is not None:
                logger.info("[cat] skipped a picture: %s", q.reason)
        good.sort(key=lambda g: -g[0].sharpness)
        best = None
        for q, data, c in good:
            try:
                url = await cat_pool.store(bot, data, q, source, c["id"])
            except Exception:
                logger.warning("[cat] couldn't save to the pool", exc_info=True)
                url = None
            best = best or url
        if best:
            return best
        if good:                                    # passed the filter but couldn't be pooled: use it directly
            return good[0][2]["url"]
    return None


async def _grow_in_background(bot) -> None:
    try:
        if _fill_lock.locked():
            return
        async with _fill_lock, aiohttp.ClientSession() as session:
            await _grow_pool(bot, session)
    except Exception:
        logger.warning("[cat] background pool top-up failed", exc_info=True)


def seen_window(pool_size: int) -> int:
    """How many days back "this server already saw it" counts. It must fit the pool: if the window holds more
    pictures than the pool has, a busy server would see everything within days and every post would need the cat
    API. So it is the pool size divided by the busiest server's daily cats (Premium max), between 1 day and
    cat_pool.SEEN_DAYS: a 400-picture pool gives 1 day, 1500 gives 5."""
    return max(1, min(cat_pool.SEEN_DAYS, pool_size // max(1, cat_setup.PREMIUM.daily_max)))


async def _pick(bot, guild_id, unseen_only=False, seen_days=None):
    # Without a server the original pick is used unchanged (also keeps older callers/tests working).
    if guild_id is None:
        return await cat_pool.pick(bot)
    return await cat_pool.pick(bot, guild_id=guild_id, unseen_only=unseen_only, seen_days=seen_days)


async def _fetch_via_pool(bot, session, guild_id=None):
    """Order of preference, cheapest first:
    1. a pooled picture this server has NOT seen lately (no API call at all);
    2. the cat API (only now, because the server has seen everything we hold), saving every good result to the pool;
    3. if the API gave nothing, a repeat from the pool (the one this server saw longest ago) rather than no cat."""
    n = await cat_pool.pool_count()
    if n >= POOL_MIN:
        window = seen_window(n)
        url = await _pick(bot, guild_id, unseen_only=True, seen_days=window)
        if url:
            if n < POOL_MAX and random.randrange(GROW_ONE_IN) == 0 and not _fill_lock.locked():
                task = asyncio.create_task(_grow_in_background(bot))
                _background.add(task)
                task.add_done_callback(_background.discard)
            return url
    async with _fill_lock:
        url = await _grow_pool(bot, session)
    if url:
        if guild_id is not None:
            await cat_pool.mark_seen(guild_id, getattr(url, "pool_id", None))
        return url
    return await _pick(bot, guild_id, seen_days=seen_window(n))


async def fetch_cat_url(session=None, bot=None, guild_id=None):
    """A cat picture URL, or None if nothing is available. Never raises.

    With `bot`, pictures come from the shared quality-filtered pool (see modules/cat_pool.py) and the cat APIs are
    only called to top it up. With `guild_id` that server is also not shown a picture it has already seen lately
    (until it has seen them all). Without `bot` (or if the pool is unavailable) the old direct path is used."""
    own = session is None
    if own:
        session = aiohttp.ClientSession()
    try:
        if bot is not None:
            try:
                url = await _fetch_via_pool(bot, session, guild_id)
                if url:
                    _recent.append(url)
                    return url
            except Exception:
                logger.warning("[cat] pool path failed, using the direct API", exc_info=True)
        for source in (_from_thecatapi,):
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


CAT_TITLES = (
    "😻✨ Meow! A cutie just landed 🐾",
    "🐱💖 Fresh cat, just for you! 🌸",
    "😸🎀 Paws-itively adorable! 🐾",
    "😽🌟 Purr-fect timing, look who's here! 💕",
    "🐈🍀 Cat delivery! Handle with love 🐾",
    "😺🌈 Say hi to this little fluff! ✨",
    "😻🧶 Someone ordered a cat? 🐾💗",
)


def cat_embed(url: str) -> discord.Embed:
    embed = discord.Embed(title=random.choice(CAT_TITLES), color=discord.Color.from_rgb(255, 182, 193))
    embed.set_image(url=url)
    return embed


async def send_cat(interaction: discord.Interaction) -> None:
    """Shared by /cat and the join DM Cat button. The caller has NOT responded yet."""
    wait = cooldown_left(interaction.user.id)
    if wait > 0:
        await interaction.response.send_message(f"😺 Easy there, another cat in {wait:.0f}s.", ephemeral=True)
        return
    await interaction.response.defer(thinking=True)
    hint = cat_pool.server_hint.set((interaction.guild_id, getattr(getattr(interaction, "guild", None), "name", None)))
    try:
        url = await fetch_cat_url(bot=interaction.client, guild_id=interaction.guild_id)
    finally:
        cat_pool.server_hint.reset(hint)
    if not url:
        await interaction.followup.send("😿 I couldn't find a cat right now. Try again in a moment.", ephemeral=True)
        return
    from discord_bot.cogs._views_cat_adopt import MyCatsButton
    view = discord.ui.View(timeout=None)
    view.add_item(MyCatsButton())
    await interaction.followup.send(embed=cat_embed(url), view=view)


async def post_cat(bot, guild: discord.Guild, channel_id: int) -> bool:
    """Posts one cat in a server channel. False if the channel is gone / not allowed / no cat was available."""
    ch = guild.get_channel(channel_id)
    if not isinstance(ch, discord.TextChannel):
        return False
    perms = ch.permissions_for(guild.me)
    if not (perms.view_channel and perms.send_messages and perms.embed_links):
        return False
    hint = cat_pool.server_hint.set((guild.id, guild.name))      # the stored picture's message will name this server
    try:
        url = await fetch_cat_url(bot=bot, guild_id=guild.id)
    finally:
        cat_pool.server_hint.reset(hint)
    if not url:
        return False
    pool_id = getattr(url, "pool_id", None)
    spawn_id = None
    if pool_id:                                   # only pooled pictures are adoptable (their photo is kept for good)
        try:
            from modules import cat_adopt
            spawn_id = await cat_adopt.create_spawn(guild.id, getattr(bot, "clone_id", None), channel_id, pool_id)
        except Exception:
            logger.warning("[cat] couldn't record the spawn, posting without Adopt", exc_info=True)
    try:
        kwargs = {}
        if spawn_id is not None:
            from discord_bot.cogs._views_cat_adopt import spawn_view
            kwargs["view"] = spawn_view(spawn_id)
        msg = await ch.send(embed=cat_embed(url), allowed_mentions=discord.AllowedMentions.none(), **kwargs)
    except discord.HTTPException:
        logger.info("[cat] couldn't post in channel %s", channel_id, exc_info=True)
        return False
    if spawn_id is not None:
        try:
            await cat_adopt.set_spawn_message(spawn_id, msg.id)
        except Exception:
            logger.debug("[cat] couldn't save the spawn message id", exc_info=True)
    return True


class CatCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        self._spawner.start()
        self._reminders.start()
        # Writing up old cats in the storage channel: main bot only (the pool and channel are shared by every bot).
        if getattr(self.bot, "clone_id", None) is None:
            self._backfill.start()

    async def cog_unload(self):
        self._spawner.cancel()
        self._reminders.cancel()
        self._backfill.cancel()

    @tasks.loop(minutes=BACKFILL_TICK_MINUTES)
    async def _backfill(self):
        try:
            from modules import cat_log_backfill
            if cat_log_backfill.enabled():
                result = await cat_log_backfill.run_once(self.bot)
                if any(result.get(k) for k in ("logged", "captions", "retry")):
                    logger.info("[cat] backfill tick: %s", result)
        except Exception:
            logger.exception("[cat] backfill tick failed")

    @_backfill.before_loop
    async def _before_backfill(self):
        await self.bot.wait_until_ready()

    @tasks.loop(minutes=REMINDER_TICK_MINUTES)
    async def _reminders(self):
        try:
            await self.remind_due()
        except Exception:
            logger.exception("[cat] reminder tick failed")

    @_reminders.before_loop
    async def _before_reminders(self):
        await self.bot.wait_until_ready()

    async def remind_due(self) -> int:
        """Reminds a few servers (of THIS bot) whose Cat game is off. Each server gets at most 2 reminders inside one
        week, then never again (modules/cat_reminder.py). A few per tick so a big rollout never floods Discord."""
        clone_id = getattr(self.bot, "clone_id", None)
        on = await cat_reminder.enabled_guilds(clone_id)
        known = await cat_reminder.states(clone_id)
        off = {g.id: g.id not in on for g in self.bot.guilds}
        sent = 0
        for guild in cat_reminder.pick_candidates(self.bot.guilds, off, known, limit=REMINDER_PER_TICK):
            if not await cat_reminder.claim(guild.id, clone_id):
                continue
            try:
                owner = await self.bot.fetch_user(guild.owner_id)
            except discord.HTTPException:
                owner = None
            if owner is None or owner.bot:
                await cat_reminder.finish(guild.id, clone_id)
                continue
            embed = None
            try:
                url = await fetch_cat_url(bot=self.bot, guild_id=guild.id)
                embed = cat_embed(url) if url else None
            except Exception:
                logger.warning("[cat] no cat picture for the reminder of guild %s", guild.id, exc_info=True)
            try:
                await owner.send(cat_reminder.reminder_text(guild.name), embed=embed)
                sent += 1
            except discord.HTTPException:
                await cat_reminder.finish(guild.id, clone_id)          # closed DMs: never retried
            await asyncio.sleep(2)
        return sent

    @tasks.loop(seconds=SPAWN_TICK_SECONDS)
    async def _spawner(self):
        try:
            await self.spawn_due()
        except Exception:
            logger.exception("[cat] spawner tick failed")

    @_spawner.before_loop
    async def _before_spawner(self):
        await self.bot.wait_until_ready()

    async def spawn_due(self) -> int:
        """Posts the cats that are due for THIS bot (main or one clone). Returns how many were posted."""
        clone_id = getattr(self.bot, "clone_id", None)
        posted = 0
        for cfg in await cat_setup.due(clone_id, SPAWN_PER_TICK):
            guild = self.bot.get_guild(cfg.guild_id)
            if guild is None:
                continue
            if not cfg.is_open():
                # outside the server's chosen hours: post nothing, wake up when the window opens
                await cat_setup.defer(cfg.guild_id, clone_id, cat_setup.next_window_start(cfg.active_start, cfg.active_end))
                continue
            premium = await cat_setup.is_premium(guild.id, clone_id)
            channels, gap = cfg.effective(premium)            # a lapsed Premium server drops to the free amount
            if not channels or not await cat_setup.claim(cfg.guild_id, clone_id, gap * random.uniform(1 - cat_setup.GAP_JITTER, 1 + cat_setup.GAP_JITTER)):
                continue
            # one cat per turn, rotating through the channels the server picked (the daily amount is per server)
            start = int(time.time() // max(gap, 1)) % len(channels)
            for channel_id in channels[start:] + channels[:start]:
                if await post_cat(self.bot, guild, channel_id):
                    posted += 1
                    break
        return posted

    @app_commands.command(name="cat", description="A random cute cat picture 🐱")
    async def cat(self, interaction: discord.Interaction):
        await send_cat(interaction)


class CatSetupCog(commands.Cog):
    """/catsetup: opens the same Cat setup wizard as the Cat button on the join DM."""

    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="catsetup", description="Set up automatic cats in this server (needs Manage Server) 🐱")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def catsetup(self, interaction: discord.Interaction):
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("Use this in a server.", ephemeral=True)
            return
        from discord_bot.cogs._views_shared import user_can_manage_guild
        if not await user_can_manage_guild(guild, interaction.user.id):
            await interaction.response.send_message("You need the **Manage Server** permission to set up cats.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        channel = interaction.channel if self._can_post(interaction.channel, guild) else self._fallback(guild)
        if channel is None:
            await interaction.followup.send(
                "I couldn't find a channel I can post in. Give me **Send Messages** and **Embed Links** in one and try again.",
                ephemeral=True)
            return
        try:
            from discord_bot.cogs._views_cat_setup import open_setup
            await open_setup(self.bot, guild, getattr(self.bot, "clone_id", None), interaction.user.id, channel)
        except (discord.Forbidden, discord.HTTPException):
            logger.exception("[cat] /catsetup couldn't post the wizard in guild %s", guild.id)
            await interaction.followup.send(
                "I couldn't post the Cat setup. Make sure I can **Send Messages** and **Embed Links** in that channel.", ephemeral=True)
            return
        where = "here" if channel == interaction.channel else channel.mention
        await interaction.followup.send(f"🐱 Cat setup opened {where}. Only you can use it.", ephemeral=True)

    @staticmethod
    def _can_post(channel, guild) -> bool:
        if not isinstance(channel, discord.TextChannel):
            return False
        p = channel.permissions_for(guild.me)
        return bool(p.view_channel and p.send_messages and p.embed_links)

    def _fallback(self, guild):
        for ch in [guild.system_channel, *guild.text_channels]:
            if self._can_post(ch, guild):
                return ch
        return None


async def setup(bot):
    try:
        await bot.add_cog(CatCog(bot))
    except app_commands.CommandLimitReached:
        logger.warning("[cat] no global slash-command slot left: /cat skipped (the join DM Cat button still works)")
    try:
        await bot.add_cog(CatSetupCog(bot))
    except app_commands.CommandLimitReached:
        logger.warning("[cat] no global slash-command slot left: /catsetup skipped (the join DM Cat button still opens the setup)")
