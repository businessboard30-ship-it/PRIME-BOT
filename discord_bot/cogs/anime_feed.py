"""Starts and stops the shared anime feed worker (modules/anime_feed_worker.py) with the bot. No commands."""

from discord.ext import commands

from modules.anime_feed_worker import Worker


class AnimeFeedCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.worker = Worker(bot, getattr(bot, "clone_id", None))

    async def cog_load(self):
        self.worker.start()

    async def cog_unload(self):
        await self.worker.stop()


async def setup(bot):
    await bot.add_cog(AnimeFeedCog(bot))
