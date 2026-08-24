"""
Voice-channel XP — adds voice-time XP on top of leveling.py's message XP.
Both add to the SAME discord_xp.total_xp/level, so /rank and /leaderboard
(leveling.py) need zero changes to show voice XP alongside text XP.

Design: track join timestamps in memory per (guild_id, clone_id, user_id).
A 60s background loop walks every tracked member still in voice and awards
xp_per_minute for each full minute elapsed, rather than awarding a lump sum
only on channel-leave — this way XP shows up live in /rank while someone is
still sitting in the channel, and a bot restart only loses at most one
partial minute per member (same trade-off leveling.py makes with its
in-memory cooldown tracker).
"""

import logging

import discord
from discord import app_commands
from discord.ext import commands, tasks
from discord_bot.cogs._dm_support import GuildOnlyCog

from database import db
from modules import leveling

logger = logging.getLogger(__name__)


def _require_perm(interaction: discord.Interaction, perm: str) -> bool:
    if interaction.guild is None:
        return False
    return getattr(interaction.permissions, perm, False)


def _clone_id_of(obj) -> int | None:
    client = obj.client if isinstance(obj, discord.Interaction) else obj
    return getattr(client, "clone_id", None)


class VoiceXPCog(GuildOnlyCog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # (guild_id, clone_id, user_id) -> minutes already paid out since join
        self._minutes_paid: dict = {}
        # (guild_id, clone_id, user_id) -> join timestamp (discord.utils.utcnow())
        self._joined_at: dict = {}
        self._backfilled = False
        self._tick.start()

    @commands.Cog.listener()
    async def on_ready(self):
        # Bot restarts (or reconnects) don't fire on_voice_state_update for
        # members already sitting in a voice channel, so without this
        # they'd earn nothing until their next join/leave/move — silently
        # under-awarding anyone mid-session across a deploy. Backfill once
        # per process; on_ready can fire again on a reconnect, but skipping
        # after the first run avoids clobbering _minutes_paid for sessions
        # already being tracked mid-flight.
        if self._backfilled:
            return
        self._backfilled = True
        clone_id = getattr(self.bot, "clone_id", None)
        for guild in self.bot.guilds:
            try:
                config = await db.get_voice_xp_config(guild.id, clone_id=clone_id)
            except Exception:
                continue
            if not config["enabled"]:
                continue
            for vc in guild.voice_channels:
                for member in vc.members:
                    if self._is_trackable(member, vc, config):
                        key = (guild.id, clone_id, member.id)
                        self._joined_at.setdefault(key, discord.utils.utcnow())
                        self._minutes_paid.setdefault(key, 0)

    def cog_unload(self):
        self._tick.cancel()

    def _is_trackable(self, member: discord.Member, channel: discord.VoiceChannel | None,
                       config: dict) -> bool:
        if channel is None or member.bot:
            return False
        if config["afk_channel_excluded"] and member.guild.afk_channel and channel.id == member.guild.afk_channel.id:
            return False
        return True

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        if member.bot:
            return
        clone_id = getattr(self.bot, "clone_id", None)
        key = (member.guild.id, clone_id, member.id)
        config = await db.get_voice_xp_config(member.guild.id, clone_id=clone_id)
        if not config["enabled"]:
            self._minutes_paid.pop(key, None)
            self._joined_at.pop(key, None)
            return

        now_trackable = self._is_trackable(member, after.channel, config)
        was_trackable = key in self._joined_at

        if now_trackable and not was_trackable:
            self._joined_at[key] = discord.utils.utcnow()
            self._minutes_paid[key] = 0
        elif not now_trackable and was_trackable:
            # Left voice / moved to AFK / disabled — pay out any final
            # partial-to-full minutes, then stop tracking.
            await self._settle(member.guild.id, clone_id, member.id, config)
            self._joined_at.pop(key, None)
            self._minutes_paid.pop(key, None)
        # Moving between two trackable channels: keep the same join clock
        # running rather than resetting it (no reason to punish channel-hopping).

    async def _settle(self, guild_id: int, clone_id: int | None, user_id: int, config: dict):
        key = (guild_id, clone_id, user_id)
        joined_at = self._joined_at.get(key)
        if joined_at is None:
            return
        elapsed_minutes = int((discord.utils.utcnow() - joined_at).total_seconds() // 60)
        already_paid = self._minutes_paid.get(key, 0)
        owed_minutes = elapsed_minutes - already_paid
        if owed_minutes <= 0:
            return
        gained = owed_minutes * config["xp_per_minute"]
        current = await db.get_xp(guild_id, user_id, clone_id=clone_id)
        old_level = leveling.compute_level(current["total_xp"])
        new_total = current["total_xp"] + gained
        new_level = leveling.compute_level(new_total)
        await db.add_xp(guild_id, user_id, gained, new_level, clone_id=clone_id)
        self._minutes_paid[key] = elapsed_minutes

        if new_level > old_level:
            guild = self.bot.get_guild(guild_id)
            member = guild.get_member(user_id) if guild else None
            if member and member.voice and member.voice.channel:
                try:
                    await member.voice.channel.send(f"🎉 {member.mention} leveled up to **level {new_level}** (from time in voice)!")
                except discord.Forbidden:
                    pass
                leveling_cog = self.bot.get_cog("LevelingCog")
                if leveling_cog:
                    await leveling_cog._grant_level_roles(member, new_level, clone_id=clone_id)

    @tasks.loop(seconds=60)
    async def _tick(self):
        # Snapshot keys since _settle can mutate the dicts it reads from.
        for (guild_id, clone_id, user_id) in list(self._joined_at.keys()):
            config = await db.get_voice_xp_config(guild_id, clone_id=clone_id)
            if not config["enabled"]:
                continue
            try:
                await self._settle(guild_id, clone_id, user_id, config)
            except Exception:
                logger.exception(f"[v0] voice XP tick failed for guild={guild_id} user={user_id}")

    @_tick.before_loop
    async def _before_tick(self):
        await self.bot.wait_until_ready()

    group = app_commands.guild_only()(app_commands.Group(name="voicexp", description="Configure voice-channel XP"))

    @group.command(name="settings", description="Show current voice XP settings")
    async def settings(self, interaction: discord.Interaction):
        await interaction.response.defer()
        config = await db.get_voice_xp_config(interaction.guild_id, clone_id=_clone_id_of(interaction))
        await interaction.followup.send(
            f"**Voice XP** — {'enabled' if config['enabled'] else 'disabled'}\n"
            f"Rate: **{config['xp_per_minute']}** XP/minute\n"
            f"AFK channel excluded: **{config['afk_channel_excluded']}**",
            ephemeral=True,
        )

    @group.command(name="toggle", description="Enable or disable voice XP for this server")
    async def toggle(self, interaction: discord.Interaction, enabled: bool):
        await interaction.response.defer(ephemeral=True)
        if not _require_perm(interaction, "manage_guild"):
            await interaction.followup.send("You need the **Manage Server** permission to do that.", ephemeral=True)
            return
        config = await db.set_voice_xp_config(interaction.guild_id, clone_id=_clone_id_of(interaction), enabled=enabled)
        await interaction.followup.send(f"✅ Voice XP is now **{'enabled' if config['enabled'] else 'disabled'}**.", ephemeral=True)

    @group.command(name="rate", description="Set how much XP is earned per minute in voice")
    async def rate(self, interaction: discord.Interaction, xp_per_minute: app_commands.Range[int, 1, 1000]):
        await interaction.response.defer(ephemeral=True)
        if not _require_perm(interaction, "manage_guild"):
            await interaction.followup.send("You need the **Manage Server** permission to do that.", ephemeral=True)
            return
        config = await db.set_voice_xp_config(interaction.guild_id, clone_id=_clone_id_of(interaction), xp_per_minute=xp_per_minute)
        await interaction.followup.send(f"✅ Voice XP rate set to **{config['xp_per_minute']}**/minute.", ephemeral=True)

    @group.command(name="afk", description="Whether time spent in the AFK channel counts")
    async def afk(self, interaction: discord.Interaction, excluded: bool):
        await interaction.response.defer(ephemeral=True)
        if not _require_perm(interaction, "manage_guild"):
            await interaction.followup.send("You need the **Manage Server** permission to do that.", ephemeral=True)
            return
        config = await db.set_voice_xp_config(interaction.guild_id, clone_id=_clone_id_of(interaction), afk_channel_excluded=excluded)
        await interaction.followup.send(f"✅ AFK channel excluded: **{config['afk_channel_excluded']}**.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(VoiceXPCog(bot))
