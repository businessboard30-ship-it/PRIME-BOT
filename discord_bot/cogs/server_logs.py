# FULL PATH: PRIME-BOT-main/discord_bot/cogs/server_logs.py

"""
Extended server-activity logging — Server / Channels / Roles / Members /
Moderation / Voice / Invites — each independently toggleable via the
/modlog wizard (discord_bot/cogs/_views_modlog_wizard.py).

Shares discord_automod_config.log_channel_id with automod.py's own basic
logging (message delete/edit, join/leave, filter hits) rather than a
separate table/channel — see database.py's discord_automod_config comment
and _views_modlog_wizard.py's module docstring for why. This cog only reads
the log_*_enabled boolean columns added alongside it; it never touches
automod's filter/action config.

All categories default OFF. Nothing here posts anything until an admin
opts a category in through the /modlog wizard (or the wizard gets
auto-posted the moment a log channel is first chosen — see
maybe_announce_new_log_channel).

Audit-log attribution: Discord's raw gateway events (on_guild_role_delete,
on_guild_channel_delete, on_member_ban, etc.) don't include *who* did it —
just that it happened. _find_audit_actor looks up the most recent matching
audit-log entry (bounded to the last ~10 seconds so it can't misattribute a
change to a stale entry) to answer "by whom". Requires View Audit Log;
silently falls back to "Unknown" if the bot lacks that permission or the
lookup fails for any reason — attribution is a nice-to-have, not a
requirement for the event to be logged at all.
"""

import logging
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

from database import db
from discord_bot.cogs._dm_support import GuildOnlyCog
from discord_bot.cogs._views_modlog_wizard import (
    build_wizard_view as build_modlog_wizard_view,
    remember_wizard_message as remember_modlog_wizard_message,
)

logger = logging.getLogger(__name__)

AUDIT_LOOKUP_WINDOW_SECONDS = 10


def _clone_id_of_bot(bot) -> "int | None":
    return getattr(bot, "clone_id", None)


async def _find_audit_actor(guild: discord.Guild, action: discord.AuditLogAction, target_id: int = None):
    """Best-effort "who did it" lookup. Returns a discord.Member/User, or
    None if it can't be determined (missing permission, nothing recent
    enough, or no matching entry at all)."""
    if not guild.me.guild_permissions.view_audit_log:
        return None
    try:
        async for entry in guild.audit_logs(action=action, limit=5):
            age = (datetime.now(timezone.utc) - entry.created_at).total_seconds()
            if age > AUDIT_LOOKUP_WINDOW_SECONDS:
                break
            if target_id is None or getattr(entry.target, "id", None) == target_id:
                return entry.user
    except (discord.Forbidden, discord.HTTPException):
        return None
    return None


