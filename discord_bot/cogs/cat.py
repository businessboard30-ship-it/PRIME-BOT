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
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlparse

import aiohttp
import discord
from discord_bot.cogs import _cat_visibility as vis
from discord import app_commands
from discord.ext import commands, tasks

from discord_bot.cogs._adaptive_skip import AdaptiveSkip
from modules import guild_activity

import config
from modules import cat_care, cat_health, cat_log, cat_perms, cat_pool, cat_quality, cat_setup, cat_vision

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
SPAWN_EXPIRE_PER_TICK = 20          # old spawns tidied per care tick (every 30 min)
SPAWN_PER_TICK = 60                 # servers handled per tick (Premium servers post as often as every ~7 minutes)

_alerts = cat_perms.AlertTracker()
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
            cat_health.record("api_http_error", error=f"cat API answered HTTP {r.status}")
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
    cat_health.record("api_call")
    try:
        data = await _get_json(session, CAT_API_BATCH_URL, headers)
    except Exception as e:
        cat_health.record("api_exception", error=f"cat API {type(e).__name__}")
        raise
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
    cat_health.record("api_ok" if out else "api_empty")
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
                cat_health.record("cand_known")
                continue
            try:
                data = await _download(session, c["url"])
            except Exception:
                cat_health.record("cand_download")
                continue
            if not data:
                cat_health.record("cand_download")
            q = cat_quality.assess(data) if data else None
            if q is not None and q.ok:
                if not await cat_vision.approve(data):          # AI: must be a clear, nice photo of a cat
                    cat_health.record("cand_vision")
                    continue
                good.append((q, data, c))
            elif q is not None:
                cat_health.record("cand_quality")
                logger.info("[cat] skipped a picture: %s", q.reason)
        good.sort(key=lambda g: -g[0].sharpness)
        best = None
        for q, data, c in good:
            try:
                url = await cat_pool.store(bot, data, q, source, c["id"])
            except Exception:
                logger.warning("[cat] couldn't save to the pool", exc_info=True)
                url = None
            cat_health.record("store_ok" if url else "store_fail")
            best = best or url
        if best:
            return best
        if good:                                    # passed the filter but couldn't be pooled: use it directly
            cat_health.record("direct_used")
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
    if n >= POOL_MIN and cat_pool.manual_request.get():
        # Someone typed /cat or tapped the Cat button: show a stored picture, never the cat API. It is picked without
        # a server, so it is not counted as "seen" and doesn't use up the pictures the automatic spawns still need
        # (that is what used to push busy servers onto the API).
        url = await cat_pool.pick(bot)
        if url:
            cat_health.record("pool_manual")
            return url
    if n >= POOL_MIN:
        window = seen_window(n)
        url = await _pick(bot, guild_id, unseen_only=True, seen_days=window)
        if url:
            cat_health.record("pool_unseen")
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
    url = await _pick(bot, guild_id, seen_days=seen_window(n))
    if url:
        cat_health.record("pool_repeat")
    return url


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
    if _recent:
        cat_health.record("recent_fallback")
        if bot is not None:
            task = asyncio.create_task(cat_health.alert(
                bot, "no new picture could be fetched; an old one was reused. Check the cat API key and the pool."))
            _background.add(task)
            task.add_done_callback(_background.discard)
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


def cat_embed(url: str, next_at: Optional[datetime] = None, title: Optional[str] = None,
              description: Optional[str] = None) -> discord.Embed:
    embed = discord.Embed(title=title or random.choice(CAT_TITLES), description=description,
                          color=discord.Color.from_rgb(255, 182, 193))
    embed.set_image(url=url)
    if next_at is not None:
        ts = int(next_at.timestamp())
        # Discord shows <t:..> in each viewer's own time zone, so "around 8:40 PM" is right for everybody
        embed.add_field(name="⏰ Next cat", value=f"Around <t:{ts}:t> (<t:{ts}:R>)", inline=False)
    return embed


