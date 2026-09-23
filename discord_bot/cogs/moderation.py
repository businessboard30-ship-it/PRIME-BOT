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

import io
import logging
from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands
from discord_bot.cogs._dm_support import GuildOnlyCog
from discord_bot import perm_check

from database import db
from modules import moderation_adapter as mod
from modules import moderation_extra as modx
from discord_bot.cogs._views_moderation import ModActionView, WarnActionView, ModLogsView, ConfirmActionView

logger = logging.getLogger(__name__)

WARN_LIMIT_BEFORE_TIMEOUT = 3
DEFAULT_TIMEOUT_MINUTES = 60  # used for auto-timeout on hitting the warn limit

# Keep each deleted-message transcript line well under Discord's 4096-char
# embed description limit even at 100 lines; anything longer than this per
# message gets truncated so one giant paste can't blow the whole embed.
CLEAR_LOG_CONTENT_TRUNCATE = 200


def _clone_id_of(interaction: discord.Interaction):
    return getattr(interaction.client, "clone_id", None)


async def _post_clear_log(interaction: discord.Interaction, channel: discord.abc.GuildChannel, deleted: list) -> None:
    """Posts a transcript of what /messages purge (or ai_purge) just deleted
    to the guild's configured mod-log channel, if one is set up and the
    "moderation" log category is enabled (discord_automod_config —
    shared with server_logs.py's ServerLogsCog, see that module's
    docstring). Best-effort only: never raises, since a logging failure
    shouldn't surface as if the purge itself failed.
    """
    try:
        config = await db.get_automod_config(interaction.guild_id, clone_id=_clone_id_of(interaction))
        if not config.get("log_channel_id") or not config.get("log_moderation_enabled"):
            return
        log_channel = interaction.guild.get_channel(int(config["log_channel_id"]))
        if log_channel is None or not deleted:
            return

        # Oldest first reads like a normal chat transcript.
        ordered = sorted(deleted, key=lambda m: m.created_at)
        lines = []
        for m in ordered:
            author = f"{m.author} ({m.author.id})" if m.author else "Unknown author"
            content = m.content.replace("\n", " ").strip() if m.content else ""
            if not content:
                if m.attachments:
                    content = f"[{len(m.attachments)} attachment(s)]"
                elif m.embeds:
                    content = "[embed]"
                else:
                    content = "[no text content]"
            elif len(content) > CLEAR_LOG_CONTENT_TRUNCATE:
                content = content[:CLEAR_LOG_CONTENT_TRUNCATE] + "…"
            lines.append(f"[{m.created_at:%Y-%m-%d %H:%M:%S}] {author}: {content}")

        transcript = "\n".join(lines)
        embed = discord.Embed(
            title="🧹 Messages purged",
            description=(
                f"**{len(deleted)}** message(s) deleted in {channel.mention} "
                f"by {interaction.user.mention}."
            ),
            color=discord.Color.orange(),
        )
        embed.set_footer(text=f"By {interaction.user}", icon_url=getattr(interaction.user.display_avatar, "url", None))

        # Discord embed field values cap at 1024 chars; a field-code-block
        # transcript fits comfortably up to that. Beyond it, fall back to a
        # .txt attachment rather than silently truncating the transcript.
        if len(transcript) <= 1000:
            embed.add_field(name="Deleted messages", value=f"```{transcript}```", inline=False)
            await log_channel.send(embed=embed)
        else:
            buf = io.BytesIO(transcript.encode("utf-8"))
            file = discord.File(buf, filename=f"purge-{channel.id}-{int(interaction.created_at.timestamp())}.txt")
            await log_channel.send(embed=embed, file=file)
    except (discord.Forbidden, discord.HTTPException) as e:
        logger.warning(f"[v0] Failed to post clear log for guild={interaction.guild_id}: {e}")
    except Exception as e:
        logger.warning(f"[v0] Unexpected error posting clear log for guild={interaction.guild_id}: {e}")


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


