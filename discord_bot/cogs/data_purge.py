"""Deletes a person's records for a server the moment they leave it (or are kicked/banned from it).

The server-wide wipe on bot removal lives in bot.py's on_guild_remove; both call modules.data_purge."""
import logging

import discord
from discord.ext import commands

from modules import data_purge

logger = logging.getLogger(__name__)


class DataPurgeCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        if member.bot or not data_purge.member_purge_enabled():
            return
        try:
            await data_purge.purge_member(member.guild.id, member.id, getattr(self.bot, "clone_id", None))
        except Exception:
            logger.exception("[purge] leave cleanup failed for member %s in guild %s", member.id, member.guild.id)


async def setup(bot):
    await bot.add_cog(DataPurgeCog(bot))