class ServerLogsCog(GuildOnlyCog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def _config(self, guild_id: int):
        clone_id = _clone_id_of_bot(self.bot)
        return await db.get_automod_config(guild_id, clone_id=clone_id)

    async def _send(self, guild: discord.Guild, config: dict, category: str, embed: discord.Embed):
        if not config.get("log_channel_id") or not config.get(f"log_{category}_enabled"):
            return
        channel = guild.get_channel(int(config["log_channel_id"]))
        if channel is None:
            return
        try:
            await channel.send(embed=embed)
        except (discord.Forbidden, discord.HTTPException):
            pass

    @staticmethod
    def _actor_footer(embed: discord.Embed, actor) -> None:
        if actor is not None:
            embed.set_footer(text=f"By {actor}", icon_url=getattr(actor.display_avatar, "url", None))
        else:
            embed.set_footer(text="Actor unknown (missing audit log access, or too old to attribute)")

    # ── /modlog wizard ───────────────────────────────────────────────────
    @app_commands.guild_only()
    @app_commands.command(name="modlog", description="Set up server activity logging (roles, channels, members, moderation, voice, invites)")
    async def modlog(self, interaction: discord.Interaction):
        await interaction.response.defer()
        if not getattr(interaction.permissions, "manage_guild", False):
            await interaction.followup.send("You need the **Manage Server** permission to use this.", ephemeral=True)
            return
        clone_id = _clone_id_of_bot(interaction.client)
        config = await db.get_automod_config(interaction.guild_id, clone_id=clone_id)
        view = build_modlog_wizard_view(interaction.guild_id, clone_id, interaction.user.id, config)
        await interaction.followup.send(view=view)
        sent = await interaction.original_response()
        await remember_modlog_wizard_message(interaction.guild_id, clone_id, interaction.user.id, sent.channel.id, sent.id)

    # ── Server ───────────────────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_guild_update(self, before: discord.Guild, after: discord.Guild):
        config = await self._config(after.id)
        if not config.get("log_server_enabled"):
            return
        changes = []
        if before.name != after.name:
            changes.append(f"**Name:** {before.name} → {after.name}")
        if before.icon != after.icon:
            changes.append("**Icon** changed")
        if before.verification_level != after.verification_level:
            changes.append(f"**Verification level:** {before.verification_level} → {after.verification_level}")
        if not changes:
            return
        embed = discord.Embed(title="⚙️ Server settings updated", description="\n".join(changes), color=discord.Color.blurple())
        actor = await _find_audit_actor(after, discord.AuditLogAction.guild_update)
        self._actor_footer(embed, actor)
        await self._send(after, config, "server", embed)

    # ── Channels ─────────────────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel: discord.abc.GuildChannel):
        config = await self._config(channel.guild.id)
        if not config.get("log_channels_enabled"):
            return
        actor = await _find_audit_actor(channel.guild, discord.AuditLogAction.channel_create, channel.id)
        embed = discord.Embed(title="➕ Channel created", description=f"{channel.mention} (`{channel.name}`)", color=discord.Color.green())
        self._actor_footer(embed, actor)
        await self._send(channel.guild, config, "channels", embed)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel):
        config = await self._config(channel.guild.id)
        if not config.get("log_channels_enabled"):
            return
        actor = await _find_audit_actor(channel.guild, discord.AuditLogAction.channel_delete, channel.id)
        embed = discord.Embed(title="➖ Channel deleted", description=f"`#{channel.name}`", color=discord.Color.red())
        self._actor_footer(embed, actor)
        await self._send(channel.guild, config, "channels", embed)

    @commands.Cog.listener()
    async def on_guild_channel_update(self, before: discord.abc.GuildChannel, after: discord.abc.GuildChannel):
        config = await self._config(after.guild.id)
        if not config.get("log_channels_enabled"):
            return
        changes = []
        if before.name != after.name:
            changes.append(f"**Name:** {before.name} → {after.name}")
        if getattr(before, "overwrites", None) != getattr(after, "overwrites", None):
            changes.append("**Permission overwrites** changed")
        if not changes:
            return
        actor = await _find_audit_actor(after.guild, discord.AuditLogAction.channel_update, after.id)
        embed = discord.Embed(title="✏️ Channel updated", description=f"{after.mention}\n" + "\n".join(changes), color=discord.Color.gold())
        self._actor_footer(embed, actor)
        await self._send(after.guild, config, "channels", embed)

    # ── Roles ────────────────────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_guild_role_create(self, role: discord.Role):
        config = await self._config(role.guild.id)
        if not config.get("log_roles_enabled"):
            return
        actor = await _find_audit_actor(role.guild, discord.AuditLogAction.role_create, role.id)
        embed = discord.Embed(title="➕ Role created", description=f"{role.mention} (`{role.name}`)", color=discord.Color.green())
        self._actor_footer(embed, actor)
        await self._send(role.guild, config, "roles", embed)

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role):
        config = await self._config(role.guild.id)
        if not config.get("log_roles_enabled"):
            return
        actor = await _find_audit_actor(role.guild, discord.AuditLogAction.role_delete, role.id)
        embed = discord.Embed(title="➖ Role deleted", description=f"`{role.name}`", color=discord.Color.red())
        self._actor_footer(embed, actor)
        await self._send(role.guild, config, "roles", embed)

    @commands.Cog.listener()
    async def on_guild_role_update(self, before: discord.Role, after: discord.Role):
        config = await self._config(after.guild.id)
        if not config.get("log_roles_enabled"):
            return
        changes = []
        if before.name != after.name:
            changes.append(f"**Name:** {before.name} → {after.name}")
        if before.color != after.color:
            changes.append(f"**Color:** {before.color} → {after.color}")
        if before.permissions != after.permissions:
            changes.append("**Permissions** changed")
        if not changes:
            return
        actor = await _find_audit_actor(after.guild, discord.AuditLogAction.role_update, after.id)
        embed = discord.Embed(title="✏️ Role updated", description=f"{after.mention}\n" + "\n".join(changes), color=discord.Color.gold())
        self._actor_footer(embed, actor)
        await self._send(after.guild, config, "roles", embed)

    # ── Members ──────────────────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        config = await self._config(after.guild.id)
        moderation_config = config  # same row, just read once
        # Timeout applied/removed lives here too (communication_disabled_until).
        # The self-timeout marker is consumed unconditionally (even when
        # Moderation logging is off), same reasoning as on_member_remove's
        # self-kick consume below — otherwise it leaks for as long as
        # Moderation logging stays disabled on the guild.
        if before.timed_out_until != after.timed_out_until:
            from discord_bot.cogs._automod_shared_state import consume_self_timeout
            was_self = consume_self_timeout(after.guild.id, after.id)
            if not was_self and moderation_config.get("log_moderation_enabled"):
                actor = await _find_audit_actor(after.guild, discord.AuditLogAction.member_update, after.id)
                if after.timed_out_until:
                    embed = discord.Embed(
                        title="⏱️ Member timed out",
                        description=f"{after.mention} until <t:{int(after.timed_out_until.timestamp())}:F>",
                        color=discord.Color.orange(),
                    )
                else:
                    embed = discord.Embed(title="⏱️ Timeout removed", description=f"{after.mention}", color=discord.Color.teal())
                self._actor_footer(embed, actor)
                await self._send(after.guild, moderation_config, "moderation", embed)

        if not config.get("log_members_enabled"):
            return
        if before.nick != after.nick:
            embed = discord.Embed(
                title="✏️ Nickname changed",
                description=f"{after.mention}\n**Before:** {before.nick or before.name}\n**After:** {after.nick or after.name}",
                color=discord.Color.gold(),
            )
            await self._send(after.guild, config, "members", embed)
        before_roles, after_roles = set(before.roles), set(after.roles)
        if before_roles != after_roles:
            added = after_roles - before_roles
            removed = before_roles - after_roles
            lines = []
            if added:
                lines.append("**Added:** " + ", ".join(r.mention for r in added if not r.is_default()))
            if removed:
                lines.append("**Removed:** " + ", ".join(r.mention for r in removed if not r.is_default()))
            if lines:
                actor = await _find_audit_actor(after.guild, discord.AuditLogAction.member_role_update, after.id)
                embed = discord.Embed(title="🎭 Member roles changed", description=f"{after.mention}\n" + "\n".join(lines), color=discord.Color.blurple())
                self._actor_footer(embed, actor)
                await self._send(after.guild, config, "members", embed)

    @commands.Cog.listener()
    async def on_user_update(self, before: discord.User, after: discord.User):
        # Global event (not per-guild) — fan out to every mutual guild that
        # has Members logging on.
        if before.name == after.name and before.display_avatar == after.display_avatar:
            return
        for guild in self.bot.guilds:
            member = guild.get_member(after.id)
            if member is None:
                continue
            config = await self._config(guild.id)
            if not config.get("log_members_enabled"):
                continue
            changes = []
            if before.name != after.name:
                changes.append(f"**Username:** {before.name} → {after.name}")
            if before.display_avatar != after.display_avatar:
                changes.append("**Avatar** changed")
            embed = discord.Embed(title="👤 Member profile updated", description=f"{member.mention}\n" + "\n".join(changes), color=discord.Color.gold())
            await self._send(guild, config, "members", embed)

    # ── Moderation ───────────────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_member_ban(self, guild: discord.Guild, user):
        config = await self._config(guild.id)
        if not config.get("log_moderation_enabled"):
            return
        actor = await _find_audit_actor(guild, discord.AuditLogAction.ban, user.id)
        embed = discord.Embed(title="🔨 Member banned", description=f"{user.mention if hasattr(user, 'mention') else user}", color=discord.Color.dark_red())
        self._actor_footer(embed, actor)
        await self._send(guild, config, "moderation", embed)

    @commands.Cog.listener()
    async def on_member_unban(self, guild: discord.Guild, user: discord.User):
        config = await self._config(guild.id)
        if not config.get("log_moderation_enabled"):
            return
        actor = await _find_audit_actor(guild, discord.AuditLogAction.unban, user.id)
        embed = discord.Embed(title="🔓 Member unbanned", description=f"{user}", color=discord.Color.green())
        self._actor_footer(embed, actor)
        await self._send(guild, config, "moderation", embed)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        # Covers manual kicks done through Discord's own UI, and kicks
        # issued via the bot's own /kick command (that command only sends
        # an ephemeral confirmation to the invoker — never an embed to the
        # log channel — so it's never a duplicate to log here).
        #
        # The self-kick marker is consumed FIRST, unconditionally (even if
        # Moderation logging is off) — automod.py sets it any time it
        # kicks someone regardless of this cog's config, so leaving it
        # unconsumed here would leak a tiny bit of memory per automod kick
        # for as long as the guild has Moderation logging disabled.
        from discord_bot.cogs._automod_shared_state import consume_self_kick
        if consume_self_kick(member.guild.id, member.id):
            # automod.py's own _enforce() (action=kick) or its
            # raid-protection on_member_join handler already posted a
            # "🛡️ Auto-mod action" embed for this one — skip to avoid a
            # duplicate. Note: the audit log's "kick" actor is always the
            # BOT itself for ANY bot-performed kick (including manual
            # /kick), so checking the actor id alone can't tell these
            # apart — this marker is what actually distinguishes them.
            return
        config = await self._config(member.guild.id)
        if not config.get("log_moderation_enabled"):
            return
        actor = await _find_audit_actor(member.guild, discord.AuditLogAction.kick, member.id)
        if actor is None:
            # No matching audit-log "kick" entry within the lookup window
            # at all — this was a plain leave, not a kick.
            return
        embed = discord.Embed(title="👢 Member kicked", description=f"{member.mention}", color=discord.Color.dark_orange())
        self._actor_footer(embed, actor)
        await self._send(member.guild, config, "moderation", embed)

    # ── Voice ────────────────────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        config = await self._config(member.guild.id)
        if not config.get("log_voice_enabled"):
            return
        if before.channel == after.channel:
            return
        if before.channel is None and after.channel is not None:
            embed = discord.Embed(description=f"🔊 {member.mention} joined {after.channel.mention}", color=discord.Color.green())
        elif before.channel is not None and after.channel is None:
            embed = discord.Embed(description=f"🔈 {member.mention} left {before.channel.mention}", color=discord.Color.dark_grey())
        else:
            embed = discord.Embed(description=f"🔀 {member.mention} moved {before.channel.mention} → {after.channel.mention}", color=discord.Color.blurple())
        await self._send(member.guild, config, "voice", embed)

    # ── Invites ──────────────────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_invite_create(self, invite: discord.Invite):
        config = await self._config(invite.guild.id)
        if not config.get("log_invites_enabled"):
            return
        inviter = f"{invite.inviter}" if invite.inviter else "Unknown"
        expiry = f"<t:{int((invite.created_at.timestamp() + invite.max_age))}:R>" if invite.max_age else "Never"
        embed = discord.Embed(
            title="🔗 Invite created",
            description=f"`{invite.code}` in {invite.channel.mention}\n**By:** {inviter}\n**Max uses:** {invite.max_uses or '∞'}\n**Expires:** {expiry}",
            color=discord.Color.green(),
        )
        await self._send(invite.guild, config, "invites", embed)

    @commands.Cog.listener()
    async def on_invite_delete(self, invite: discord.Invite):
        config = await self._config(invite.guild.id)
        if not config.get("log_invites_enabled"):
            return
        embed = discord.Embed(title="🗑️ Invite deleted", description=f"`{invite.code}`", color=discord.Color.red())
        await self._send(invite.guild, config, "invites", embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(ServerLogsCog(bot))
