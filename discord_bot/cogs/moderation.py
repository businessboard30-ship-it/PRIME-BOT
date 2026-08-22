"""
Discord equivalent of the ban/kick/mute/warn subset of handlers/moderation.py.
Per the port spec: reuse the storage layer as-is, but use discord.py's native
Member.kick()/ban()/timeout() instead of trying to map Telegram-specific
concepts (restrict_chat_member permissions objects, ban+unban-as-kick) onto
Discord, which has real kick/timeout primitives Telegram doesn't.

modules/moderation_adapter.py (warns) and modules/moderation_extra.py
(action log) are already keyed by a generic chat_id — guild.id slots in
directly with zero changes needed there.
"""

import logging
from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands
from discord_bot.cogs._dm_support import GuildOnlyCog

from modules import moderation_adapter as mod
from modules import moderation_extra as modx
from discord_bot.cogs._views_moderation import ModActionView, WarnActionView, ModLogsView, ConfirmActionView

logger = logging.getLogger(__name__)

WARN_LIMIT_BEFORE_TIMEOUT = 3
DEFAULT_TIMEOUT_MINUTES = 60  # used for auto-timeout on hitting the warn limit


def _require_perm(interaction: discord.Interaction, perm: str) -> bool:
    """Checks the invoking user's permission in the current channel.

    Uses interaction.permissions (always populated by Discord for any
    command run inside a guild channel) rather than
    interaction.user.guild_permissions, because interaction.user comes
    back as a plain discord.User instead of discord.Member when this app
    is invoked via a user-install context — even while run inside a real
    server channel — which made guild_permissions unreachable for anyone
    using the bot as a personal (user-installed) app, including owners.
    """
    if interaction.guild is None:
        return False
    return getattr(interaction.permissions, perm, False)


async def _deny(interaction: discord.Interaction, perm_name: str):
    msg = f"You need the **{perm_name}** permission to do that."
    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)