async def _respond(interaction: discord.Interaction, content: str, view):
    """Shared result-sender for the do-work functions below.

    The manual /command flow reaches these with `interaction.response`
    still free (it's the fresh interaction handed to ConfirmActionView's
    on_confirm), so it edits the confirm message in place as before. The
    AI-confirmed flow (see ai_kick/ai_ban/ai_unwarn/ai_purge below) reaches
    these with `interaction.response` already consumed by AIConfirmView's
    own button-disable edit, so it must use a followup instead — calling
    response.edit_message a second time raises discord.InteractionResponded.
    """
    if interaction.response.is_done():
        # followup.send() rejects view=None outright (it only accepts an
        # actual View/LayoutView or the MISSING sentinel meaning "no view").
        # None here means "no buttons", so translate it to MISSING.
        await interaction.followup.send(
            content,
            view=view if view is not None else discord.utils.MISSING,
            ephemeral=True,
        )
    else:
        # edit_message() *does* accept view=None (it means "clear the
        # existing view/buttons"), so pass it through unchanged here.
        await interaction.response.edit_message(content=content, view=view)


class ModerationCog(GuildOnlyCog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # AI-executed commands with requires_confirmation=True in
    # ai_command_allowlist.py that ALSO show their own ConfirmActionView
    # here would otherwise get double-confirmed and crash on
    # discord.InteractionResponded (AIConfirmView already used up the
    # interaction's response before execute_ai_command ever reaches this
    # cog's callback). ai_command_guard.execute_ai_command looks this map
    # up and, when present, calls the named method directly instead of
    # going through the slash-command callback + its ConfirmActionView.
    AI_CONFIRMED_HANDLERS = {
        "kick": "ai_kick",
        "ban": "ai_ban",
        "unwarn": "ai_unwarn",
        "purge": "ai_purge",
    }

    # ── /kick ────────────────────────────────────────────────────────────
    async def _do_kick(self, confirm_interaction: discord.Interaction, member: discord.Member, reason: str):
        try:
            await member.kick(reason=reason)
        except discord.Forbidden:
            await _respond(
                confirm_interaction,
                "⚠️ " + (perm_check.member_problem(confirm_interaction.guild, member, "kick_members", "kick") or "I can't kick that member (check role hierarchy)."),
                None,
            )
            return
        await modx.log_action(confirm_interaction.guild_id, "kick", confirm_interaction.user.id, target_user_id=member.id, reason=reason)
        await _respond(confirm_interaction, f"👢 {member.mention} kicked.\nReason: {reason}", ModActionView(member.id))

    @app_commands.command(name="kick", description="Kick a member from this server")
    @app_commands.guild_only()
    @app_commands.describe(member="Member to kick", reason="Reason (shown in the audit log)")
    async def kick(self, interaction: discord.Interaction, member: discord.Member, reason: str = "No reason given"):
        if not _require_perm(interaction, "kick_members"):
            await _deny(interaction, "Kick Members")
            return

        view = ConfirmActionView(interaction.user.id, lambda ci: self._do_kick(ci, member, reason), confirm_label="Kick")
        await interaction.response.send_message(
            f"⚠️ Kick {member.mention}? Reason: {reason}", view=view, ephemeral=True
        )

    async def ai_kick(self, confirm_interaction: discord.Interaction, member: discord.Member, reason: str = "No reason given"):
        """Entry point for execute_ai_command — confirm_interaction is the
        AIConfirmView button click, so the user has already confirmed and
        this must NOT show another ConfirmActionView. Re-checks permission
        since time has passed since the confirm prompt was shown."""
        if not _require_perm(confirm_interaction, "kick_members"):
            await _deny(confirm_interaction, "Kick Members")
            return
        await self._do_kick(confirm_interaction, member, reason)

    # ── /ban ─────────────────────────────────────────────────────────────
    @app_commands.command(name="ban", description="Ban a member from this server")
    @app_commands.guild_only()
    @app_commands.describe(
        user="Member to ban — pick them from the list, or paste their numeric Discord user ID "
             "(works even if they've left, or never joined at all)",
        reason="Reason (shown in the audit log)",
        delete_days="Days of message history to delete (0-7)",
    )
    async def ban(self, interaction: discord.Interaction, user: str, reason: str = "No reason given", delete_days: app_commands.Range[int, 0, 7] = 0):
        if not _require_perm(interaction, "ban_members"):
            await _deny(interaction, "Ban Members")
            return

        member, target_id, target_mention, error = await self._resolve_ban_target(interaction, user)
        if error:
            await interaction.response.send_message(error, ephemeral=True)
            return

        view = ConfirmActionView(
            interaction.user.id,
            lambda ci: self._do_ban(ci, member, target_id, target_mention, reason, delete_days),
            confirm_label="Ban",
        )
        await interaction.response.send_message(
            f"⚠️ Ban {target_mention}? This also deletes {delete_days} day(s) of messages.\nReason: {reason}",
            view=view, ephemeral=True
        )

    async def _resolve_ban_target(self, interaction: discord.Interaction, user: str):
        """Shared by ban() and ai_ban(). `user` is raw text either way (a
        picked member's mention/name, or a hand-typed ID). Resolve to a
        real member first so hierarchy checks / a proper mention still
        apply when they're in the server; otherwise fall back to the raw
        ID directly — this is what lets you ban someone who already left,
        or who never joined in the first place (pre-emptive ban).
        Returns (member_or_None, target_id, target_mention, error_message).
        error_message is None on success."""
        digits = user.strip("<@!>")
        member = None
        if digits.isdigit():
            member = interaction.guild.get_member(int(digits))
            if member is None:
                try:
                    member = await interaction.guild.fetch_member(int(digits))
                except discord.NotFound:
                    member = None
        else:
            member = discord.utils.find(
                lambda m: user.lower() in (m.name.lower(), m.display_name.lower()),
                interaction.guild.members,
            )
            if member is None:
                return None, None, None, (
                    f"Couldn't find a member matching `{user}`. Pick them from the list, "
                    "or paste their numeric Discord user ID to ban by ID."
                )

        target_id = member.id if member else int(digits)
        target_mention = member.mention if member else f"`{target_id}` (not in this server)"
        return member, target_id, target_mention, None

    async def _do_ban(self, confirm_interaction: discord.Interaction, member, target_id: int, target_mention: str, reason: str, delete_days: int):
        try:
            if member is not None:
                await member.ban(reason=reason, delete_message_days=delete_days)
            else:
                # Ban-by-ID: no hierarchy to check since they're not a
                # member here, and no message history to purge.
                await confirm_interaction.guild.ban(discord.Object(id=target_id), reason=reason)
        except discord.Forbidden:
            hint = perm_check.member_problem(confirm_interaction.guild, member, "ban_members", "ban") if member else None
            await _respond(
                confirm_interaction,
                "⚠️ " + (hint or "I can't ban that member (check role hierarchy) or don't have permission to ban."),
                None,
            )
            return
        await modx.log_action(confirm_interaction.guild_id, "ban", confirm_interaction.user.id, target_user_id=target_id, reason=reason)
        await _respond(confirm_interaction, f"🔨 {target_mention} banned.\nReason: {reason}", ModActionView(target_id))

    async def ai_ban(self, confirm_interaction: discord.Interaction, user: str, reason: str = "No reason given", delete_days: int = 0):
        """Entry point for execute_ai_command — see ai_kick for why this
        must not show another ConfirmActionView."""
        if not _require_perm(confirm_interaction, "ban_members"):
            await _deny(confirm_interaction, "Ban Members")
            return
        member, target_id, target_mention, error = await self._resolve_ban_target(confirm_interaction, user)
        if error:
            await confirm_interaction.followup.send(error, ephemeral=True)
            return
        await self._do_ban(confirm_interaction, member, target_id, target_mention, reason, delete_days)

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
            await interaction.response.send_message("⚠️ " + perm_check.forbidden_hint(interaction.guild, "ban_members"), ephemeral=True)
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
                "⚠️ " + (perm_check.member_problem(interaction.guild, member, "moderate_members", "time out") or "I can't time out that member (check role hierarchy)."), ephemeral=True
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
            await interaction.response.send_message("⚠️ " + (perm_check.member_problem(interaction.guild, member, "moderate_members", "change the timeout of") or perm_check.forbidden_hint(interaction.guild, "moderate_members")), ephemeral=True)
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
                    f"— {perm_check.member_problem(interaction.guild, member, 'moderate_members', 'time out') or 'check my role hierarchy'}\nReason: {reason}",
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

        view = ConfirmActionView(interaction.user.id, lambda ci: self._do_unwarn(ci, member), confirm_label="Clear warns")
        await interaction.response.send_message(
            f"⚠️ Clear all {current} warn(s) for {member.mention}? This can't be undone.", view=view, ephemeral=True
        )

    async def _do_unwarn(self, confirm_interaction: discord.Interaction, member: discord.Member):
        await mod.clear_warns(member.id, confirm_interaction.guild_id)
        await modx.log_action(confirm_interaction.guild_id, "unwarn", confirm_interaction.user.id, target_user_id=member.id, reason="")
        await _respond(confirm_interaction, f"✅ Cleared warns for {member.mention}.", WarnActionView(member))

    async def ai_unwarn(self, confirm_interaction: discord.Interaction, member: discord.Member):
        """Entry point for execute_ai_command — see ai_kick for why this
        must not show another ConfirmActionView. Unlike the manual /unwarn,
        the AI's confirm prompt was already shown before we knew the warn
        count, so the zero-warns case is handled here instead of upfront."""
        if not _require_perm(confirm_interaction, "moderate_members"):
            await _deny(confirm_interaction, "Timeout Members")
            return
        current = await mod.get_warn_count(member.id, confirm_interaction.guild_id)
        if current == 0:
            await confirm_interaction.followup.send(f"{member.mention} has no warns to clear.", ephemeral=True)
            return
        await self._do_unwarn(confirm_interaction, member)

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

    # ── /messages clear ─────────────────────────────────────────────────
    # A group (not a standalone top-level command) so this only costs one
    # slot against Discord's 100-command cap and future message-management
    # subcommands can nest under it the same way.
    messages_group = app_commands.guild_only()(
        app_commands.Group(name="messages", description="Message management")
    )

    @messages_group.command(name="purge", description="Bulk-delete recent messages in this channel")
    @app_commands.describe(amount="How many recent messages to delete (1-100). Discord can only bulk-delete messages younger than 14 days.")
    async def purge(self, interaction: discord.Interaction, amount: app_commands.Range[int, 1, 100]):
        if not _require_perm(interaction, "manage_messages"):
            await _deny(interaction, "Manage Messages")
            return

        view = ConfirmActionView(interaction.user.id, lambda ci: self._do_clear(ci, interaction.channel, amount), confirm_label="Delete")
        await interaction.response.send_message(
            f"⚠️ Delete the last {amount} message(s) in {interaction.channel.mention}? This can't be undone.",
            view=view, ephemeral=True
        )

    async def _do_clear(self, confirm_interaction: discord.Interaction, channel, amount: int):
        try:
            deleted = await channel.purge(limit=amount)
        except discord.Forbidden:
            await _respond(confirm_interaction, "⚠️ I don't have permission to manage messages in this channel.", None)
            return
        except discord.HTTPException as e:
            await _respond(
                confirm_interaction,
                f"⚠️ Couldn't delete those messages: {e}. Discord only bulk-deletes messages younger than 14 days.",
                None,
            )
            return
        await modx.log_action(confirm_interaction.guild_id, "clear", confirm_interaction.user.id, target_user_id=channel.id, reason=f"{len(deleted)} message(s)")
        await _post_clear_log(confirm_interaction, channel, deleted)
        await _respond(confirm_interaction, f"🧹 Deleted {len(deleted)} message(s) in {channel.mention}.", None)

    async def ai_purge(self, confirm_interaction: discord.Interaction, amount: int):
        """Entry point for execute_ai_command — see ai_kick for why this
        must not show another ConfirmActionView. Uses confirm_interaction's
        own channel (the channel the AI chat happened in), matching what
        the AI's confirm prompt told the user it would delete from."""
        if not _require_perm(confirm_interaction, "manage_messages"):
            await _deny(confirm_interaction, "Manage Messages")
            return
        await self._do_clear(confirm_interaction, confirm_interaction.channel, amount)

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
