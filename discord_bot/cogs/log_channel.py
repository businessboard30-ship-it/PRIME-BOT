# path: discord_bot/cogs/log_channel.py

"""Posts the bot's log lines to LOG_CHANNEL_ID, or to an auto-created private #bot-logs in the support server (main bot only). See modules/log_feed.py."""

import logging

import discord
from discord.ext import commands, tasks

from modules import build_info, log_feed

logger = logging.getLogger(__name__)
MAX_MESSAGES_PER_FLUSH = 3          # a log storm becomes at most 3 messages every 10 s


class LogChannelCog(commands.Cog):
    """LOG_CHANNEL_ID set -> use that channel. Unset -> the MAIN bot makes (or finds) a private #bot-logs in the support server
    and, if it can't, DMs the owner once. Clones never auto-create anything."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.handler = log_feed.install()
        self._announced = False
        self._auto = None              # the auto channel once found/created
        self._auto_failed = False
        self.flush.start()

    async def _auto_channel(self):
        if self._auto is not None or self._auto_failed or getattr(self.bot, "clone_id", None) is not None:
            return self._auto
        from config import DISCORD_CLONE_ADMIN_IDS, DISCORD_SUPPORT_SERVER_ID
        guild = self.bot.get_guild(int(DISCORD_SUPPORT_SERVER_ID or 0))
        reason = "the support server isn't set or the bot isn't in it"
        if guild is not None:
            existing = log_feed.find_existing(guild.text_channels)
            if existing is not None:
                self._auto = existing
                return existing
            try:
                overwrites = {guild.default_role: discord.PermissionOverwrite(view_channel=False),
                              guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True,
                                                                    embed_links=True, attach_files=True)}
                for uid in DISCORD_CLONE_ADMIN_IDS:
                    member = guild.get_member(int(uid))
                    if member is not None:
                        overwrites[member] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
                self._auto = await guild.create_text_channel(log_feed.AUTO_NAME, overwrites=overwrites, topic=log_feed.AUTO_TOPIC,
                                                             reason="Bot log feed (private)")
                return self._auto
            except Exception as exc:
                reason = f"couldn't create it ({type(exc).__name__}); the bot needs Manage Channels in the support server"
        self._auto_failed = True
        for uid in DISCORD_CLONE_ADMIN_IDS:                      # one DM so you know why there is no feed
            try:
                user = await self.bot.fetch_user(int(uid))
                await user.send(f"⚠️ Log feed: {reason}. Set `LOG_CHANNEL_ID` in Railway to a channel id instead.")
            except Exception:
                pass
        return None

    async def _target(self):
        chan_id = log_feed.channel_id()
        if chan_id:
            return self.bot.get_channel(chan_id)
        return await self._auto_channel()

    def cog_unload(self):
        self.flush.cancel()

    def _label(self) -> str:
        cid = getattr(self.bot, "clone_id", None)
        return "main bot" if cid is None else f"clone #{cid}"

    @tasks.loop(seconds=10)
    async def flush(self):
        if not self.bot.is_ready():
            return
        channel = await self._target()
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
        target = log_feed.channel_id() or getattr(self._auto, "id", None)
        if message.author.bot or not target or message.channel.id != target:
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
