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
from dataclasses import dataclass
from typing import List, Optional

from modules import anime_feed as af
from modules import anime_feed_alerts as alerts

logger = logging.getLogger(__name__)

STOPPED, SLEEPING, RUNNING, BACKOFF = "STOPPED", "SLEEPING", "RUNNING", "BACKOFF"
MIN_SLEEP, MAX_SLEEP = 30, 1800
GRACE_SECONDS = af.LOOKBACK_HOURS * 3600
MAX_POSTS_PER_CYCLE = 25
POST_GAP = 1.0
BACKOFF_BASE, BACKOFF_MAX = 60, 1800

_workers: List["Worker"] = []


def wake_all() -> None:
    for w in list(_workers):
        w.wake()


@dataclass
class PassStats:
    """What one pass did, logged as ONE line so an operator can read the feed's health from the logs alone."""
    servers: int = 0           # servers with the feed on that this bot can see
    due: int = 0               # (server, episode) pairs that were due
    posted: int = 0
    dms: int = 0
    dm_backlog: bool = False   # the per-pass DM cap was hit; the rest goes on the next short pass
    ms: int = 0

    def line(self) -> str:
        return (f"servers={self.servers} due={self.due} posted={self.posted} dms={self.dms} "
                f"dm_backlog={int(self.dm_backlog)} took={self.ms}ms")

    @property
    def busy(self) -> bool:
        return bool(self.posted or self.dms or self.dm_backlog)


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
    def __init__(self, bot, clone_id=None, post_gap: float = POST_GAP):
        self.bot, self.clone_id, self.post_gap = bot, clone_id, post_gap
        self.state = STOPPED
        self.failures = 0
        self._wake = asyncio.Event()
        self.task: Optional[asyncio.Task] = None
        self.posted_total = 0
        self.dms_total = 0
        self.last_pass: Optional[PassStats] = None
        self._paused_logged = False

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
        feed_on, alerts_on = af.feed_enabled(), af.alerts_enabled()
        if not (feed_on or alerts_on):                                # both kill switches are engaged: idle, no database and no provider requests
            if not self._paused_logged:
                logger.warning("[anime-feed] paused by ANIME_FEED_ENABLED / ANIME_ALERTS_ENABLED; no posting, no DMs")
                self._paused_logged = True
            return None
        self._paused_logged = False
        started = time.monotonic()
        guilds = []
        if feed_on:
            try:
                guilds = [g for g in await af.enabled_guilds(self.clone_id) if self.bot.get_guild(g.guild_id) is not None]
            except Exception:
                logger.warning("swallowed exception in Worker.cycle", exc_info=True)
                return self._fail("couldn't read the feed settings")
        dm_work = False
        if alerts_on:
            try:
                dm_work = await alerts.has_dm_followers()             # someone with a DM alert is work too, even when no server has the feed on
            except Exception:
                if not guilds:
                    return self._fail("couldn't read the DM alerts")
                logger.debug("[anime-feed] couldn't read the DM alerts", exc_info=True)
        if not guilds and not dm_work:
            return None
        items, ok = await af.airing_soon()
        if not ok:
            return self._fail("AniList is unreachable")
        self.failures = 0
        self._set(RUNNING)
        now = int(time.time())
        posted = 0
        stats = PassStats(servers=len(guilds))
        for cfg in guilds:
            since = cfg.enabled_at.timestamp() if cfg.enabled_at else None
            for p in due(af.filter_kinds(items, cfg.content_kinds), now, since):
                stats.due += 1
                if posted >= MAX_POSTS_PER_CYCLE:
                    break
                if await self._post(cfg, p):
                    posted += 1
                    await asyncio.sleep(self.post_gap)
        self.posted_total += posted
        dms, backlog = 0, False
        if dm_work:
            try:
                dms, backlog = await self._dm_alerts(items, now)
            except asyncio.CancelledError:
                raise
            except Exception:                                         # DMs never break server posting
                logger.exception("[anime-feed] DM pass crashed")
        self.dms_total += dms
        self._set(SLEEPING)
        wait = MIN_SLEEP if backlog else next_wait(items, now)       # a capped DM queue is finished on the next short pass
        stats.posted, stats.dms, stats.dm_backlog = posted, dms, backlog
        stats.ms = int((time.monotonic() - started) * 1000)
        self.last_pass = stats
        # one INFO line when the pass did something, a DEBUG line when it was quiet (so a healthy idle feed does not fill the log)
        (logger.info if stats.busy else logger.debug)("[anime-feed] pass: %s sleeping=%ss", stats.line(), wait)
        return wait

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
        try:                                                          # people in their quiet hours are not mentioned (they still see the post)
            follower_ids = alerts.split_mentions(follower_ids, await alerts.get_prefs_many(follower_ids), int(time.time()))
        except Exception:
            logger.debug("[anime-feed] couldn't read quiet hours; mentioning as usual", exc_info=True)
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

    async def _dm_alerts(self, items: List[af.Preview], now: int):
        """Sends the opt-in DMs for episodes that just aired. Bounded: at most MAX_DMS_PER_CYCLE per pass, DM_GAP apart, one per
        person per episode (claimed in the database first). Returns (sent, backlog) where backlog means the cap was hit."""
        import discord
        from discord_bot.cogs._views_anime_feed import post_view
        sent = 0
        for p in due(items, now):
            recipients = await alerts.dm_followers(p.anime_id)
            prefs = await alerts.get_prefs_many(recipients)
            for uid in recipients:
                if alerts.is_quiet(prefs.get(uid, alerts.Prefs()), now):
                    continue                                          # held: tried again next pass while the episode is still fresh
                if sent >= alerts.MAX_DMS_PER_CYCLE:
                    return sent, True
                if not await alerts.claim_alert(uid, p.anime_id, p.episode):
                    continue                                          # already sent (restart, or another bot process)
                try:
                    user = self.bot.get_user(uid) or await self.bot.fetch_user(uid)
                    await user.send(view=post_view(p, tag="DM alert", dm=True), allowed_mentions=discord.AllowedMentions.none())
                    await alerts.note_dm_result(uid, True)
                    sent += 1
                except (discord.Forbidden, discord.NotFound):         # closed DMs or a deleted account: keep the claim, count the failure
                    try:
                        await alerts.note_dm_result(uid, False)
                    except Exception:
                        logger.debug("[anime-feed] couldn't count a closed DM", exc_info=True)
                except discord.HTTPException:
                    logger.warning("[anime-feed] DM failed, will retry", exc_info=True)
                    await alerts.release_alert(uid, p.anime_id, p.episode)
                await asyncio.sleep(self.post_gap if self.post_gap < alerts.DM_GAP else alerts.DM_GAP)
        return sent, False
