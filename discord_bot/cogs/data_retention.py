"""Runs the timed purges (modules/data_retention.py). The short flood purge runs in every bot process; the daily purges
run only on the main bot."""
import logging

from discord.ext import commands, tasks

from modules import data_retention as dr

logger = logging.getLogger(__name__)


class DataRetentionCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.flood_loop.start()
        self.daily_loop.start()

    def cog_unload(self):
        self.flood_loop.cancel()
        self.daily_loop.cancel()

    def _is_main(self) -> bool:
        return not getattr(self.bot, "clone_id", None)

    @tasks.loop(minutes=5)
    async def flood_loop(self):
        try:
            await dr.purge_flood()
        except Exception:
            logger.exception("[retention] flood purge failed")

    @tasks.loop(hours=24)
    async def daily_loop(self):
        if not self._is_main():
            return
        try:
            logger.info("[retention] daily purge: %s", await dr.purge_daily())
        except Exception:
            logger.exception("[retention] daily purge failed")

    @flood_loop.before_loop
    @daily_loop.before_loop
    async def _wait(self):
        await self.bot.wait_until_ready()


async def setup(bot):
    await bot.add_cog(DataRetentionCog(bot))
