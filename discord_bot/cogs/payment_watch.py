"""Runs modules/payment_watch every 30 minutes on the main bot and DMs the owner when payments turn red or recover."""
import asyncio
import logging

from discord.ext import commands, tasks

from modules import payment_watch

logger = logging.getLogger(__name__)

STARTUP_DELAY_SECONDS = 120


class PaymentWatchCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Only the main bot watches: clones share the same database and would send the owner duplicate DMs.
        if getattr(bot, "clone_id", None) is None:
            self._loop.start()

    def cog_unload(self):
        self._loop.cancel()

    @tasks.loop(minutes=30)
    async def _loop(self):
        try:
            _issues, text, send = await payment_watch.run_check()
            if send:
                from gumroad_payments import _alert_owner
                await _alert_owner(text)
            from modules import payment_recover
            done = await payment_recover.recover_missed_sales()
            fixed = [r for r, m in done.items() if m == "ok"]
            if fixed:
                from gumroad_payments import _alert_owner
                await _alert_owner(f"✅ Recovered {len(fixed)} paid Gumroad sale(s) that missed their ping and unlocked them: "
                                   + ", ".join(f"`{r}`" for r in fixed[:10]))
        except Exception:
            logger.exception("[payment-watch] check failed")

    @_loop.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()
        await asyncio.sleep(STARTUP_DELAY_SECONDS)


async def setup(bot: commands.Bot):
    await bot.add_cog(PaymentWatchCog(bot))
