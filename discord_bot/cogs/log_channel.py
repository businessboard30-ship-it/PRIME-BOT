# path: discord_bot/cogs/log_channel.py

"""Posts the bot's WARNING/ERROR log lines to the channel in LOG_CHANNEL_ID. See modules/log_feed.py. Off when the variable is unset."""

import logging

import discord
from discord.ext import commands, tasks

from modules import build_info, log_feed

logger = logging.getLogger(__name__)
MAX_MESSAGES_PER_FLUSH = 3          # a log storm becomes at most 3 messages every 10 s


class LogChannelCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.handler = log_feed.install()
        self._announced = False
        self.flush.start()

    def cog_unload(self):
        self.flush.cancel()

    def _label(self) -> str:
        cid = getattr(self.bot, "clone_id", None)
        return "main bot" if cid is None else f"clone #{cid}"

    @tasks.loop(seconds=10)
    async def flush(self):
        chan_id = log_feed.channel_id()
        if not chan_id or not self.bot.is_ready():
            return
        channel = self.bot.get_channel(chan_id)
        if channel is None:
            return
        try:
            if not self._announced:
                self._announced = True
                await channel.send(f"✅ Log feed on for **{self._label()}**. WARNING/ERROR lines and AI-tool activity will appear here. "
                                   f"Type `version` here any time to see which code is running.\n{build_info.summary(self._label())}",
                                   allowed_mentions=discord.AllowedMentions.none())
            lines = self.handler.drain()
            if not lines:
                return
            msgs = log_feed.chunks(lines, self._label())
            for m in msgs[:MAX_MESSAGES_PER_FLUSH]:
                await channel.send(m, allowed_mentions=discord.AllowedMentions.none())
            if len(msgs) > MAX_MESSAGES_PER_FLUSH:
                await channel.send(f"... {len(msgs) - MAX_MESSAGES_PER_FLUSH} more log message(s) skipped.")
        except Exception:
            pass        # never log here (it could feed itself); a missing permission just means no posts

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """`version` typed in the log channel -> which commit this process runs. Ignores bots and every other channel."""
        if message.author.bot or message.channel.id != log_feed.channel_id():
            return
        if (message.content or "").strip().lower() not in ("version", "!version"):
            return
        try:
            await message.channel.send(build_info.summary(self._label()), allowed_mentions=discord.AllowedMentions.none())
        except Exception:
            pass

    @flush.before_loop
    async def _wait(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(LogChannelCog(bot))
