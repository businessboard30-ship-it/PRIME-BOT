# path: discord_bot/cogs/join_gate.py

"""
Join gate listener: catches brand-new accounts and default-avatar joiners.

Configured only from the Server Owners Panel (/serversetup -> Moderation -> Join gate & Scam Shield),
so it costs no slash-command slot. Per join it does one cached read (60s), no per-message work.

When a joiner is caught it always posts an alert in the server's mod-log channel (if one is set).
With the 'kick' action it also kicks them first. Bots are never touched.
"""

from __future__ import annotations

import logging
import time
from typing import Dict, Optional, Tuple

import discord
from discord.ext import commands

from database import db
from discord_bot import perm_check
from modules import join_gate as jg

logger = logging.getLogger(__name__)

CACHE_SECONDS = 60
_cache: Dict[Tuple[int, Optional[int]], Tuple[float, dict]] = {}


def invalidate(guild_id: int, clone_id: Optional[int] = None) -> None:
    """Called by the panel right after a change so it takes effect immediately."""
    _cache.pop((guild_id, clone_id), None)


async def _config(guild_id: int, clone_id: Optional[int]) -> dict:
    key = (guild_id, clone_id)
    now = time.monotonic()
    hit = _cache.get(key)
    if hit and hit[0] > now:
        return hit[1]
    cfg = await db.get_join_gate_config(guild_id, clone_id=clone_id)
    if len(_cache) > 5000:
        _cache.clear()
    _cache[key] = (now + CACHE_SECONDS, cfg)
    return cfg


async def _log_channel(guild: discord.Guild, clone_id: Optional[int]):
    try:
        am = await db.get_automod_config(guild.id, clone_id=clone_id)
        ch_id = am.get("log_channel_id")
        return guild.get_channel(int(ch_id)) if ch_id else None
    except Exception:
        logger.debug("[join-gate] couldn't read the log channel", exc_info=True)
        return None


class JoinGateCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if member.bot:
            return
        clone_id = getattr(self.bot, "clone_id", None)
        guild = member.guild
        try:
            cfg = await _config(guild.id, clone_id)
            reasons = jg.reasons_for(member.created_at, member.avatar is not None, cfg)
            if not reasons:
                return
            await self._act(member, cfg, reasons, clone_id)
        except Exception:
            logger.exception("[join-gate] failed while checking a joiner in guild %s", guild.id)

    async def _act(self, member: discord.Member, cfg: dict, reasons: list, clone_id: Optional[int]) -> None:
        guild = member.guild
        kicked: Optional[bool] = None
        if cfg.get("action") == "kick":
            kicked = False
            me = guild.me
            if me is None or not me.guild_permissions.kick_members or member.top_role >= me.top_role:
                perm_check.flag(guild.id, clone_id, "join_gate",
                                "The join gate couldn't kick someone — give me **Kick Members** and keep my role "
                                "above the roles new members get.")
            else:
                try:
                    await member.kick(reason="[join gate] " + "; ".join(reasons))
                    kicked = True
                    perm_check.clear(guild.id, clone_id, "join_gate")
                except (discord.Forbidden, discord.HTTPException):
                    perm_check.flag(guild.id, clone_id, "join_gate",
                                    f"The join gate couldn't kick someone. {perm_check.forbidden_hint(guild, 'kick_members')}")
        quarantined: Optional[bool] = None
        if cfg.get("action") == "quarantine":
            quarantined = False
            try:
                from modules import server_panel_quarantine as spq
                quarantined, why = await spq.quarantine_member(
                    guild, clone_id, guild.me, member, "[join gate] " + "; ".join(reasons))
                if quarantined:
                    perm_check.clear(guild.id, clone_id, "join_gate")
                else:
                    perm_check.flag(guild.id, clone_id, "join_gate",
                                    f"The join gate couldn't quarantine someone: {why}")
            except Exception:
                logger.exception("[join-gate] quarantine failed in guild %s", guild.id)
        try:
            await db.bump_join_gate_blocked(guild.id, clone_id=clone_id)
        except Exception:
            logger.exception("[join-gate] counter bump failed for guild %s", guild.id)

        ch = await _log_channel(guild, clone_id)
        if ch is None:
            return
        if quarantined is not None:
            outcome = ("🔒 quarantined — staff can release them in the panel" if quarantined
                       else "⚠️ couldn't quarantine (set a quarantine role and check my permissions)")
        else:
            outcome = {True: "👢 kicked", False: "⚠️ couldn't kick (check my permissions)", None: "🔔 let in — staff alerted"}[kicked]
        embed = discord.Embed(title="🚪 Join gate caught a new member", color=discord.Color.orange())
        embed.add_field(name="Member", value=f"{member} (`{member.id}`)", inline=False)
        embed.add_field(name="Why", value="\n".join(f"• {r}" for r in reasons)[:1000], inline=False)
        embed.add_field(name="What happened", value=outcome, inline=False)
        try:
            await ch.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            logger.debug("[join-gate] couldn't post the alert", exc_info=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(JoinGateCog(bot))
