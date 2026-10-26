# path: discord_bot/cogs/clone_health.py
"""Main bot only: a self-updating clone health board in the support server.

On start it finds (or creates, private to admins and this bot) the #clone-health channel in the support server and
keeps one big Components V2 board there: every clone's state, servers and last-seen time. It edits the SAME messages;
it never posts a new one unless a page is missing.

Staying quiet is part of the design:
  * It looks every minute but only calls Discord when a clone changes state or server count (modules/clone_health
    fingerprint). "Seen 3m ago" is a live <t:..:R> timestamp, so time passing needs no edit. A slow refresh keeps the
    member counts roughly fresh, and edits are never closer together than MIN_EDIT_GAP.
  * Logging: one WARNING when a clone goes offline (four missed heartbeats, not one), one INFO when it recovers, and a
    single summary line when many go at once (that is an infrastructure event, not N separate problems). No alert on a
    restart: the first look after boot is only a baseline. A failing tick logs on the first failure and then rarely.
  * Nothing here can ping anyone (allowed_mentions none) and clones never load this cog.
"""
from __future__ import annotations

import logging
import time
from collections import deque
from typing import Optional

import discord
from discord.ext import commands, tasks

import config
from database import db
from modules import clone_health as ch

logger = logging.getLogger(__name__)

CHANNEL_NAME = "clone-health"
CHANNEL_TOPIC = "Live health of every clone. One self-updating message, posted by the main bot. Staff only."
TICK_SECONDS = 60
MIN_EDIT_GAP = 30            # never edit twice within this many seconds
REFRESH_SECONDS = 3600       # also refresh hourly so member counts do not go stale
MASS_OFFLINE = 5             # this many going offline at once = one summary line
LOG_EVERY_N_FAILURES = 30


class CloneHealthCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._boot_ts = int(time.time())
        self._channel_id: Optional[int] = None
        self._messages: list[discord.Message] = []
        self._fp: Optional[str] = None
        self._prev: Optional[dict] = None
        self._events: deque = deque(maxlen=6)
        self._last_edit = 0.0
        self._failures = 0
        self._channel_warned = False

    async def cog_load(self):
        self.tick.start()

    async def cog_unload(self):
        self.tick.cancel()

    @tasks.loop(seconds=TICK_SECONDS)
    async def tick(self):
        try:
            await self.refresh()
            self._failures = 0
        except Exception:
            self._failures += 1
            if self._failures == 1 or self._failures % LOG_EVERY_N_FAILURES == 0:
                logger.warning("[clone-health] update failed (%d in a row)", self._failures, exc_info=True)

    @tick.before_loop
    async def _before_tick(self):
        await self.bot.wait_until_ready()

    # ---- the whole cycle ----
    async def refresh(self, force: bool = False) -> bool:
        """One look. Returns True when the board was written. Safe to call any time."""
        now = time.time()
        entries = ch.build_entries(await db.get_clone_health_rows(), now)
        self._record_transitions(entries, now)
        self._prev = {e.clone_id: e.state for e in entries}

        main = ch.MainInfo(len(self.bot.guilds), sum((g.member_count or 0) for g in self.bot.guilds), self._boot_ts)
        fp = ch.fingerprint(entries, main.servers)
        due = force or fp != self._fp or (now - self._last_edit) >= REFRESH_SECONDS
        if not due:
            return False
        if self._messages and not force and (now - self._last_edit) < MIN_EDIT_GAP:
            return False                      # changed, but too soon after the last edit: the next look retries

        channel = await self._ensure_channel()
        if channel is None:
            return False
        pages = ch.build_pages(entries, main, list(self._events))
        if not await self._publish(channel, pages):
            return False
        self._fp, self._last_edit = fp, now
        return True

    # ---- logging: only what someone would act on ----
    def _record_transitions(self, entries, now: float) -> None:
        went_down, recovered = ch.diff_states(self._prev, entries)
        stamp = int(now)
        if len(went_down) >= MASS_OFFLINE:
            ids = ", ".join(f"#{e.clone_id}" for e in went_down[:8]) + ("…" if len(went_down) > 8 else "")
            logger.warning("[clone-health] %d clones went offline at once (%s): likely a host or database problem, "
                           "not the clones themselves", len(went_down), ids)
            self._events.append((stamp, f"🔴 {len(went_down)} clones went offline at once"))
        else:
            for e in went_down:
                logger.warning("[clone-health] clone #%d (@%s) is offline: no heartbeat for over %d minutes",
                               e.clone_id, e.name, ch.DELAYED_MAX_AGE // 60)
                self._events.append((stamp, f"🔴 #{e.clone_id} {e.name} went offline"))
        if len(recovered) >= MASS_OFFLINE:
            logger.info("[clone-health] %d clones are back", len(recovered))
            self._events.append((stamp, f"🟢 {len(recovered)} clones recovered"))
        else:
            for e in recovered:
                logger.info("[clone-health] clone #%d (@%s) is back", e.clone_id, e.name)
                self._events.append((stamp, f"🟢 #{e.clone_id} {e.name} recovered"))

    # ---- channel + messages ----
    async def _ensure_channel(self) -> Optional[discord.TextChannel]:
        guild = self.bot.get_guild(int(getattr(config, "DISCORD_SUPPORT_SERVER_ID", 0) or 0))
        if guild is None:
            return None
        channel = guild.get_channel(self._channel_id) if self._channel_id else None
        if channel is None:
            channel = discord.utils.get(guild.text_channels, name=CHANNEL_NAME)
        if channel is None:
            try:
                overwrites = {
                    guild.default_role: discord.PermissionOverwrite(view_channel=False),
                    guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, embed_links=True,
                                                         read_message_history=True),
                }
                channel = await guild.create_text_channel(CHANNEL_NAME, overwrites=overwrites, topic=CHANNEL_TOPIC,
                                                          reason="Clone health board")
                self._messages = []
            except discord.HTTPException:
                if not self._channel_warned:          # once, not every minute
                    logger.warning("[clone-health] couldn't create #%s in the support server (needs Manage Channels)",
                                   CHANNEL_NAME, exc_info=True)
                    self._channel_warned = True
                return None
        self._channel_warned = False
        if channel.id != self._channel_id:
            self._channel_id, self._messages = channel.id, []
        return channel

    async def _adopt_existing(self, channel: discord.TextChannel) -> None:
        """After a restart, reuse the board messages already in the channel instead of posting new ones."""
        me = self.bot.user.id
        self._messages = [m async for m in channel.history(limit=100, oldest_first=True) if m.author.id == me]

    async def _publish(self, channel: discord.TextChannel, pages) -> bool:
        none = discord.AllowedMentions.none()
        try:
            if not self._messages:
                await self._adopt_existing(channel)
            current = list(self._messages)
            out = []
            for i, view in enumerate(pages):
                msg = current[i] if i < len(current) else None
                if msg is not None:
                    try:
                        await msg.edit(view=view, allowed_mentions=none)
                        out.append(msg)
                        continue
                    except discord.NotFound:
                        pass                          # someone deleted it: post a fresh one in its place
                out.append(await channel.send(view=view, allowed_mentions=none))
            for extra in current[len(pages):]:        # the board got shorter
                try:
                    await extra.delete()
                except discord.HTTPException:
                    pass
            self._messages = out
            return True
        except discord.HTTPException:
            self._messages = []                       # re-adopt next time; the retry happens on the next look
            logger.warning("[clone-health] couldn't update the board", exc_info=True)
            return False


async def setup(bot: commands.Bot):
    if getattr(bot, "clone_id", None) is not None:
        return                                        # main bot only
    await bot.add_cog(CloneHealthCog(bot))
    logger.info("[clone-health] ✅ CloneHealthCog loaded")
