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

# Hard cap on voice XP rate regardless of admin setting.
# Keeps voice XP in the same ballpark as text XP (15-25 XP/msg every 60s).
VOICE_XP_RATE_CAP = 25

# Max number of bots in a voice channel that count toward the "not alone"
# minimum (see _is_trackable). Lets one real member earn voice XP solo
# alongside a couple of music/utility bots, without letting someone spam
# extra bots into the channel to bypass the anti-farming check entirely.
VOICE_XP_MAX_BOTS_COUNTED = 2


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
        # Must be unmuted/undeafened — muted or deafened members aren't
        # participating and can idle-farm voice XP without contributing.
        voice_state = member.voice
        if voice_state and (voice_state.self_mute or voice_state.mute or
                            voice_state.self_deaf or voice_state.deaf):
            return False
        # Must not be truly alone — solo voice sitting is easy to farm
        # indefinitely. Bots count toward this minimum too (e.g. a music
        # bot playing along with you), but only up to VOICE_XP_MAX_BOTS_COUNTED
        # so stacking extra bots in the channel can't be used to game the
        # threshold — it's about "something is actually happening in here",
        # not a loophole for infinite bot-farming.
        real_members = [m for m in channel.members if not m.bot]
        bot_count = min(len(channel.members) - len(real_members), VOICE_XP_MAX_BOTS_COUNTED)
        if len(real_members) + bot_count < 2:
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
        # Apply rate cap before multipliers so boosted servers can't bypass it.
        effective_rate = min(config["xp_per_minute"], VOICE_XP_RATE_CAP)
        gained = owed_minutes * effective_rate
        guild_boost = await db.get_active_guild_xp_boost(guild_id, clone_id=clone_id)
        if guild_boost:
            gained = round(gained * float(guild_boost["multiplier"]))
        # Voice XP does NOT reset the text XP cooldown — they run on separate
        # clocks. db.add_xp is called without cooldown_seconds so voice payouts
        # never race with on_message's cooldown check.
        current = await db.get_xp(guild_id, user_id, clone_id=clone_id)
        old_level = leveling.compute_level(current["total_xp"])
        new_total = current["total_xp"] + gained
        new_level = leveling.compute_level(new_total)
        await db.add_xp(guild_id, user_id, gained, new_level, clone_id=clone_id)
        self._minutes_paid[key] = elapsed_minutes

        if new_level > old_level:
            guild = self.bot.get_guild(guild_id)
            member = guild.get_member(user_id) if guild else None
            if guild is None:
                return
            leveling_cog = self.bot.get_cog("LevelingCog")
            # Bug fix: grant roles even when the member already left voice
            # (the most common case — they levelled up in their last minute).
            # Previously this block was guarded by member.voice.channel so
            # a leave-triggered settle never granted the role.
            if member and isinstance(member, discord.Member):
                if leveling_cog:
                    await leveling_cog._grant_level_roles(member, new_level, clone_id=clone_id)
                # Announce in the configured level-up channel, not the voice
                # channel the member may have already left.
                lv_config = await db.get_leveling_config(guild_id, clone_id=clone_id)
                if leveling_cog and lv_config.get("card_style") != "off":
                    announce_ch = await leveling_cog._ensure_announce_channel(guild, lv_config, clone_id=clone_id)
                    if announce_ch is None and member.voice and member.voice.channel:
                        announce_ch = member.voice.channel
                    if announce_ch:
                        try:
                            await announce_ch.send(f"🎉 {member.mention} leveled up to **level {new_level}** (voice XP)!")
                        except discord.Forbidden:
                            pass

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

    @group.command(name="rate", description="Set how much XP is earned per minute in voice (max 25)")
    async def rate(self, interaction: discord.Interaction, xp_per_minute: app_commands.Range[int, 1, 25]):
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
