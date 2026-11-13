# path: discord_bot/cogs/role_nudge.py

"""Reminds servers that set up tickets but never made a support role, with a persistent button that makes it.

One reminder per server, ever, posted in the server's mod-log channel (never a DM). The reminder is claimed in the database
BEFORE it is sent (restart- and multi-process-safe) and given back if the post fails. Looks at every server with ONE query,
skips sleeping servers, and sends at most a few per pass so a deploy never floods channels.
"""
from __future__ import annotations

import asyncio
import logging

import discord
from discord.ext import commands, tasks

from database import db
from modules import guild_activity
from modules import role_helper as rh

logger = logging.getLogger(__name__)

MIN_SERVER_AGE_HOURS = 24
MAX_PER_PASS = 5
PASS_HOURS = 6


class RoleNudgeCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        self.nudge_loop.start()

    async def cog_unload(self):
        self.nudge_loop.cancel()

    @tasks.loop(hours=PASS_HOURS)
    async def nudge_loop(self):
        try:
            await self.nudge_pass()
        except Exception:
            logger.exception("[role-nudge] pass failed")

    @nudge_loop.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()
        await asyncio.sleep(420)               # let the bot settle after a start first

    async def _modlog_channel(self, guild: discord.Guild, clone_id):
        try:
            cfg = await db.get_automod_config(guild.id, clone_id)
        except Exception:
            return None
        cid = (cfg or {}).get("log_channel_id")
        channel = guild.get_channel(int(cid)) if cid else None
        if not isinstance(channel, discord.TextChannel):
            return None
        p = channel.permissions_for(guild.me)
        return channel if (p.view_channel and p.send_messages) else None

    async def nudge_pass(self) -> int:
        from discord_bot.cogs._views_role_helper import reminder_view
        clone_id = getattr(self.bot, "clone_id", None)
        candidates = await rh.ticket_candidates(clone_id)
        if not candidates:
            return 0
        sent = 0
        for guild in list(self.bot.guilds):
            if sent >= MAX_PER_PASS:
                break
            if guild.id not in candidates:
                continue
            joined = getattr(guild.me, "joined_at", None)
            if joined is not None:
                from datetime import datetime, timezone
                j = joined if joined.tzinfo else joined.replace(tzinfo=timezone.utc)
                if (datetime.now(timezone.utc) - j).total_seconds() < MIN_SERVER_AGE_HOURS * 3600:
                    continue
            if guild_activity.is_sleeping(guild.id, getattr(guild, "member_count", None)):
                continue                        # asleep: reminded on the first pass after it wakes
            channel = await self._modlog_channel(guild, clone_id)
            if channel is None:
                continue                        # no mod-log channel I can write in: never a DM, look again next pass
            if not await rh.claim(guild.id, clone_id, "ticket"):
                continue
            try:
                await channel.send(view=reminder_view("ticket", guild.id, clone_id),
                                   allowed_mentions=discord.AllowedMentions.none())
                sent += 1
            except (discord.Forbidden, discord.HTTPException):
                await rh.release(guild.id, clone_id, "ticket")
                logger.info("[role-nudge] couldn't post in the mod-log channel of guild %s", guild.id)
            await asyncio.sleep(2)
        if sent:
            logger.info("[role-nudge] sent %s reminder(s)", sent)
        return sent


async def setup(bot):
    await bot.add_cog(RoleNudgeCog(bot))
