# path: discord_bot/cogs/invites.py

"""
Invite tracker — Discord equivalent of ProBot/MEE6's "who invited this
member" feature. Setup is a Components V2 wizard (see _views_invites.py),
posted automatically some time after the bot joins a server (delayed, not
immediate — see WIZARD_POST_DELAY below) and also re-postable any time via
/invites setup, for when the auto-posted one gets buried or deleted.

There is no gateway event for "invite X was used" — Discord only tells
you a member joined. Attribution works by diffing:

  1. Every active invite's use-count is cached (discord_invite_cache),
     rebuilt wholesale on bot startup (on_ready, covers the restart
     case) and on guild join, then kept current on every member join.
  2. On a new join, guild.invites() is re-fetched and compared against
     the cache: whichever invite's `uses` went up by 1 is the one used.
  3. A single-use invite is DELETED by Discord the instant it's used
     (it never shows up with uses+1) — handled as its own case: if
     nothing matched by use-count but exactly one invite disappeared
     from the fetch, that's the one.
  4. The guild's vanity invite (if it has one) isn't included in
     guild.invites() at all, so it's checked separately via
     guild.vanity_invite().
  5. If none of the above resolves — missing Manage Server permission,
     a Discord Discovery join, or a genuinely ambiguous case — the join
     is logged with no inviter rather than guessed at, and the
     announcement (if enabled) says so plainly instead of pretending.

Leaves are tracked too (on_member_remove closes the join row) so the
leaderboard's "net" count isn't inflated by someone using alt accounts
to join-then-leave for a fake invite count.
"""

import logging
from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands, tasks

from database import db
from discord_bot.cogs._dm_support import GuildOnlyCog
from discord_bot.cogs._views_invites import check_wizard_access, build_wizard_view, remember_wizard_message

logger = logging.getLogger(__name__)

# How long after joining a guild the wizard gets posted, instead of
# immediately on join — a brand-new server's first minutes are already
# full of other bots' own setup messages, so landing one more the
# instant we join just adds to that pile-up and is easy to lose in the
# scroll. _scheduler_loop below is what actually posts it once this much
# time has passed (checked every SCHEDULER_INTERVAL, not on a per-guild
# timer, so it also naturally covers guilds that were pending across a
# bot restart).
WIZARD_POST_DELAY = timedelta(hours=1)
SCHEDULER_INTERVAL_MINUTES = 5

# Same order welcome.py's _suggested_channel uses for its own on-join
# post. Duplicated (not imported) since welcome.py doesn't export it for
# reuse and this is a small, self-contained heuristic.
DEFAULT_CHANNEL_NAME_HINTS = ("welcome", "general", "lobby", "entrance")


def _clone_id_of(bot) -> int:
    return getattr(bot, "clone_id", None)


def _suggested_channel(guild: discord.Guild):
    if guild.system_channel and guild.system_channel.permissions_for(guild.me).send_messages:
        return guild.system_channel
    for hint in DEFAULT_CHANNEL_NAME_HINTS:
        for channel in guild.text_channels:
            if hint in channel.name.lower() and channel.permissions_for(guild.me).send_messages:
                return channel
    for channel in guild.text_channels:
        if channel.permissions_for(guild.me).send_messages:
            return channel
    return None


