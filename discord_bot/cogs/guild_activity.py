# path: discord_bot/cogs/guild_activity.py

"""Feeds modules.guild_activity: stamps servers on messages, commands and voice, saves the stamps every 10 minutes.

Also (main bot only) keeps a private #bot-sleep-log channel in the support server, created on demand, where it posts
what went to sleep or woke up (loops from modules.sleep_events, servers that turned dormant or active again) and a
daily summary."""

import logging

import discord
from discord.ext import commands, tasks

from database import db
from modules import guild_activity as ga
from modules import sleep_events as se

logger = logging.getLogger(__name__)

CHANNEL_NAME = "bot-sleep-log"
CHANNEL_KEY = "sleep_log_channel_id"
NO_MENTIONS = discord.AllowedMentions.none()


class GuildActivityCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._key = ga.setting_key(getattr(bot, "clone_id", None))
        self._loaded = False
        self._dormant_prev = None       # None until the first pass: that one is silent
        self._channel_id = 0
        self._digest_started = False
        self._flush.start()
        if self._is_main():
            self._post_loop.start()
            self._digest_loop.start()

    def _is_main(self) -> bool:
        return getattr(self.bot, "clone_id", None) is None and se.enabled()

    def cog_unload(self):
        self._flush.cancel()
        self._post_loop.cancel()
        self._digest_loop.cancel()

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
            self._report_dormancy()
            if ga.tracker.dirty:
                ga.tracker.dirty = False
                await db.set_global_setting(self._key, ga.tracker.dump())
        except Exception:
            ga.tracker.dirty = True
            logger.warning("[guild-activity] save failed, will retry", exc_info=True)

    def _report_dormancy(self) -> None:
        days = ga.dormant_days()
        now_dormant = {g.id for g in self.bot.guilds if ga.tracker.is_dormant(g.id)}
        prev, self._dormant_prev = self._dormant_prev, now_dormant
        if prev is None:
            return
        for gid in now_dormant - prev:
            g = self.bot.get_guild(gid)
            se.emit("dormant", str(gid), f"🌙 **{g.name if g else gid}** went dormant: no activity for {days} days, "
                                          "so cat spawns and Ship are paused there.")
        for gid in prev - now_dormant:
            g = self.bot.get_guild(gid)
            se.emit("active", str(gid), f"☀️ **{g.name if g else gid}** is active again: cat spawns and Ship are back on.")

    async def _log_channel(self):
        """The private log channel in the support server: saved id, then by name, else created. None when not possible."""
        from config import DISCORD_SUPPORT_SERVER_ID
        guild = self.bot.get_guild(DISCORD_SUPPORT_SERVER_ID) if DISCORD_SUPPORT_SERVER_ID else None
        if guild is None:
            return None
        saved = self._channel_id or int(await db.get_global_setting(CHANNEL_KEY) or 0)
        ch = guild.get_channel(saved) if saved else None
        if ch is None:
            ch = discord.utils.get(guild.text_channels, name=CHANNEL_NAME)
        created = False
        if ch is None:
            me = guild.me
            if me is None or not me.guild_permissions.manage_channels:
                logger.warning("[sleep-log] no Manage Channels permission in the support server, cannot create #%s", CHANNEL_NAME)
                return None
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(view_channel=False),
                me: discord.PermissionOverwrite(view_channel=True, send_messages=True, embed_links=True, read_message_history=True),
            }
            ch = await guild.create_text_channel(
                CHANNEL_NAME, overwrites=overwrites, reason="Bot sleep log (what was put to sleep or woke up)",
                topic="What the bot put to sleep to save resources, and what woke up again: idle loops, dormant servers, daily summary.")
            created = True
        if ch.id != saved:
            await db.set_global_setting(CHANNEL_KEY, str(ch.id))
        self._channel_id = ch.id
        if created:
            await ch.send(embed=discord.Embed(
                title="💤 Bot sleep log",
                description=("The bot posts here when it puts something to sleep to save resources, and when it wakes up again.\n\n"
                             "😴 a background job found nothing to do and now checks less often\n"
                             "☀️ it found work and is back at full speed\n"
                             "🌙 a server was silent for a long time, so optional fun features (cat spawns, Ship) pause there\n"
                             "📊 a summary once a day\n\n"
                             "Important jobs (giveaways, scheduled messages, anti-raid, verification) wake instantly when there is work; "
                             "sleeping only means checking the database less often."),
                color=0x5865F2), allowed_mentions=NO_MENTIONS)
        return ch

    @tasks.loop(minutes=2)
    async def _post_loop(self):
        try:
            if not se.pending():
                return
            ch = await self._log_channel()
            if ch is None:
                return          # events wait (bounded) until the channel can exist
            lines = [text for _kind, text in se.drain(15)]
            if lines:
                await ch.send(embed=discord.Embed(description="\n".join(lines)[:3900], color=0x2B2D31),
                              allowed_mentions=NO_MENTIONS)
        except Exception:
            logger.warning("[sleep-log] post failed", exc_info=True)

    @tasks.loop(hours=24)
    async def _digest_loop(self):
        try:
            if not self._digest_started:
                self._digest_started = True      # the first tick is startup, not a day
                return
            ch = await self._log_channel()
            if ch is None:
                return
            c = se.reset_digest()
            dormant = len(self._dormant_prev or ())
            total = len(self.bot.guilds)
            await ch.send(embed=discord.Embed(
                title="📊 Daily sleep summary",
                description=(f"Jobs that went to sleep: **{c['sleep']}**, woke up: **{c['wake']}** "
                             f"(repeat notices held back: {c['suppressed']})\n"
                             f"Servers that went dormant: **{c['dormant']}**, came back: **{c['active']}**\n"
                             f"Dormant right now: **{dormant}** of **{total}** servers"),
                color=0x57F287), allowed_mentions=NO_MENTIONS)
        except Exception:
            logger.warning("[sleep-log] digest failed", exc_info=True)

    @_post_loop.before_loop
    async def _before_post(self):
        await self.bot.wait_until_ready()

    @_digest_loop.before_loop
    async def _before_digest(self):
        await self.bot.wait_until_ready()

    @_flush.before_loop
    async def _before_flush(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(GuildActivityCog(bot))
