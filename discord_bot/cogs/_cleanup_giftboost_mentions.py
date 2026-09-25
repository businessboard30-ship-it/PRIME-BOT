# path: discord_bot/cogs/_cleanup_giftboost_mentions.py

"""
One-time cleanup: the AI chat used to sometimes suggest the owner-only
/levelrole giftboost subcommand as if any user could run it. That's now
blocked going forward (modules.ai_features BOT_RULES #9 +
scrub_giftboost_mention). This cog deletes any of those messages already
sent, on the first on_ready after this deploy.

There's no per-message channel log for AI chat replies (only each
session's last_bot_message_id), so this can only find/delete a message
that was still the last one in its session — best-effort, not exhaustive.
For a DM session, the user's DM channel is tried directly; for a guild
session, every text channel in that guild is tried until the message is
found (message IDs aren't unique enough to fetch without knowing the
channel).
"""

import logging

import discord
from discord.ext import commands

from database import db

logger = logging.getLogger(__name__)


class CleanupGiftboostMentionsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._done = False

    @commands.Cog.listener()
    async def on_ready(self):
        if self._done:
            return
        self._done = True
        try:
            candidates = await db.get_giftboost_mention_candidates()
        except Exception:
            logger.exception("[giftboost-cleanup] failed to query candidates")
            return
        if not candidates:
            return

        deleted = 0
        for row in candidates:
            message_id = row["last_bot_message_id"]
            guild_id = row.get("guild_id")
            user_id = row["user_id"]
            try:
                if guild_id:
                    guild = self.bot.get_guild(guild_id)
                    if guild is None:
                        continue
                    msg = await self._find_in_guild(guild, message_id)
                else:
                    msg = await self._find_in_dm(user_id, message_id)
                if msg is None:
                    continue
                await msg.delete()
                deleted += 1
            except (discord.Forbidden, discord.HTTPException, discord.NotFound):
                continue
            except Exception:
                logger.exception("[giftboost-cleanup] error handling one candidate")
                continue
        if deleted:
            logger.info(f"[giftboost-cleanup] deleted {deleted} message(s) suggesting /levelrole giftboost")

    async def _find_in_guild(self, guild: discord.Guild, message_id: int):
        for channel in guild.text_channels:
            try:
                return await channel.fetch_message(message_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                continue
        return None

    async def _find_in_dm(self, user_id: int, message_id: int):
        user = self.bot.get_user(user_id)
        if user is None:
            try:
                user = await self.bot.fetch_user(user_id)
            except discord.HTTPException:
                return None
        dm = user.dm_channel or await user.create_dm()
        try:
            return await dm.fetch_message(message_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return None


async def setup(bot: commands.Bot):
    await bot.add_cog(CleanupGiftboostMentionsCog(bot))
