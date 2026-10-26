"""Starts and stops the shared anime feed worker (modules/anime_feed_worker.py) with the bot, and sends the one-time anime setup
reminder to the mod-log channel of servers that never set the feed up (modules/anime_feed_nudge.py). No commands."""

import asyncio
import logging

import discord
from discord.ext import commands, tasks

from database import db
from modules import anime_feed as af
from modules import anime_feed_nudge as nudge
from modules.anime_feed_worker import Worker

logger = logging.getLogger(__name__)


class AnimeFeedCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.worker = Worker(bot, getattr(bot, "clone_id", None))

    async def cog_load(self):
        self.worker.start()
        self.nudge_loop.start()

    async def cog_unload(self):
        self.nudge_loop.cancel()
        await self.worker.stop()

    @tasks.loop(hours=nudge.PASS_HOURS)
    async def nudge_loop(self):
        try:
            await self.nudge_pass()
        except Exception:
            logger.exception("[anime-nudge] pass failed")

    @nudge_loop.before_loop
    async def _before_nudge(self):
        await self.bot.wait_until_ready()
        await asyncio.sleep(300)          # let the bot settle after a start before looking at servers

    async def nudge_pass(self) -> int:
        """Sends at most MAX_PER_PASS reminders. Returns how many went out."""
        clone_id = getattr(self.bot, "clone_id", None)
        done = await nudge.nudged_guild_ids(clone_id)
        sent = 0
        for guild in list(self.bot.guilds):
            if sent >= nudge.MAX_PER_PASS:
                break
            if guild.id in done or not nudge.old_enough(getattr(guild.me, "joined_at", None)):
                continue
            channel = await self._modlog_channel(guild, clone_id)
            if channel is None:
                continue                  # no mod-log channel (or none I can write in): never a DM, look again next pass
            cfg = await af.get_config(guild.id, clone_id)
            if nudge.is_set_up(cfg.enabled, cfg.channel_id):
                continue
            if not await nudge.claim(guild.id, clone_id):
                continue
            try:
                from discord_bot.cogs._views_anime_nudge import nudge_view
                name = self.bot.user.display_name if self.bot.user else "I"
                await channel.send(view=nudge_view(name, guild.id), allowed_mentions=discord.AllowedMentions.none())
                sent += 1
            except (discord.Forbidden, discord.HTTPException):
                await nudge.release(guild.id, clone_id)
                logger.info("[anime-nudge] couldn't post in the mod-log channel of guild %s", guild.id)
            await asyncio.sleep(2)
        if sent:
            logger.info("[anime-nudge] sent %s reminder(s)", sent)
        return sent

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


async def setup(bot):
    await bot.add_cog(AnimeFeedCog(bot))