class InvitesCog(GuildOnlyCog):
    def __init__(self, bot):
        self.bot = bot
        self._scheduler_loop.start()

    def cog_unload(self):
        self._scheduler_loop.cancel()

    # ── snapshotting ─────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_ready(self):
        # Baseline for every guild this process is already in — covers the
        # startup/restart case (on_guild_join only fires for joins that
        # happen while connected), so the very first join after a restart
        # still has a cache to diff against instead of finding nothing and
        # falling through to the "untracked source" branch.
        clone_id = _clone_id_of(self.bot)
        for guild in self.bot.guilds:
            try:
                await self._snapshot_invites(guild)
            except Exception:
                logger.exception(f"[invites] startup snapshot failed for guild {guild.id}")
            # Backfill for guilds the bot was already in before this
            # feature existed (or before this process's first-ever
            # on_guild_join for it) — without this they'd never get a
            # wizard_due_at set and so would never see the wizard at all.
            try:
                config = await db.get_invite_tracker_config(guild.id, clone_id=clone_id)
                if config.get("wizard_due_at") is None and config.get("wizard_message_id") is None:
                    due_at = discord.utils.utcnow() + WIZARD_POST_DELAY
                    await db.set_invite_tracker_config(guild.id, clone_id=clone_id, wizard_due_at=due_at)
            except Exception:
                logger.exception(f"[invites] startup backfill-schedule failed for guild {guild.id}")

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild):
        try:
            await self._snapshot_invites(guild)
        except Exception:
            logger.exception(f"[invites] join snapshot failed for guild {guild.id}")
        # Not posted right away — just marks it due later. _scheduler_loop
        # is what actually posts it, once WIZARD_POST_DELAY has passed.
        try:
            clone_id = _clone_id_of(self.bot)
            due_at = discord.utils.utcnow() + WIZARD_POST_DELAY
            await db.set_invite_tracker_config(guild.id, clone_id=clone_id, wizard_due_at=due_at)
        except Exception:
            logger.exception(f"[invites] failed to schedule wizard for guild {guild.id}")

    @tasks.loop(minutes=SCHEDULER_INTERVAL_MINUTES)
    async def _scheduler_loop(self):
        """Posts the auto-wizard for any guild whose WIZARD_POST_DELAY has
        elapsed and hasn't had one posted yet (wizard_message_id still
        NULL). Checked on an interval rather than a per-guild asyncio
        timer so a guild that joined right before a restart doesn't lose
        its scheduled post — it's just picked up on the next tick once
        due, exactly like automod's _reminder_loop."""
        try:
            clone_id = _clone_id_of(self.bot)
            due_guild_ids = await db.get_due_invite_wizard_guilds(clone_id)
        except Exception:
            logger.exception("[invites] scheduler: failed to list due guilds")
            return
        for guild_id in due_guild_ids:
            guild = self.bot.get_guild(guild_id)
            if guild is None:
                continue
            try:
                await self.post_setup_wizard_on_join(guild)
            except Exception:
                logger.exception(f"[invites] scheduler: failed to post wizard for guild {guild_id}")

    @_scheduler_loop.before_loop
    async def _before_scheduler_loop(self):
        await self.bot.wait_until_ready()

    async def post_setup_wizard_on_join(self, guild: discord.Guild):
        """Posts the invite-tracker wizard in-channel. Despite the name
        (kept for parity with welcome.py's equivalent) this is no longer
        called right on join — see on_guild_join above — it's called
        later by _scheduler_loop, and on demand by /invites setup.
        Best-effort: a guild with no postable channel is skipped
        silently."""
        channel = _suggested_channel(guild)
        if not channel:
            return
        try:
            clone_id = _clone_id_of(self.bot)
            config = await db.get_invite_tracker_config(guild.id, clone_id=clone_id)
            view = build_wizard_view(guild.id, clone_id, None, config)
            message = await channel.send(
                content=(
                    f"🔗 Want to know who's inviting people to **{guild.name}**? Set up the invite "
                    f"tracker below — anyone with **Manage Server** can use it, no commands needed. "
                    f"(You can also bring this back later with `/invites setup`.)"
                ),
                view=view,
            )
            await remember_wizard_message(guild.id, clone_id, None, message.channel.id, message.id)
        except (discord.HTTPException, discord.Forbidden) as e:
            logger.info(f"[invites] Auto-posted setup wizard skipped for guild {guild.id}: {e}")

    group = app_commands.guild_only()(app_commands.Group(name="invites", description="Invite tracker"))

    @group.command(name="setup", description="Post (or re-post) the invite tracker setup wizard here")
    async def setup_wizard(self, interaction: discord.Interaction):
        await interaction.response.defer()
        if not await check_wizard_access(interaction, interaction.user.id, "invites", "manage_guild", "Manage Server"):
            return
        clone_id = _clone_id_of(self.bot)
        config = await db.get_invite_tracker_config(interaction.guild_id, clone_id=clone_id)
        view = build_wizard_view(interaction.guild_id, clone_id, interaction.user.id, config)
        # Posted publicly (not ephemeral) so anyone in the server can see
        # it — only the invoker can actually use its components, same
        # invoker_id-in-custom_id gate every other wizard here uses.
        await interaction.followup.send(view=view)
        message = await interaction.original_response()
        await remember_wizard_message(interaction.guild_id, clone_id, interaction.user.id, message.channel.id, message.id)

    async def _snapshot_invites(self, guild: discord.Guild) -> None:
        """Rebuilds the persisted use-count cache from a fresh
        guild.invites() fetch (+ vanity invite if the guild has one).
        Silently no-ops on missing permission — the cache just stays
        empty, and _handle_join already falls back to the "no
        permission" branch gracefully when that's the case."""
        clone_id = _clone_id_of(self.bot)
        cache = {}
        try:
            invites = await guild.invites()
        except (discord.Forbidden, discord.HTTPException):
            invites = []
        for inv in invites:
            cache[inv.code] = {
                "uses": inv.uses or 0,
                "inviter_id": inv.inviter.id if inv.inviter else None,
                "is_vanity": False,
            }
        if "VANITY_URL" in guild.features:
            try:
                vanity = await guild.vanity_invite()
                if vanity:
                    cache[vanity.code] = {"uses": vanity.uses or 0, "inviter_id": None, "is_vanity": True}
            except (discord.Forbidden, discord.HTTPException):
                pass
        await db.replace_invite_cache(guild.id, clone_id, cache)

    # ── join / leave tracking ────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if member.bot:
            return
        try:
            await self._handle_join(member)
        except Exception:
            logger.exception(f"[invites] join handling failed for member {member.id} in guild {member.guild.id}")

    async def _handle_join(self, member: discord.Member):
        guild = member.guild
        clone_id = _clone_id_of(self.bot)
        config = await db.get_invite_tracker_config(guild.id, clone_id=clone_id)
        old_cache = await db.get_invite_cache(guild.id, clone_id=clone_id)

        try:
            current_invites = await guild.invites()
        except (discord.Forbidden, discord.HTTPException):
            current_invites = None

        inviter_id = None
        invite_code = None
        is_vanity = False
        no_permission = current_invites is None

        if current_invites is not None:
            new_cache = {}
            used_invite = None
            for inv in current_invites:
                new_cache[inv.code] = {
                    "uses": inv.uses or 0,
                    "inviter_id": inv.inviter.id if inv.inviter else None,
                    "is_vanity": False,
                }
                old = old_cache.get(inv.code)
                if old is not None and (inv.uses or 0) > old.get("uses", 0):
                    used_invite = inv

            if used_invite is not None:
                inviter_id = used_invite.inviter.id if used_invite.inviter else None
                invite_code = used_invite.code
            else:
                # No use-count increase found — check for a single-use
                # invite that's now gone entirely, which is how Discord
                # signals "this one was just used" for single-use invites
                # (they're deleted on use, not incremented).
                gone_codes = [c for c in old_cache if c not in new_cache and not old_cache[c].get("is_vanity")]
                if len(gone_codes) == 1:
                    code = gone_codes[0]
                    inviter_id = old_cache[code].get("inviter_id")
                    invite_code = code

            if invite_code is None and "VANITY_URL" in guild.features:
                try:
                    vanity = await guild.vanity_invite()
                    if vanity:
                        old_vanity = old_cache.get(vanity.code)
                        if old_vanity and (vanity.uses or 0) > old_vanity.get("uses", 0):
                            is_vanity = True
                            invite_code = vanity.code
                        new_cache[vanity.code] = {"uses": vanity.uses or 0, "inviter_id": None, "is_vanity": True}
                except (discord.Forbidden, discord.HTTPException):
                    pass

            await db.replace_invite_cache(guild.id, clone_id, new_cache)

        await db.record_invite_join(guild.id, clone_id, member.id, inviter_id, invite_code)

        if not config.get("enabled", True) or not config.get("channel_id"):
            return
        channel = guild.get_channel(int(config["channel_id"]))
        if channel is None:
            return

        if inviter_id:
            _, net = await db.get_inviter_stats(guild.id, clone_id, inviter_id)
            content = (
                f"📥 {member.mention} joined — invited by <@{inviter_id}> "
                f"(**{net}** invite{'s' if net != 1 else ''})"
            )
        elif is_vanity:
            content = f"📥 {member.mention} joined via the server's **vanity invite**."
        elif no_permission:
            content = f"📥 {member.mention} joined. (I need **Manage Server** here to attribute invites.)"
        else:
            content = (
                f"📥 {member.mention} joined via an untracked source "
                f"(e.g. server Discovery, or an invite that changed before I could check it)."
            )
        try:
            await channel.send(content)
        except (discord.Forbidden, discord.HTTPException):
            pass

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        if member.bot:
            return
        try:
            clone_id = _clone_id_of(self.bot)
            await db.record_invite_leave(member.guild.id, clone_id, member.id)
        except Exception:
            logger.exception(f"[invites] leave handling failed for member {member.id} in guild {member.guild.id}")


async def setup(bot):
    await bot.add_cog(InvitesCog(bot))
    logger.info("[invites] ✅ InvitesCog loaded — /invites setup command registered")
