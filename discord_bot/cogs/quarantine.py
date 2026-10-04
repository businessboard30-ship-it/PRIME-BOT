# path: discord_bot/cogs/quarantine.py

"""
Quarantine listener: someone who left while quarantined gets the quarantine role straight back when
they rejoin. Everything else about quarantine lives in the Server Owners Panel
(/serversetup -> Moderation -> Quarantine & lockdown), so this costs no slash-command slot.
"""

from __future__ import annotations

import logging

import discord
from discord.ext import commands

logger = logging.getLogger(__name__)


class QuarantineCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if member.bot:
            return
        try:
            from modules import server_panel_quarantine as spq
            await spq.apply_on_join(member, getattr(self.bot, "clone_id", None))
        except Exception:
            logger.exception("[quarantine] rejoin check failed in guild %s", member.guild.id)


async def setup(bot: commands.Bot):
    await bot.add_cog(QuarantineCog(bot))
