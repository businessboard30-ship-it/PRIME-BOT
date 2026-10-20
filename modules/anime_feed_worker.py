# path: modules/anime_feed_worker.py

"""
Anime feed worker (slice B): ONE shared background task per bot process, not one per server.

Lifecycle (from the plan): STOPPED = nobody has the feed on, so it waits on an event with NO timer and makes no provider
requests; SLEEPING = feeds are on, waiting until the next episode is due (or at most 30 minutes, to refresh the schedule);
RUNNING = posting a bounded batch; BACKOFF = the provider or database failed, wait with exponential backoff and jitter.
A settings change calls `wake_all()` so it takes effect at once. One cached AniList request serves every server. Each post
is claimed in the database BEFORE sending, so restarts and retries never double post. Only episodes that air after a feed
was switched on are posted (never a backlog), and only ones that aired within the last 3 hours.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import List, Optional

from modules import anime_alerts as aa
from modules import anime_feed as af

logger = logging.getLogger(__name__)

STOPPED, SLEEPING, RUNNING, BACKOFF = "STOPPED", "SLEEPING", "RUNNING", "BACKOFF"
MIN_SLEEP, MAX_SLEEP = 30, 1800
GRACE_SECONDS = af.LOOKBACK_HOURS * 3600
MAX_POSTS_PER_CYCLE = 25
POST_GAP = 1.0
BACKOFF_BASE, BACKOFF_MAX = 60, 1800
MAX_DMS_PER_CYCLE = 40                  # bounded DM queue: at most this many alerts per pass
DM_GAP = 1.2                            # seconds between DMs (about 50 a minute at most)

_workers: List["Worker"] = []


def wake_all() -> None:
    for w in list(_workers):
        w.wake()


# ── pure rules ──────────────────────────────────────────────────────────

def due(items: List[af.Preview], now: int, since_ts: Optional[float] = None) -> List[af.Preview]:
    """Episodes that have aired, within the grace window, and (when given) after the feed was switched on."""
    out = []
    for p in items:
        if p.airing_at > now or now - p.airing_at > GRACE_SECONDS:
            continue
        if since_ts is not None and p.airing_at < since_ts:
            continue
        out.append(p)
    return out


def next_wait(items: List[af.Preview], now: int) -> int:
    """Seconds to sleep: until the next scheduled episode (+5 s), never less than 30 s or more than 30 min."""
    future = [p.airing_at for p in items if p.airing_at > now]
    wait = (min(future) - now + 5) if future else MAX_SLEEP
    return max(MIN_SLEEP, min(MAX_SLEEP, wait))


def backoff_delay(attempt: int, rng=random) -> float:
    return min(BACKOFF_BASE * (2 ** max(0, attempt)), BACKOFF_MAX) * rng.uniform(0.5, 1.0)


# ── the worker ──────────────────────────────────────────────────────────

class Worker:
    def __init__(self, bot, clone_id=None, post_gap: float = POST_GAP, dm_gap: float = DM_GAP):
        self.bot, self.clone_id, self.post_gap, self.dm_gap = bot, clone_id, post_gap, dm_gap
        self.state = STOPPED
        self.failures = 0
        self._wake = asyncio.Event()
        self.task: Optional[asyncio.Task] = None
        self.posted_total = 0

    def wake(self) -> None:
        self._wake.set()

    def start(self) -> None:
        if self.task is None or self.task.done():
            _workers.append(self)
            self.task = asyncio.ensure_future(self.run())

    async def stop(self) -> None:
        """Graceful shutdown: cancel the task and wait for it. Claims are made before sending, so nothing is lost or doubled."""
        if self in _workers:
            _workers.remove(self)
        if self.task is not None:
            self.task.cancel()
            try:
                await self.task
            except (asyncio.CancelledError, Exception):
                pass
            self.task = None

    def _set(self, state: str) -> None:
        if state != self.state:
            logger.info("[anime-feed] worker %s -> %s", self.state, state)
            self.state = state

    async def run(self) -> None:
        wait_ready = getattr(self.bot, "wait_until_ready", None)
        if wait_ready:
            await wait_ready()
        while True:
            try:
                wait = await self.cycle()
            except asyncio.CancelledError:
                raise
            except Exception:                             # never let one bad pass kill the shared worker: back off and go on
                logger.exception("[anime-feed] cycle crashed")
                wait = self._fail("a pass crashed")
            if wait is None:                              # nobody has the feed on: wait for an event, no timer, no polling
                self._set(STOPPED)
                await self._wake.wait()
                self._wake.clear()
                continue
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=wait)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()

    async def cycle(self) -> Optional[float]:
        """One pass. Returns seconds to wait, or None when there is nothing to do until something changes."""
        try:
            guilds = [g for g in await af.enabled_guilds(self.clone_id) if self.bot.get_guild(g.guild_id) is not None]
        except Exception:
            return self._fail("couldn't read the feed settings")
        try:
            dm_wanted = await aa.any_dm_alerts()                  # someone with a DM alert keeps the worker awake even with no feeds on
        except Exception:
            logger.debug("[anime-feed] couldn't check DM alerts", exc_info=True)
            dm_wanted = False
        if not guilds and not dm_wanted:
            return None
        items, ok = await af.airing_soon()
        if not ok:
            return self._fail("AniList is unreachable")
        self.failures = 0
        self._set(RUNNING)
        now = int(time.time())
        posted = 0
        for cfg in guilds:
            since = cfg.enabled_at.timestamp() if cfg.enabled_at else None
            for p in due(items, now, since):
                if posted >= MAX_POSTS_PER_CYCLE:
                    break
                if await self._post(cfg, p):
                    posted += 1
                    await asyncio.sleep(self.post_gap)
        self.posted_total += posted
        if dm_wanted:
            try:
                await self._deliver_dms(items, now)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("[anime-feed] DM pass failed")
        self._set(SLEEPING)
        wait = next_wait(items, now)
        logger.debug("[anime-feed] posted %s, sleeping %ss (%s servers)", posted, wait, len(guilds))
        return wait

    async def _deliver_dms(self, items: List[af.Preview], now: int) -> int:
        """Private alerts for people who chose DM. Claimed BEFORE sending; skipped in quiet hours; bounded per pass."""
        sent = 0
        for p in due(items, now):
            if sent >= MAX_DMS_PER_CYCLE:
                break
            try:
                targets = await aa.dm_targets(p.anime_id, p.airing_at)
            except Exception:
                logger.warning("[anime-feed] couldn't read DM targets", exc_info=True)
                continue
            for t in targets:
                if sent >= MAX_DMS_PER_CYCLE:
                    break
                if aa.in_quiet(now, t.tz_minutes, t.quiet_start, t.quiet_end):
                    continue
                try:
                    if not await aa.claim_dm(t.user_id, p.anime_id, p.episode):
                        continue
                except Exception:
                    logger.warning("[anime-feed] couldn't claim a DM", exc_info=True)
                    continue
                if await self._dm(t.user_id, p):
                    sent += 1
                await asyncio.sleep(self.dm_gap)
        return sent

    async def _dm(self, user_id: int, p: af.Preview) -> bool:
        import discord
        from discord_bot.cogs._views_anime_feed import post_view
        try:
            user = self.bot.get_user(user_id) or await self.bot.fetch_user(user_id)
            await user.send(view=post_view(p, tag="Alert · change it with Alert me"), allowed_mentions=discord.AllowedMentions.none())
        except (discord.Forbidden, discord.NotFound):                   # DMs closed (or the account is gone): keep the claim, count it
            try:
                if await aa.note_dm_result(user_id, False):
                    logger.info("[anime-feed] DMs closed 3 times in a row; a person's DM alerts moved back to mention mode")
            except Exception:
                logger.debug("[anime-feed] couldn't record a DM failure", exc_info=True)
            return False
        except Exception:                                               # temporary: give the claim back so a later pass retries
            logger.warning("[anime-feed] DM failed, will retry", exc_info=True)
            try:
                await aa.release_dm(user_id, p.anime_id, p.episode)
            except Exception:
                logger.debug("[anime-feed] couldn't release a DM claim", exc_info=True)
            return False
        try:
            await aa.note_dm_result(user_id, True)
        except Exception:
            logger.debug("[anime-feed] couldn't record a DM success", exc_info=True)
        return True

    def _fail(self, why: str) -> float:
        delay = backoff_delay(self.failures)
        self.failures += 1
        self._set(BACKOFF)
        logger.warning("[anime-feed] %s; backing off %.0fs", why, delay)
        return delay

    async def _post(self, cfg: af.FeedConfig, p: af.Preview) -> bool:
        import discord
        from discord_bot.cogs._views_anime_feed import post_view
        try:
            if not await af.claim(cfg.guild_id, self.clone_id, p.anime_id, p.episode):
                return False                              # already posted (or another worker has it)
        except Exception:
            logger.warning("[anime-feed] couldn't claim a post", exc_info=True)
            return False
        guild = self.bot.get_guild(cfg.guild_id)
        channel = guild.get_channel(cfg.channel_id) if guild else None
        if channel is None:
            try:
                await af.disable(cfg.guild_id, self.clone_id)         # the channel was deleted: stop instead of failing forever
            except Exception:
                logger.debug("[anime-feed] couldn't switch a feed off", exc_info=True)
            return False
        follower_ids = [u for u in await af.followers_of(p.anime_id) if guild.get_member(u) is not None]
        line, shown = af.ping_line(cfg.ping_role_id, follower_ids)
        mentions = discord.AllowedMentions(users=[discord.Object(id=u) for u in shown], roles=[discord.Object(id=cfg.ping_role_id)] if cfg.ping_role_id else False,
                                           everyone=False)
        try:
            await channel.send(view=post_view(p, ping=line), allowed_mentions=mentions)
            return True
        except discord.Forbidden:
            logger.info("[anime-feed] no permission to post in guild %s", cfg.guild_id)   # keep the claim: don't retry forever
            return False
        except discord.HTTPException:
            logger.warning("[anime-feed] post failed in guild %s, will retry", cfg.guild_id, exc_info=True)
            try:
                await af.release(cfg.guild_id, self.clone_id, p.anime_id, p.episode)
            except Exception:
                logger.debug("[anime-feed] couldn't release a claim", exc_info=True)
            return False