class ModerationCog(GuildOnlyCog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ── /kick ────────────────────────────────────────────────────────────
    @app_commands.command(name="kick", description="Kick a member from this server")
    @app_commands.guild_only()
    @app_commands.describe(member="Member to kick", reason="Reason (shown in the audit log)")
    async def kick(self, interaction: discord.Interaction, member: discord.Member, reason: str = "No reason given"):
        if not _require_perm(interaction, "kick_members"):
            await _deny(interaction, "Kick Members")
            return

        async def _do_kick(confirm_interaction: discord.Interaction):
            try:
                await member.kick(reason=reason)
            except discord.Forbidden:
                await confirm_interaction.response.edit_message(
                    content="I don't have permission to kick that member (check role hierarchy).", view=None
                )
                return
            await modx.log_action(interaction.guild_id, "kick", interaction.user.id, target_user_id=member.id, reason=reason)
            await confirm_interaction.response.edit_message(
                content=f"👢 {member.mention} kicked.\nReason: {reason}", view=ModActionView(member.id)
            )

        view = ConfirmActionView(interaction.user.id, _do_kick, confirm_label="Kick")
        await interaction.response.send_message(
            f"⚠️ Kick {member.mention}? Reason: {reason}", view=view, ephemeral=True
        )

    # ── /ban ─────────────────────────────────────────────────────────────
    @app_commands.command(name="ban", description="Ban a member from this server")
    @app_commands.guild_only()
    @app_commands.describe(member="Member to ban", reason="Reason (shown in the audit log)", delete_days="Days of message history to delete (0-7)")
    async def ban(self, interaction: discord.Interaction, member: discord.Member, reason: str = "No reason given", delete_days: app_commands.Range[int, 0, 7] = 0):
        if not _require_perm(interaction, "ban_members"):
            await _deny(interaction, "Ban Members")
            return

        async def _do_ban(confirm_interaction: discord.Interaction):
            try:
                await member.ban(reason=reason, delete_message_days=delete_days)
            except discord.Forbidden:
                await confirm_interaction.response.edit_message(
                    content="I don't have permission to ban that member (check role hierarchy).", view=None
                )
                return
            await modx.log_action(interaction.guild_id, "ban", interaction.user.id, target_user_id=member.id, reason=reason)
            await confirm_interaction.response.edit_message(
                content=f"🔨 {member.mention} banned.\nReason: {reason}", view=ModActionView(member.id)
            )

        view = ConfirmActionView(interaction.user.id, _do_ban, confirm_label="Ban")
        await interaction.response.send_message(
            f"⚠️ Ban {member.mention}? This also deletes {delete_days} day(s) of messages.\nReason: {reason}",
            view=view, ephemeral=True
        )

    # ── /unban ───────────────────────────────────────────────────────────
    @app_commands.command(name="unban", description="Unban a user by ID")
    @app_commands.guild_only()
    @app_commands.describe(user_id="The numeric Discord user ID to unban")
    async def unban(self, interaction: discord.Interaction, user_id: str):
        if not _require_perm(interaction, "ban_members"):
            await _deny(interaction, "Ban Members")
            return
        if not user_id.isdigit():
            await interaction.response.send_message("Usage: `/unban <user_id>` (numeric ID).", ephemeral=True)
            return
        try:
            await interaction.guild.unban(discord.Object(id=int(user_id)), reason=f"Unbanned by {interaction.user.id}")
        except discord.NotFound:
            await interaction.response.send_message("That user isn't banned here.", ephemeral=True)
            return
        except discord.Forbidden:
            await interaction.response.send_message("I don't have permission to unban.", ephemeral=True)
            return
        await modx.log_action(interaction.guild_id, "unban", interaction.user.id, target_user_id=int(user_id), reason="")
        await interaction.response.send_message(f"✅ Unbanned user {user_id}.", view=ModActionView(int(user_id)))

    # ── /timeout (mute) ──────────────────────────────────────────────────
    @app_commands.command(name="timeout", description="Timeout (mute) a member")
    @app_commands.guild_only()
    @app_commands.describe(member="Member to timeout", minutes="Duration in minutes (max 40320 / 28 days)", reason="Reason")
    async def timeout(self, interaction: discord.Interaction, member: discord.Member, minutes: app_commands.Range[int, 1, 40320], reason: str = "No reason given"):
        if not _require_perm(interaction, "moderate_members"):
            await _deny(interaction, "Timeout Members")
            return
        try:
            await member.timeout(timedelta(minutes=minutes), reason=reason)
        except discord.Forbidden:
            await interaction.response.send_message(
                "I don't have permission to timeout that member (check role hierarchy).", ephemeral=True
            )
            return
        await modx.log_action(interaction.guild_id, "timeout", interaction.user.id, target_user_id=member.id, reason=f"{minutes}min: {reason}")
        await interaction.response.send_message(f"🔇 {member.mention} timed out for {minutes} min.\nReason: {reason}", view=ModActionView(member.id))

    # ── /untimeout (unmute) ──────────────────────────────────────────────
    @app_commands.command(name="untimeout", description="Remove an active timeout from a member")
    @app_commands.guild_only()
    @app_commands.describe(member="Member to restore")
    async def untimeout(self, interaction: discord.Interaction, member: discord.Member):
        if not _require_perm(interaction, "moderate_members"):
            await _deny(interaction, "Timeout Members")
            return
        try:
            await member.timeout(None, reason=f"Timeout removed by {interaction.user.id}")
        except discord.Forbidden:
            await interaction.response.send_message("I don't have permission to do that.", ephemeral=True)
            return
        await modx.log_action(interaction.guild_id, "untimeout", interaction.user.id, target_user_id=member.id, reason="")
        await interaction.response.send_message(f"🔊 {member.mention} timeout removed.", view=ModActionView(member.id))

    # ── /warn ────────────────────────────────────────────────────────────
    @app_commands.command(name="warn", description="Warn a member")
    @app_commands.guild_only()
    @app_commands.describe(member="Member to warn", reason="Reason")
    async def warn(self, interaction: discord.Interaction, member: discord.Member, reason: str = "No reason given"):
        if not _require_perm(interaction, "moderate_members"):
            await _deny(interaction, "Timeout Members")
            return
        count = await mod.add_warn(member.id, interaction.guild_id, interaction.user.id, reason)
        await modx.log_action(interaction.guild_id, "warn", interaction.user.id, target_user_id=member.id, reason=reason)

        if count >= WARN_LIMIT_BEFORE_TIMEOUT:
            try:
                await member.timeout(timedelta(minutes=DEFAULT_TIMEOUT_MINUTES), reason=f"Reached {count}/{WARN_LIMIT_BEFORE_TIMEOUT} warns")
                await interaction.response.send_message(
                    f"⚠️ {member.mention} warned ({count}/{WARN_LIMIT_BEFORE_TIMEOUT}) and timed out "
                    f"for {DEFAULT_TIMEOUT_MINUTES} min for reaching the warn limit.\nReason: {reason}",
                    view=WarnActionView(member)
                )
            except discord.Forbidden:
                await interaction.response.send_message(
                    f"⚠️ {member.mention} warned ({count}/{WARN_LIMIT_BEFORE_TIMEOUT}) but I couldn't time them out "
                    f"— check my role hierarchy.\nReason: {reason}",
                    view=WarnActionView(member)
                )
            return

        await interaction.response.send_message(
            f"⚠️ {member.mention} warned ({count}/{WARN_LIMIT_BEFORE_TIMEOUT}).\nReason: {reason}",
            view=WarnActionView(member)
        )

    # ── /unwarn ──────────────────────────────────────────────────────────
    @app_commands.command(name="unwarn", description="Clear all warns for a member")
    @app_commands.guild_only()
    @app_commands.describe(member="Member to clear")
    async def unwarn(self, interaction: discord.Interaction, member: discord.Member):
        if not _require_perm(interaction, "moderate_members"):
            await _deny(interaction, "Timeout Members")
            return

        current = await mod.get_warn_count(member.id, interaction.guild_id)
        if current == 0:
            await interaction.response.send_message(f"{member.mention} has no warns to clear.", ephemeral=True)
            return

        async def _do_unwarn(confirm_interaction: discord.Interaction):
            await mod.clear_warns(member.id, interaction.guild_id)
            await modx.log_action(interaction.guild_id, "unwarn", interaction.user.id, target_user_id=member.id, reason="")
            await confirm_interaction.response.edit_message(
                content=f"✅ Cleared warns for {member.mention}.", view=WarnActionView(member)
            )

        view = ConfirmActionView(interaction.user.id, _do_unwarn, confirm_label="Clear warns")
        await interaction.response.send_message(
            f"⚠️ Clear all {current} warn(s) for {member.mention}? This can't be undone.", view=view, ephemeral=True
        )

    # ── /warns ───────────────────────────────────────────────────────────
    async def send_warns(self, interaction: discord.Interaction, target: discord.Member, ephemeral: bool = False):
        count = await mod.get_warn_count(target.id, interaction.guild_id)
        await interaction.response.send_message(
            f"{target.mention} has {count}/{WARN_LIMIT_BEFORE_TIMEOUT} warns.",
            ephemeral=ephemeral, view=WarnActionView(target)
        )

    @app_commands.command(name="warns", description="Check a member's warn count (defaults to yourself)")
    @app_commands.guild_only()
    @app_commands.describe(member="Member to check (optional)")
    async def warns(self, interaction: discord.Interaction, member: discord.Member = None):
        await self.send_warns(interaction, member or interaction.user, ephemeral=(member is None))

    # ── /modlogs ─────────────────────────────────────────────────────────
    async def send_modlogs(self, interaction: discord.Interaction, limit: int = 10, page: int = 0, edit: bool = False):
        if not _require_perm(interaction, "moderate_members"):
            await _deny(interaction, "Timeout Members")
            return
        logs = await modx.get_logs(interaction.guild_id, limit=limit + 1, offset=page * limit)
        has_next = len(logs) > limit
        logs = logs[:limit]
        if not logs:
            embed = discord.Embed(
                description="No moderation actions logged yet." if page == 0 else "No more entries.",
                color=discord.Color.blurple(),
            )
            view = None
        else:
            embed = discord.Embed(title=f"Mod logs — page {page + 1}", color=discord.Color.blurple())
            for entry in logs:
                action = entry.get("action_type", "?")
                target = entry.get("target_user_id")
                reason = entry.get("reason") or "—"
                embed.add_field(name=f"{action} · target {target}", value=reason, inline=False)
            view = ModLogsView(limit, page=page, has_next=has_next)

        # Prev/Next/Refresh on the modlogs message itself edit in place;
        # everything else (the initial /modlogs call, or the "Mod Logs"
        # button on a *different* message like a ban confirmation) sends
        # a fresh ephemeral message instead of overwriting that message.
        if edit:
            await interaction.response.edit_message(embed=embed, view=view)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True, view=view)

    @app_commands.command(name="modlogs", description="Show recent moderation actions in this server")
    @app_commands.guild_only()
    @app_commands.describe(limit="How many actions per page (max 20)")
    async def modlogs(self, interaction: discord.Interaction, limit: app_commands.Range[int, 1, 20] = 10):
        await self.send_modlogs(interaction, limit=limit, page=0, edit=False)


async def setup(bot: commands.Bot):
    await bot.add_cog(ModerationCog(bot))