async def send_cat(interaction: discord.Interaction) -> None:
    """Shared by /cat and the join DM Cat button: opens the private cat menu with a fresh cat picture on it.
    The caller has NOT responded yet. If the person asked for a picture a moment ago, the menu still opens, just without one."""
    wait = cooldown_left(interaction.user.id)
    await interaction.response.defer(ephemeral=not vis.is_public(interaction), thinking=True)   # public in a server
    url, note = None, None
    if wait > 0:
        note = f"😺 Easy there, another cat picture in {wait:.0f}s."
    else:
        hint = cat_pool.server_hint.set((interaction.guild_id, getattr(getattr(interaction, "guild", None), "name", None)))
        manual = cat_pool.manual_request.set(True)
        try:
            url = await fetch_cat_url(bot=interaction.client, guild_id=interaction.guild_id)
        finally:
            cat_pool.manual_request.reset(manual)
            cat_pool.server_hint.reset(hint)
    if wait <= 0 and not url:
        note = "😿 I couldn't find a cat picture right now. Try again in a moment."
    from discord_bot.cogs._views_cat_hub import open_hub
    await open_hub(interaction, str(url) if url else None, note=note, deferred=True)


async def post_cat(bot, guild: discord.Guild, channel_id: int, next_at: Optional[datetime] = None) -> bool:
    """Posts one cat in a server channel. False if the channel is gone / not allowed / no cat was available."""
    ch = guild.get_channel(channel_id)
    if not isinstance(ch, discord.TextChannel):
        return False
    missing = cat_perms.missing_names(ch.permissions_for(guild.me))
    if missing:
        logger.info("[cat] not posting in %s (guild %s): missing %s", channel_id, guild.id, ", ".join(missing))
        return False
    runaway, url = None, None
    try:                                  # a cat that wandered off from a hungry home comes back in a spawn first
        runaway = await cat_care.take_runaway(guild.id)
        if runaway is not None:
            raw = await cat_pool.url_for(bot, runaway.pool_id)
            if raw:
                url = cat_pool.PooledUrl(raw, runaway.pool_id)
            else:
                await cat_care.undo_return(runaway.pool_id)
                runaway = None
    except Exception:
        logger.warning("[cat] couldn't bring a runaway cat back", exc_info=True)
        runaway, url = None, None
    if url is None:
        hint = cat_pool.server_hint.set((guild.id, guild.name))      # the stored picture's message will name this server
        try:
            url = await fetch_cat_url(bot=bot, guild_id=guild.id)
        finally:
            cat_pool.server_hint.reset(hint)
    if not url:
        return False
    pool_id = getattr(url, "pool_id", None)
    if pool_id:
        from modules import cat_adopt as _ca
        # Never post the same picture again while it is still waiting in this server (that is the "duplicate cat").
        for _ in range(2):
            if not await _ca.picture_already_open(guild.id, pool_id):
                break
            logger.info("[cat] guild %s: picture %s is already open here, picking another", guild.id, pool_id)
            cat_health.record("dup_blocked")
            runaway = None            # a runaway that is already out stays out; it re-queues itself when its spawn fades
            hint = cat_pool.server_hint.set((guild.id, guild.name))
            try:
                url = await fetch_cat_url(bot=bot, guild_id=guild.id)
            finally:
                cat_pool.server_hint.reset(hint)
            if not url:
                return False
            pool_id = getattr(url, "pool_id", None)
            if not pool_id:
                break
        else:
            return False
        if pool_id and await _ca.picture_already_open(guild.id, pool_id):
            return False
    spawn_id = None
    if pool_id:                                   # only pooled pictures are adoptable (their photo is kept for good)
        try:
            from modules import cat_adopt
            spawn_id = await cat_adopt.create_spawn(guild.id, getattr(bot, "clone_id", None), channel_id, pool_id)
        except Exception:
            logger.warning("[cat] couldn't record the spawn, posting without Adopt", exc_info=True)
    try:             # a Components V2 post; spawn posts sent before this change are classic embeds and still work
        from discord_bot.cogs._views_cat_adopt import spawn_post
        card = (spawn_post(spawn_id, "🐾 A familiar face!", str(url), next_at, cat_care.returned_embed_text(runaway.cat_name))
                if runaway else spawn_post(spawn_id, random.choice(CAT_TITLES), str(url), next_at))
        msg = await ch.send(view=card, allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException:
        logger.info("[cat] couldn't post in channel %s", channel_id, exc_info=True)
        if runaway:
            await cat_care.undo_return(runaway.pool_id)
        return False
    if runaway:
        cat_log.record(bot, "returned", guild_id=guild.id, guild_name=guild.name, pool_id=runaway.pool_id,
                       cat_name=runaway.cat_name, rarity=runaway.rarity, trait=runaway.trait)
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
        self._care.start()
        self._spawn_gate = AdaptiveSkip(idle_threshold=4, idle_skip=1, max_skip=4, name="cat_spawner")
        self._spawner.start()
        # Writing up old cats in the storage channel: main bot only (the pool and channel are shared by every bot).
        if getattr(self.bot, "clone_id", None) is None:
            self._backfill.start()
            self._ops_flush.start()              # main bot posts every bot's queued health reports to the support server
            self._requests_sweep.start()         # main bot gives back the wager of request-board posts that ran out
            self._market_sweep.start()           # main bot closes market listings that ran out of time
            self._league_sweep.start()           # main bot closes League seasons that ran out (promotions, relegations)
        self._health_first = True
        self._health_report.start()              # every bot queues its own report: where pictures came from, why new ones didn't

    async def cog_unload(self):
        self._care.cancel()
        self._spawner.cancel()
        self._backfill.cancel()
        self._health_report.cancel()
        if getattr(self.bot, "clone_id", None) is None:
            self._ops_flush.cancel()
            self._requests_sweep.cancel()
            self._market_sweep.cancel()
            self._league_sweep.cancel()

    @tasks.loop(seconds=cat_care.HUNGER_TICK_SECONDS)
    async def _care(self):
        try:
            await self.care_tick()
        except Exception:
            logger.exception("[cat] care tick failed")

    @_care.before_loop
    async def _before_care(self):
        await self.bot.wait_until_ready()

    async def care_tick(self) -> dict:
        """Random hunger, one gentle heads-up per owner, and cats that wander off after 48 hours hungry (this bot's
        cats only). Every step is independent: one failing never stops the others."""
        clone_id = getattr(self.bot, "clone_id", None)
        out = {"hungry": 0, "warned": 0, "left": 0, "faded": 0}
        try:
            out["hungry"] = await cat_care.make_hungry(clone_id)
        except Exception:
            logger.warning("[cat] hunger step failed", exc_info=True)
        # No "getting hungry" DM any more: owners see hunger in My cats instead.
        try:
            gone = await cat_care.make_runaways(clone_id)
            out["left"] = len(gone)
            by_owner: dict = {}
            for c in gone:
                guild = self.bot.get_guild(c["guild_id"]) if c.get("guild_id") else None
                cat_log.record(self.bot, "runaway", guild_id=c.get("guild_id"), guild_name=getattr(guild, "name", None),
                               user_id=c["owner_id"], pool_id=c["pool_id"], cat_name=c["cat_name"], rarity=c["rarity"],
                               trait=c["trait"])
                by_owner.setdefault(c["owner_id"], []).append(c["cat_name"])
            # No runaway DM any more: owners see it in My cats.
        except Exception:
            logger.warning("[cat] runaway step failed", exc_info=True)
        try:
            out["faded"] = await self.fade_spawns(clone_id)
        except Exception:
            logger.warning("[cat] spawn expiry step failed", exc_info=True)
        if any(out.values()):
            logger.info("[cat] care tick: %s", out)
        return out

    async def fade_spawns(self, clone_id) -> int:
        """Unadopted cats older than cat_adopt.SPAWN_EXPIRE_HOURS wander off: the Adopt button is disabled and the post
        says so. A message that can't be edited (deleted, no access) is simply skipped."""
        from modules import cat_adopt
        from discord_bot.cogs._views_cat_adopt import spawn_edit_kwargs
        rows = await cat_adopt.expire_spawns(clone_id, SPAWN_EXPIRE_PER_TICK)
        for r in rows:
            guild = self.bot.get_guild(r["guild_id"])
            ch = guild.get_channel(r["channel_id"]) if guild else None
            if ch is None or not r.get("message_id"):
                continue
            try:
                msg = await ch.fetch_message(r["message_id"])      # classic or V2: the helper edits it the matching way
                await msg.edit(**spawn_edit_kwargs(msg, r["id"], cat_adopt.EXPIRED_NOTE, gone=True))
            except discord.HTTPException:
                logger.debug("[cat] couldn't mark spawn %s as gone", r["id"], exc_info=True)
            await asyncio.sleep(1)
        return len(rows)

    @tasks.loop(hours=cat_health.REPORT_EVERY_HOURS)
    async def _health_report(self):
        if self._health_first:
            self._health_first = False       # skip the tick that fires at startup; report after a full period
            return
        await cat_health.send_report(self.bot)

    @_health_report.before_loop
    async def _before_health_report(self):
        await self.bot.wait_until_ready()

    @tasks.loop(minutes=5)
    async def _requests_sweep(self):
        try:
            from modules import cat_requests
            n = await cat_requests.expire_due()
            if n:
                logger.info("[cat] request board: %s expired request(s) refunded", n)
        except Exception:
            logger.exception("[cat] request board sweep failed")

    @_requests_sweep.before_loop
    async def _before_requests_sweep(self):
        await self.bot.wait_until_ready()

    @tasks.loop(minutes=10)
    async def _market_sweep(self):
        try:
            from discord_bot.cogs import _views_cat_market as vm
            from modules import cat_market, market
            ids = await market.expire_due_ids()
            for lid in ids:                       # best effort: mark the posted listing as expired
                l = await cat_market.get(lid)
                if l is not None:
                    await vm.close_listing_post(self.bot, l, "\u231B **Expired**, the seller can list it again.")
            if ids:
                logger.info("[cat] market: %s listing(s) expired", len(ids))
        except Exception:
            logger.exception("[cat] market sweep failed")

    @_market_sweep.before_loop
    async def _before_market_sweep(self):
        await self.bot.wait_until_ready()

    @tasks.loop(minutes=30)
    async def _league_sweep(self):
        try:
            from modules import cat_league
            n = await cat_league.rollover()
            if n:
                logger.info("[cat] league: %s season(s) closed", n)
        except Exception:
            logger.exception("[cat] league sweep failed")

    @_league_sweep.before_loop
    async def _before_league_sweep(self):
        await self.bot.wait_until_ready()

    @tasks.loop(minutes=1)
    async def _ops_flush(self):
        from modules import ops_reports
        await ops_reports.flush(self.bot)

    @_ops_flush.before_loop
    async def _before_ops_flush(self):
        await self.bot.wait_until_ready()

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

    @tasks.loop(seconds=SPAWN_TICK_SECONDS)
    async def _spawner(self):
        if not self._spawn_gate.should_run():
            return
        try:
            self._spawn_gate.record(found_something=bool(await self.spawn_due()))
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
            if guild_activity.is_dormant(guild.id):
                # nobody has talked here for weeks: no cat for an empty room; look again in an hour (activity wakes it)
                await cat_setup.defer(cfg.guild_id, clone_id, datetime.now(timezone.utc) + timedelta(hours=1))
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
            done = False
            # claim() just scheduled the next cat: show that time on this one
            next_at = cat_setup.upcoming(cfg, await cat_setup.get_next_at(cfg.guild_id, clone_id))
            for channel_id in channels[start:] + channels[:start]:
                if await post_cat(self.bot, guild, channel_id, next_at):
                    posted += 1
                    done = True
                    break
            if done:
                _alerts.ok(guild.id, clone_id)
            else:
                await self._maybe_alert_owner(guild, clone_id, channels)
        return posted

    async def _maybe_alert_owner(self, guild, clone_id, channels) -> None:
        """The game is ON but nothing could be posted. If a permission or channel is the reason, and it has been for a
        few turns in a row, tell the server owner once (then at most weekly) in the log instead of failing silently. The Cat game never DMs anyone."""
        try:
            problems = cat_perms.diagnose(guild, channels, discord.TextChannel)
            if len(problems) < len(channels):         # at least one channel is fine: the failure was something else
                _alerts.ok(guild.id, clone_id)
                return
            if not _alerts.failed(guild.id, clone_id):
                return
            logger.info("[cat] guild %s: nothing could be posted (permission/channel problem); not DMing the owner", guild.id)
        except Exception:
            logger.warning("[cat] permission alert failed for guild %s", guild.id, exc_info=True)

    @app_commands.command(name="cat", description="Your cat menu: a cat picture, your cats, daily care, market and more 🐱")
    @app_commands.describe(profile="Show this player's cat profile instead of a cat picture")
    async def cat(self, interaction: discord.Interaction, profile: Optional[discord.User] = None):
        if profile is not None:
            from discord_bot.cogs._views_cat_features import show_public_profile
            await show_public_profile(interaction, profile)
            return
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
        return not cat_perms.missing_names(channel.permissions_for(guild.me))

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
