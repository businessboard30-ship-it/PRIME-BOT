# path: discord_bot/cogs/guild_activity.py

"""Feeds modules.guild_activity: stamps servers on messages, commands and voice, saves the stamps every 10 minutes."""

import logging

import discord
from discord.ext import commands, tasks

from database import db
from modules import guild_activity as ga

logger = logging.getLogger(__name__)


class GuildActivityCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._key = ga.setting_key(getattr(bot, "clone_id", None))
        self._loaded = False
        self._flush.start()

    def cog_unload(self):
        self._flush.cancel()

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.guild is not None and not message.author.bot:
            ga.tracker.touch(message.guild.id)

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        ga.tracker.touch(interaction.guild_id)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        if after.channel is not None and not member.bot:
            ga.tracker.touch(member.guild.id)

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild):
        ga.tracker.touch(guild.id)

    @tasks.loop(minutes=10)
    async def _flush(self):
        try:
            if not self._loaded:
                ga.tracker.load(await db.get_global_setting(self._key))
                self._loaded = True
            ids = [g.id for g in self.bot.guilds]
            ga.tracker.seed(ids)
            ga.tracker.forget_missing(ids)
            if ga.tracker.dirty:
                ga.tracker.dirty = False
                await db.set_global_setting(self._key, ga.tracker.dump())
        except Exception:
            ga.tracker.dirty = True
            logger.warning("[guild-activity] save failed, will retry", exc_info=True)

    @_flush.before_loop
    async def _before_flush(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(GuildActivityCog(bot))
