# path: discord_bot/cogs/log_channel.py

"""One shared log feed for the main bot and every clone. See modules/log_feed.py and modules/log_feed_store.py.

Destination, in order: LOG_CHANNEL_ID (this process only) -> the shared webhook in the database (works for clones that are not in
the log server) -> the shared channel id in the database. The MAIN bot is the only one that sets it up: it finds/creates a private
#bot-logs in the support server, saves the channel id and a webhook to the database, and if it can't it DMs the owner and asks for
a channel id (reply to the DM with it). Clones never create anything; they just read the database."""

import logging

import aiohttp
import discord
from discord.ext import commands, tasks

from modules import build_info, log_feed, log_feed_store

logger = logging.getLogger(__name__)
MAX_MESSAGES_PER_FLUSH = 3          # a log storm becomes at most 3 messages every 10 s
NONE = discord.AllowedMentions.none()


class LogChannelCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.handler = log_feed.install()
        self._announced = False
        self._channel = None             # main bot: the channel once found/created
        self._gave_up = False            # main bot: auto-setup failed, waiting for the owner's reply
        self._awaiting_id = False
        self._session = None
        self.flush.start()

    def cog_unload(self):
        self.flush.cancel()
        if self._session is not None and not self._session.closed:
            self.bot.loop.create_task(self._session.close())

    # ── identity ──
    def _is_main(self) -> bool:
        return getattr(self.bot, "clone_id", None) is None

    def _label(self) -> str:
        return "main bot" if self._is_main() else f"clone #{self.bot.clone_id}"

    def _owner_ids(self):
        from config import DISCORD_CLONE_ADMIN_IDS
        return {int(x) for x in DISCORD_CLONE_ADMIN_IDS}

    # ── main bot only: make the shared destination ──
    async def _make_webhook(self, channel):
        try:
            hook = await channel.create_webhook(name="Bot logs", reason="Shared log feed for the main bot and all clones")
            return hook.url
        except Exception:
            return None

    async def _adopt(self, channel) -> None:
        """Save this channel (and a webhook for it) as THE shared log destination."""
        self._channel = channel
        _cid, url = await log_feed_store.get(force=True)
        if _cid != channel.id or not url:
            url = await self._make_webhook(channel)
        await log_feed_store.save(channel.id, url)

    async def _setup_main(self):
        cid, url = await log_feed_store.get()
        if cid:
            chan = self.bot.get_channel(cid)
            if chan is not None:
                self._channel = chan
                if not url:
                    await self._adopt(chan)           # channel known but no webhook yet: make one so clones can post
                return
        if self._gave_up:
            return
        from config import DISCORD_SUPPORT_SERVER_ID
        guild = self.bot.get_guild(int(DISCORD_SUPPORT_SERVER_ID or 0))
        reason = "the support server isn't set or the bot isn't in it"
        if guild is not None:
            chan = log_feed.find_existing(guild.text_channels)
            try:
                if chan is None:
                    overwrites = {guild.default_role: discord.PermissionOverwrite(view_channel=False),
                                  guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True,
                                                                        read_message_history=True, embed_links=True,
                                                                        manage_webhooks=True)}
                    for uid in self._owner_ids():
                        member = guild.get_member(uid)
                        if member is not None:
                            overwrites[member] = discord.PermissionOverwrite(view_channel=True, send_messages=True,
                                                                             read_message_history=True)
                    chan = await guild.create_text_channel(log_feed.AUTO_NAME, overwrites=overwrites, topic=log_feed.AUTO_TOPIC,
                                                           reason="Bot log feed (private)")
                await self._adopt(chan)
                return
            except Exception as exc:
                reason = f"couldn't set up #{log_feed.AUTO_NAME} ({type(exc).__name__}); the bot needs Manage Channels (and Manage Webhooks) there"
        self._gave_up = True
        self._awaiting_id = True
        for uid in self._owner_ids():                  # ask once, by DM, for a channel id
            try:
                user = await self.bot.fetch_user(uid)
                await user.send(f"⚠️ Log feed: {reason}.\nReply to this message with the **channel ID** where logs should go "
                                "(right-click the channel > Copy Channel ID; the bot must be able to post there). "
                                "I'll save it, and the main bot and all clones will use it.")
            except Exception:
                pass

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return
        # 1) the owner answers the DM with a channel id (main bot only, only while we are waiting)
        if (self._is_main() and self._awaiting_id and message.guild is None and message.author.id in self._owner_ids()):
            cid = log_feed_store.parse_channel_id(message.content)
            if cid is None:
                return
            chan = self.bot.get_channel(cid)
            if chan is None or not hasattr(chan, "send"):
                await message.channel.send("I can't see that channel. Make sure the bot is in that server and can view it, then send the ID again.")
                return
            try:
                await self._adopt(chan)
                self._awaiting_id = False
                self._announced = False                     # announce in the new channel
                await message.channel.send(f"✅ Saved. Logs from the main bot and all clones now go to #{chan.name}.")
            except Exception as exc:
                await message.channel.send(f"Couldn't save that ({type(exc).__name__}). Check the bot's permissions there.")
            return
        # 2) `version` typed in the log channel -> which commit this process runs
        cid, _ = await log_feed_store.get()
        target = log_feed.channel_id() or cid
        if (target and message.channel.id == target and (message.content or "").strip().lower() in ("version", "!version")):
            try:
                await message.channel.send(build_info.summary(self._label()), allowed_mentions=NONE)
            except Exception:
                pass

    # ── every process: send ──
    async def _webhook(self, url):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return discord.Webhook.from_url(url, session=self._session)

    async def _send(self, text: str) -> bool:
        """Webhook first (reaches clones that aren't in the log server), then the channel. True if it was delivered."""
        own = log_feed.channel_id()
        cid, url = await log_feed_store.get()
        if own is None and url:
            try:
                await (await self._webhook(url)).send(text, username=self._label()[:80], allowed_mentions=NONE)
                return True
            except discord.NotFound:
                await log_feed_store.forget_webhook()          # deleted on Discord; the main bot will make a new one
            except Exception:
                pass
        chan = self.bot.get_channel(own or cid) if (own or cid) else None
        if chan is None and self._is_main():
            chan = self._channel
        if chan is None:
            return False
        try:
            await chan.send(text, allowed_mentions=NONE)
            return True
        except Exception:
            return False

    @tasks.loop(seconds=10)
    async def flush(self):
        if not self.bot.is_ready():
            return
        try:
            if self._is_main() and log_feed.channel_id() is None:
                await self._setup_main()
            if not self._announced:
                ok = await self._send(f"✅ Log feed on for **{self._label()}**. WARNING/ERROR lines and AI-tool activity appear here; "
                                      f"type `version` for the running code.\n{build_info.summary(self._label())}")
                self._announced = ok
                if not ok:
                    return
            lines = self.handler.drain()
            if not lines:
                return
            msgs = log_feed.chunks(lines, self._label())
            for m in msgs[:MAX_MESSAGES_PER_FLUSH]:
                await self._send(m)
            if len(msgs) > MAX_MESSAGES_PER_FLUSH:
                await self._send(f"... {len(msgs) - MAX_MESSAGES_PER_FLUSH} more log message(s) from {self._label()} skipped.")
        except Exception:
            pass        # never log here (it could feed itself)

    @flush.before_loop
    async def _wait(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(LogChannelCog(bot))
