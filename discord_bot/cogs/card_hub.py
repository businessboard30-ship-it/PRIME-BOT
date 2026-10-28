# path: discord_bot/cogs/card_hub.py

"""
Card hub cog. Adds NO commands: the card Store / collection / profile / trading screens open from the leaderboard's
"Cards" button (see _views_card_hub.py). This cog only hosts the background sweep that keeps the economy tidy:

  * expire_rentals  - removes rental copies past their expiry (never one that is inside a pending trade)
  * expire_trades   - marks pending trade offers past their TTL as expired and unlocks their cards

Both are idempotent, single-statement-per-row, and cheap when there is nothing to do, so a missed or repeated run is
harmless. The sweep is database-global, so it runs on the main bot only (clones would just repeat it).
"""

import logging

from discord.ext import commands, tasks

from modules import card_economy, card_trade

logger = logging.getLogger(__name__)

SWEEP_MINUTES = 5


async def sweep_once() -> tuple:
    """One pass. Never raises: each half is isolated so one failing doesn't stop the other. Returns (rentals, trades)."""
    rentals = trades = 0
    try:
        rentals = await card_economy.expire_rentals()
    except Exception:
        logger.warning("[cards] rental expiry sweep failed", exc_info=True)
    try:
        trades = await card_trade.expire_trades()
    except Exception:
        logger.warning("[cards] trade expiry sweep failed", exc_info=True)
    if rentals or trades:
        logger.info("[cards] sweep: %s rental(s) expired, %s trade offer(s) expired", rentals, trades)
    return rentals, trades


class CardHubCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        if getattr(self.bot, "clone_id", None) is None:
            self.sweep_loop.start()

    async def cog_unload(self):
        self.sweep_loop.cancel()

    @tasks.loop(minutes=SWEEP_MINUTES)
    async def sweep_loop(self):
        await sweep_once()

    @sweep_loop.before_loop
    async def _before_sweep(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(CardHubCog(bot))
