"""
Auto-moderation — Discord equivalent of Dyno's automod, and the logging half
of Carl-bot.

Config lives in database.py's discord_automod_config, a Discord-only table
(guild_id, clone_id)-scoped like the rest of this expansion — NOT the
earlier approach of bolting columns onto Telegram's group_moderation_settings.
That table's admin_id column has a hard FK into `users(user_id)` (Telegram's
user table), so writing a Discord admin id there risked an FK violation, and
it had no clone_id, so two Discord bots in the same guild silently shared
one automod config. See database.py's discord_automod_config comment for
the full reasoning.

Action logging (modx.log_action) and warns (mod.add_warn) still go through
the existing shared Telegram-side modules on purpose — the expansion spec
wants a unified mod-log across manual and automatic actions, and those two
functions already self-heal the users-table FK issue for Discord ids (see
their own docstrings). Flood-event rate tracking also stays on the shared
flood_events table: it's a transient per-user counter, not guild config, so
two bots racing to track/clear the same counter in a shared guild is a
minor simplification, not a data leak.

Filters implemented here, each independently toggleable via /automod:
  - word_filter          -> discord_automod_config.banned_words (JSONB list)
  - anti_invite          -> regex match on discord.gg/... links
  - anti_mention         -> too many @mentions in one message
  - spam (flood)         -> modules.moderation_extra's existing flood_events
  - min account age gate -> on_member_join, kicks accounts younger than
                             min_account_age_hours (raid protection)

All content-based filters share one configurable action (action: delete /
warn / timeout / kick) rather than a per-filter action, to keep the config
surface small for this first pass.
"""

import asyncio
import logging
import time
import os
import re
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks
from discord_bot.cogs._dm_support import GuildOnlyCog

from database import db
from modules import moderation_adapter as mod
from modules import moderation_extra as modx
from config import DASHBOARD_BASE_URL, DISCORD_CLONE_ADMIN_IDS
from discord_bot.i18n_helpers import get_lang, tr
from discord_bot.cogs._views_shared import ActionButton, NavCardView, refresh_button
from discord_bot.cogs._views_automod import AutomodPanelView
from discord_bot.cogs._views_automod_wizard import (
    build_wizard_view as build_automod_wizard_view,
    remember_wizard_message as remember_automod_wizard_message,
    refresh_posted_wizard as refresh_automod_wizard,
)
from discord_bot.cogs._views_automod_reminders import build_reminder_view

# Curated list (LDNOOBW's public "en" list, deduped/sorted) bundled at
# data/preset_banned_words.txt so /automod bannedword preset works offline —
# no network call to a third-party site at runtime.
PRESET_BANNED_WORDS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data", "preset_banned_words.txt",
)

# i18n scope note: the /automod slash commands below (ephemeral, seen only
# by the invoking admin) go through tr(). The mod-log embeds this cog posts
# into a guild's configured log channel (_enforce, on_message_delete,
# on_message_edit, on_member_remove, on_member_join's raid-kick embed) stay
# in English on purpose — that channel is a shared audit trail read by a
# whole mod team, not personalized per viewer, and per-entry language
# switching would hurt greppability/searchability of the log.

logger = logging.getLogger(__name__)

INVITE_RE = re.compile(r"(discord\.gg/|discordapp\.com/invite/|discord\.com/invite/)", re.IGNORECASE)

VALID_ACTIONS = {"delete", "warn", "timeout", "kick"}
VALID_FILTERS = {"word_filter", "anti_invite", "anti_mention", "spam"}


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


async def _deny(interaction: discord.Interaction, perm_name: str, lang: str):
    msg = await tr("You need the **{perm_name}** permission to do that.", lang, perm_name=perm_name)
    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)


def _require_bot_owner(user_id: int) -> bool:
    """Same convention as bump.py's _require_bot_owner / archive.py's
    _is_owner: DISCORD_CLONE_ADMIN_IDS, not guild ownership. Used for
    /automod owner's cleanup subcommand — deleting reminder DMs across
    every guild the bot can see is not something a single guild's owner
    should be able to trigger."""
    return user_id in DISCORD_CLONE_ADMIN_IDS


def _clone_id_of(interaction: discord.Interaction):
    """Same convention as the other Discord-expansion cogs: None on the main
    bot, the clone's row id on a clone process."""
    return getattr(interaction.client, "clone_id", None)


DEFAULT_LOG_CHANNEL_NAME = "mod-logs"
# How many DMs the owner gets nudged with about a bot-created log channel
# before we leave them alone. Spacing between reminders lives in
# _reminder_loop's interval, not here.
MAX_LOG_CHANNEL_NOTICES = 3
LOG_CHANNEL_REMINDER_GAP = timedelta(days=2)

# Same capped/spaced-DM approach as the log-channel notices, for a
# different one-time nudge: owners who never turned the word filter on.
MAX_WORDFILTER_NOTICES = 3
WORDFILTER_REMINDER_GAP = timedelta(days=2)

# Exact embed titles the old, since-removed standalone-DM code path used to
# send (one DM per guild per notice, before _reminder_loop was changed to
# send one combined DM per owner — see _send_combined_reminder). Kept here,
# not deleted along with that code, purely so /automod owner
# cleanupreminders can still recognize and delete any of these still
# sitting in an owner's DMs, including the crash-loop burst that prompted
# this whole change.
LEGACY_REMINDER_EMBED_TITLES = {
    "📋 I set up a log channel for you",
    "📋 Reminder: log channel is active",
    "📋 Last reminder about your log channel",
    "🛡️ Your word filter isn't on yet",
    "🛡️ Reminder: word filter is still off",
    "🛡️ Last reminder: word filter is still off",
}


class AutomodCog(GuildOnlyCog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Message ids the bot itself just deleted via _enforce() below.
        # Discord still fires on_message_delete for bot-initiated deletes,
        # so without this, every automod action produced BOTH the
        # "🛡️ Auto-mod action" embed (from _enforce) AND a "🗑️ Message
        # deleted" embed (from the on_message_delete listener) for the
        # same message — i.e. the log channel doubled up. A small bounded
        # set of recently-self-deleted ids lets on_message_delete recognize
        # and skip those, while still logging normal user/other deletes.
        self._self_deleted_ids: set[int] = set()
        self._reminder_loop.start()

    def cog_unload(self):
        self._reminder_loop.cancel()

    # ── auto-create-on-join log channel ─────────────────────────────────
    async def ensure_log_channel(self, guild: discord.Guild, clone_id=None):
        """Returns (channel, just_created). Reuses the configured log
        channel if it's still set and still exists; otherwise tries to
        create one (or re-create one that got deleted out from under us)
        so every guild gets logging (message delete/edit, join/leave,
        auto-mod actions — see the listeners above) without an admin
        having to run /automod setlogchannel first. Silently gives up
        (returns None, False) if the bot lacks Manage Channels — there's
        nothing to DM the owner about in that case."""
        config = await db.get_automod_config(guild.id, clone_id=clone_id)
        existing_id = config.get("log_channel_id")
        if existing_id:
            channel = guild.get_channel(int(existing_id))
            if channel is not None:
                return channel, False
            # Configured channel was deleted. If an admin had picked it
            # deliberately, don't second-guess them by silently spinning
            # up a replacement and re-starting owner DMs.
            if not config.get("log_channel_auto_created"):
                return None, False

        if not guild.me.guild_permissions.manage_channels:
            return None, False

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, embed_links=True),
        }
        try:
            channel = await guild.create_text_channel(
                DEFAULT_LOG_CHANNEL_NAME,
                overwrites=overwrites,
                reason="PRIME-BOT: auto-created logging channel (message/member/auto-mod logs)",
            )
        except (discord.Forbidden, discord.HTTPException) as e:
            logger.info(f"Could not auto-create log channel in {guild.id}: {e}")
            return None, False

        await db.set_automod_config(
            guild.id, clone_id=clone_id,
            log_channel_id=channel.id, log_channel_auto_created=True,
            log_channel_notice_count=0, log_channel_last_notice_at=None,
        )
        return channel, True

    # NOTE: the old _notify_owner_log_channel / _notify_owner_word_filter
    # methods (one standalone DM per guild per type) were removed —
    # _reminder_loop now collects pending items across every guild first
    # and sends one combined DM per owner via _send_combined_reminder
    # below. The on-join first-notice text lives in build_join_notice_fields
    # (folded into bot.py's single combined on-join DM), and the legacy
    # embed titles those old methods used to send are listed in
    # LEGACY_REMINDER_EMBED_TITLES further down for the owner cleanup
    # command to recognize and delete.

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild):
        """New servers: same auto-create-and-announce flow as the backfill
        in _reminder_loop's startup pass, just fired immediately instead
        of waiting for the next loop tick."""
        # NOTE: the actual owner DM for these is no longer sent from here —
        # it's folded into bot.py's single combined on-join DM via
        # build_join_notice_fields() below, so several separate DMs don't
        # land on the owner back-to-back. This still performs the real
        # side-effect (creating the log channel).
        clone_id = getattr(self.bot, "clone_id", None)
        await self.ensure_log_channel(guild, clone_id=clone_id)

    async def build_join_notice_fields(self, guild: discord.Guild, clone_id=None) -> list:
        """Returns a list of (title, body) tuples for the combined on-join
        DM — the log-channel-created notice and/or the word-filter nudge,
        whichever apply. Performs the same bookkeeping (notice_count,
        last_notice_at) that the old standalone _notify_owner_* sends did,
        so the recurring _reminder_loop follow-ups still work correctly
        without a duplicate first notice."""
        fields = []
        channel, created = await self.ensure_log_channel(guild, clone_id=clone_id)
        if created and channel is not None:
            fields.append((
                "📋 I set up a log channel for you",
                f"I created {channel.mention} in **{guild.name}** to log deleted/edited messages, "
                f"member joins & leaves, and auto-mod actions.\n\n"
                f"Want a different channel, or none at all? Run `/automod setlogchannel` in the server.",
            ))
            await db.set_automod_config(
                guild.id, clone_id=clone_id,
                log_channel_notice_count=1,
                log_channel_last_notice_at=discord.utils.utcnow(),
            )

        config = await db.get_automod_config(guild.id, clone_id=clone_id)
        if not config.get("word_filter_enabled"):
            fields.append((
                "🛡️ Your word filter isn't on yet",
                f"I moderate messages in **{guild.name}**, but the word filter is currently off, "
                f"so nothing gets blocked.\n\nRun `/automod toggle filter:word_filter enabled:True` "
                f"to turn it on, then `/automod bannedword preset` to load a starter list.",
            ))
            await db.set_automod_config(
                guild.id, clone_id=clone_id,
                wordfilter_notice_count=1,
                wordfilter_last_notice_at=discord.utils.utcnow(),
            )
        return fields

    async def _collect_log_channel_item(self, guild: discord.Guild, config: dict, clone_id, now):
        """Performs the (DM-independent) log-channel create/backfill side
        effect immediately, and returns a pending-notice item dict for
        _reminder_loop to fold into this owner's combined DM — or None if
        nothing needs saying. Never sends a DM itself; that only happens
        once per owner, after every guild has been collected (see
        _send_combined_reminder), so an owner with several guilds gets one
        message instead of one per guild."""
        if not config.get("log_channel_id"):
            # Backfill for guilds where the bot already had a #mod-logs
            # channel sitting there from before this notify feature (or the
            # log_channel_auto_created column) existed — without this, such
            # a channel is invisible to us (log_channel_id was never
            # written to the DB either), so ensure_log_channel would try to
            # create a SECOND mod-logs channel. Adopt the existing one
            # instead: treat it as ours, mark it auto-created, and surface
            # notice #1 for it, exactly as if we'd just created it.
            existing = discord.utils.get(guild.text_channels, name=DEFAULT_LOG_CHANNEL_NAME)
            if existing is not None:
                await db.set_automod_config(
                    guild.id, clone_id=clone_id,
                    log_channel_id=existing.id, log_channel_auto_created=True,
                    log_channel_notice_count=0, log_channel_last_notice_at=None,
                )
                return {
                    "type": "log_channel", "guild_id": guild.id, "guild_name": guild.name,
                    "channel_id": existing.id, "notice_number": 1, "max_notices": MAX_LOG_CHANNEL_NOTICES,
                }

            channel, created = await self.ensure_log_channel(guild, clone_id=clone_id)
            if created:
                return {
                    "type": "log_channel", "guild_id": guild.id, "guild_name": guild.name,
                    "channel_id": channel.id, "notice_number": 1, "max_notices": MAX_LOG_CHANNEL_NOTICES,
                }
            return None

        if not config.get("log_channel_auto_created"):
            return None  # admin owns this channel; not our business
        notice_count = int(config.get("log_channel_notice_count") or 0)
        if notice_count <= 0 or notice_count >= MAX_LOG_CHANNEL_NOTICES:
            return None
        last_notice_at = config.get("log_channel_last_notice_at")
        if last_notice_at and (now - last_notice_at) < LOG_CHANNEL_REMINDER_GAP:
            return None
        channel = guild.get_channel(int(config["log_channel_id"]))
        if channel is None:
            return None
        return {
            "type": "log_channel", "guild_id": guild.id, "guild_name": guild.name,
            "channel_id": channel.id, "notice_number": notice_count + 1, "max_notices": MAX_LOG_CHANNEL_NOTICES,
        }

    async def _collect_word_filter_item(self, guild: discord.Guild, config: dict, clone_id, now):
        """Same idea as _collect_log_channel_item but for the word-filter
        nudge — pure lookup, no side effects, no DM."""
        if config.get("word_filter_enabled"):
            return None  # owner already turned it on — nothing to nudge about
        notice_count = int(config.get("wordfilter_notice_count") or 0)
        if notice_count >= MAX_WORDFILTER_NOTICES:
            return None
        if notice_count == 0:
            # Guild the bot was already in before this feature shipped (or
            # rejoined while offline, so on_guild_join's first notify never
            # fired) — surface the same notice #1 an on_guild_join would have.
            return {
                "type": "word_filter", "guild_id": guild.id, "guild_name": guild.name,
                "notice_number": 1, "max_notices": MAX_WORDFILTER_NOTICES,
            }
        last_notice_at = config.get("wordfilter_last_notice_at")
        if last_notice_at and (now - last_notice_at) < WORDFILTER_REMINDER_GAP:
            return None
        return {
            "type": "word_filter", "guild_id": guild.id, "guild_name": guild.name,
            "notice_number": notice_count + 1, "max_notices": MAX_WORDFILTER_NOTICES,
        }

    async def _resolve_owner(self, guild: discord.Guild):
        try:
            owner = guild.owner or (await guild.fetch_owner() if guild.owner_id else None)
        except (discord.HTTPException, discord.Forbidden):
            return None
        return owner if (owner is not None and not owner.bot) else None

    async def _send_combined_reminder(self, owner: discord.User, items: list, clone_id):
        """One DM per owner per tick, covering every pending item across
        every guild of theirs collected this tick — instead of the old
        _notify_owner_log_channel / _notify_owner_word_filter each firing
        its own separate DM. A single "Remind me later" / "Don't ask
        again" button pair (see _views_automod_reminders.py) applies to
        every item in the message at once.

        Creates the DB batch row BEFORE sending so the buttons have a real
        batch id to key off of the moment the message exists; deletes that
        row again if the send turns out to fail for a known reason (DMs
        closed, etc.) rather than leaving an orphaned row nothing will
        ever reference. Per-item notice_count/last_notice_at are only
        bumped AFTER a successful send — same "don't burn a slot on an
        unknown failure" care _notify_owner_word_filter used to take,
        just applied to the whole batch instead of one item."""
        if owner is None or not items:
            return
        batch_id = await db.create_automod_reminder_batch(clone_id, owner.id, items)

        embed = discord.Embed(
            title="🔔 A few things need your attention",
            color=discord.Color.blurple(),
            timestamp=discord.utils.utcnow(),
        )
        for item in items:
            final = item["notice_number"] >= item["max_notices"]
            if item["type"] == "log_channel":
                name = f"📋 Log channel — {item['guild_name']}"
                value = (
                    f"<#{item['channel_id']}> is logging server activity"
                    + (" (last reminder about this)" if final else "")
                    + ". Change or turn it off with `/automod setlogchannel` in that server."
                )
            else:
                name = f"🛡️ Word filter off — {item['guild_name']}"
                value = (
                    "Still off, so nothing gets blocked"
                    + (" (last reminder about this)" if final else "")
                    + ". Run `/automod toggle filter:word_filter enabled:True` then "
                    "`/automod bannedword preset` in that server."
                )
            embed.add_field(name=name, value=value, inline=False)
        embed.set_footer(text="Or use the buttons below.")
        view = build_reminder_view(batch_id)

        try:
            dm_channel = owner.dm_channel or await owner.create_dm()
            message = await dm_channel.send(embed=embed, view=view)
        except (discord.HTTPException, discord.Forbidden, discord.NotFound):
            # Known, final outcome — nothing will ever back this batch row.
            logger.info(f"Could not DM combined automod reminder to owner {owner.id}")
            await db.delete_automod_reminder_batch(batch_id)
            return
        except Exception:
            # Unknown failure (network blip, timeout, ...) — don't burn the
            # notice slots for a run of bad luck; let the next tick retry
            # with a fresh batch. Clean up this dead-end row either way.
            await db.delete_automod_reminder_batch(batch_id)
            raise

        await db.set_automod_reminder_batch_message(batch_id, dm_channel.id, message.id)

        for item in items:
            if item["type"] == "log_channel":
                await db.set_automod_config(
                    item["guild_id"], clone_id=clone_id,
                    log_channel_notice_count=item["notice_number"],
                    log_channel_last_notice_at=discord.utils.utcnow(),
                )
            else:
                await db.set_automod_config(
                    item["guild_id"], clone_id=clone_id,
                    wordfilter_notice_count=item["notice_number"],
                    wordfilter_last_notice_at=discord.utils.utcnow(),
                )

    @tasks.loop(hours=12)
    async def _reminder_loop(self):
        """Two independent, idempotent jobs per guild, safe to run on every
        tick and every restart:
          1. Log channel — backfill + up to MAX_LOG_CHANNEL_NOTICES owner
             DMs, spaced by LOG_CHANNEL_REMINDER_GAP. Manually-configured
             log channels (log_channel_auto_created=False) are never
             touched — see ensure_log_channel.
          2. Word filter — up to MAX_WORDFILTER_NOTICES owner DMs nudging
             them to turn word_filter_enabled on, spaced by
             WORDFILTER_REMINDER_GAP. Stops the moment an owner turns the
             filter on themselves — see the toggle command.

        Both jobs are collected first (per guild) WITHOUT sending
        anything, then grouped by owner so an owner of several guilds gets
        ONE combined DM instead of one per guild/type — see
        _send_combined_reminder. This is also what keeps a post-downtime
        catch-up pass from bursting several separate messages at an owner
        in the same minute: however many items piled up while offline
        still collapse into a single DM per owner per tick."""
        clone_id = getattr(self.bot, "clone_id", None)
        now = discord.utils.utcnow()
        owner_items: dict = {}
        owner_objs: dict = {}
        for guild in list(self.bot.guilds):
            try:
                config = await db.get_automod_config(guild.id, clone_id=clone_id)
                log_item = await self._collect_log_channel_item(guild, config, clone_id, now)
                wf_item = await self._collect_word_filter_item(guild, config, clone_id, now)
                pending = [i for i in (log_item, wf_item) if i is not None]
                if not pending:
                    continue
                owner = await self._resolve_owner(guild)
                if owner is None:
                    continue
                owner_items.setdefault(owner.id, []).extend(pending)
                owner_objs[owner.id] = owner
            except Exception:
                logger.exception(f"_reminder_loop failed collecting for guild {guild.id}")

        for owner_id, items in owner_items.items():
            try:
                await self._send_combined_reminder(owner_objs[owner_id], items, clone_id)
            except Exception:
                logger.exception(f"_reminder_loop failed sending combined reminder to owner {owner_id}")
            await asyncio.sleep(1)  # spread DMs out instead of bursting

    @_reminder_loop.before_loop
    async def _before_reminder_loop(self):
        await self.bot.wait_until_ready()

    # ── log-channel helper ──────────────────────────────────────────────
    async def _send_log(self, guild: discord.Guild, config: dict, embed: discord.Embed):
        channel_id = config.get("log_channel_id")
        if not channel_id:
            return
        channel = guild.get_channel(int(channel_id))
        if channel is None:
            return
        try:
            await channel.send(embed=embed)
        except discord.Forbidden:
            pass

    # ── enforcement ──────────────────────────────────────────────────────
    async def _enforce(self, message: discord.Message, config: dict, reason: str):
        """Apply config['action'] to a violating message/member."""
        action = config.get("action") or "delete"
        t0 = time.monotonic()
        try:
            # Mark this id BEFORE deleting so the on_message_delete listener
            # (which will fire from Discord's own gateway event once this
            # delete goes through) knows to skip it instead of double-logging.
            self._self_deleted_ids.add(message.id)
            await message.delete()
            logger.info(
                "[automod] deleted msg %s in guild %s after %.2fs (reason=%s)",
                message.id, message.guild.id, time.monotonic() - t0, reason,
            )
        except (discord.Forbidden, discord.NotFound) as e:
            # Delete didn't actually happen — don't leave a stale id around
            # to accidentally suppress a legitimate future delete log.
            self._self_deleted_ids.discard(message.id)
            logger.warning(
                "[automod] delete FAILED for msg %s in guild %s after %.2fs: %s",
                message.id, message.guild.id, time.monotonic() - t0, e,
            )

        if action == "delete":
            pass  # already done above
        elif action == "warn":
            await mod.add_warn(message.author.id, message.guild.id, self.bot.user.id, reason=f"[automod] {reason}")
        elif action == "timeout":
            minutes = int(config.get("timeout_minutes") or 10)
            try:
                await message.author.timeout(timedelta(minutes=minutes), reason=f"[automod] {reason}")
            except discord.Forbidden:
                pass
        elif action == "kick":
            try:
                await message.author.kick(reason=f"[automod] {reason}")
            except discord.Forbidden:
                pass

        await modx.log_action(message.guild.id, f"automod_{action}", self.bot.user.id,
                               target_user_id=message.author.id, reason=reason)

        embed = discord.Embed(
            title="🛡️ Auto-mod action",
            description=f"**User:** {message.author.mention}\n**Action:** {action}\n**Reason:** {reason}",
            color=discord.Color.orange(),
        )
        await self._send_log(message.guild, config, embed)

    # ── message filters ─────────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None:
            return
        member = message.author
        if isinstance(member, discord.Member) and member.guild_permissions.manage_messages:
            return  # never automod moderators/admins

        received_at = time.monotonic()
        logger.info(
            "[automod] on_message received msg %s in guild %s at %s",
            message.id, message.guild.id, datetime.utcnow().isoformat(),
        )

        clone_id = getattr(self.bot, "clone_id", None)
        config = await db.get_automod_config(message.guild.id, clone_id=clone_id)
        logger.info(
            "[automod] config fetched for msg %s after %.2fs",
            message.id, time.monotonic() - received_at,
        )

        if config.get("anti_invite_enabled") and INVITE_RE.search(message.content or ""):
            await self._enforce(message, config, "Posted a Discord invite link")
            return

        if config.get("anti_mention_enabled"):
            threshold = int(config.get("anti_mention_threshold") or 5)
            mention_count = len(message.mentions) + len(message.role_mentions)
            if mention_count >= threshold:
                await self._enforce(message, config, f"Mass mention ({mention_count} mentions)")
                return

        if config.get("word_filter_enabled"):
            content = (message.content or "").lower()
            matched = next((w for w in config.get("banned_words", []) if w in content), None)
            if matched:
                await self._enforce(message, config, f"Used a blocked word/phrase (\"{matched}\")")
                return

        # Flood tracking is always recorded (cheap, and needed so a
        # just-enabled spam filter has history to judge against); enforcement
        # itself stays opt-in via spam_enabled.
        await modx.record_flood_event(message.guild.id, member.id, (message.content or "")[:200])
        if config.get("spam_enabled"):
            window = int(config.get("spam_flood_window_seconds") or 10)
            flood_threshold = int(config.get("spam_flood_threshold") or 10)
            count = await modx.count_recent_flood_events(message.guild.id, member.id, window_seconds=window)
            if count >= flood_threshold:
                await modx.clear_flood_events(message.guild.id, member.id)
                await self._enforce(message, config, f"Flooding ({count} messages in {window}s)")
                return

    # ── raid protection: minimum account age on join ───────────────────
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        clone_id = getattr(self.bot, "clone_id", None)
        config = await db.get_automod_config(member.guild.id, clone_id=clone_id)
        min_hours = int(config.get("min_account_age_hours") or 0)
        if min_hours <= 0:
            return
        age = datetime.now(timezone.utc) - member.created_at
        if age < timedelta(hours=min_hours):
            try:
                await member.kick(reason=f"[automod] Account age {age.days}d below {min_hours}h minimum (raid protection)")
                embed = discord.Embed(
                    title="🛡️ Auto-mod action",
                    description=f"**User:** {member.mention}\n**Action:** kick\n"
                                f"**Reason:** Account created {age.days}d ago, below {min_hours}h minimum",
                    color=discord.Color.orange(),
                )
                await self._send_log(member.guild, config, embed)
            except discord.Forbidden:
                pass

    # ── deleted/edited message + join/leave logging ─────────────────────
    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message):
        if message.guild is None or message.author.bot:
            return
        if message.id in self._self_deleted_ids:
            # Already logged as a "🛡️ Auto-mod action" embed by _enforce();
            # this gateway event is just Discord confirming that delete, so
            # skip it here to avoid a duplicate "🗑️ Message deleted" entry.
            self._self_deleted_ids.discard(message.id)
            return
        clone_id = getattr(self.bot, "clone_id", None)
        config = await db.get_automod_config(message.guild.id, clone_id=clone_id)
        if not config.get("log_channel_id"):
            return
        embed = discord.Embed(
            title="🗑️ Message deleted",
            description=(message.content or "*(no text content)*")[:1000],
            color=discord.Color.red(),
        )
        embed.set_author(name=str(message.author), icon_url=getattr(message.author.display_avatar, "url", None))
        embed.add_field(name="Channel", value=message.channel.mention)
        await self._send_log(message.guild, config, embed)

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        if before.guild is None or before.author.bot or before.content == after.content:
            return
        clone_id = getattr(self.bot, "clone_id", None)
        config = await db.get_automod_config(before.guild.id, clone_id=clone_id)
        if not config.get("log_channel_id"):
            return
        embed = discord.Embed(title="✏️ Message edited", color=discord.Color.gold())
        embed.set_author(name=str(before.author), icon_url=getattr(before.author.display_avatar, "url", None))
        embed.add_field(name="Before", value=(before.content or "*(empty)*")[:500], inline=False)
        embed.add_field(name="After", value=(after.content or "*(empty)*")[:500], inline=False)
        embed.add_field(name="Channel", value=before.channel.mention)
        await self._send_log(before.guild, config, embed)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        clone_id = getattr(self.bot, "clone_id", None)
        config = await db.get_automod_config(member.guild.id, clone_id=clone_id)
        if not config.get("log_channel_id"):
            return
        embed = discord.Embed(description=f"📤 {member.mention} left or was removed.", color=discord.Color.dark_grey())
        await self._send_log(member.guild, config, embed)

    # ── slash commands ──────────────────────────────────────────────────
    group = app_commands.guild_only()(app_commands.Group(name="automod", description="Configure auto-moderation for this server"))

    @group.command(name="status", description="Show current auto-moderation settings")
    async def status(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        lang = await get_lang(interaction)
        s = await db.get_automod_config(interaction.guild_id, clone_id=_clone_id_of(interaction))
        not_set = await tr("not set", lang)
        action_label = await tr("Action on violation", lang)
        action_value = f"{s.get('action', 'delete')}" + (f" ({s.get('timeout_minutes', 10)}min)" if s.get("action") == "timeout" else "")
        word_filter_label = await tr("Word filter", lang)
        word_filter_value = ("✅" if s.get("word_filter_enabled") else "❌") + f" ({len(s.get('banned_words', []))} word(s))"
        invite_label = await tr("Invite link filter", lang)
        invite_value = "✅" if s.get("anti_invite_enabled") else "❌"
        mention_label = await tr("Mass mention filter", lang)
        mention_value = ("✅" if s.get("anti_mention_enabled") else "❌") + f" (threshold: {s.get('anti_mention_threshold', 5)})"
        spam_label = await tr("Spam/flood auto-action", lang)
        spam_value = ("✅" if s.get("spam_enabled") else "❌") + f" ({s.get('spam_flood_threshold', 10)} msgs / {s.get('spam_flood_window_seconds', 10)}s)"
        age_label = await tr("Min account age gate", lang)
        age_value = f"{s.get('min_account_age_hours', 0)}h" if s.get("min_account_age_hours") else "❌"
        log_label = await tr("Log channel", lang)
        log_value = f"<#{s['log_channel_id']}>" if s.get("log_channel_id") else not_set

        lines = [
            f"{action_label}: {action_value}", f"{word_filter_label}: {word_filter_value}",
            f"{invite_label}: {invite_value}", f"{mention_label}: {mention_value}",
            f"{spam_label}: {spam_value}", f"{age_label}: {age_value}", f"{log_label}: {log_value}",
        ]
        buttons = [
            refresh_button(self, "status"),
            ActionButton("Blocked words", discord.ButtonStyle.secondary, self, "bw_list", emoji="🚫"),
        ]
        card = NavCardView("Auto-moderation settings", lines, discord.Color.blurple(), buttons)
        await interaction.followup.send(view=card, ephemeral=True)

    @group.command(name="setup", description="Set up auto-moderation with a guided step-by-step wizard")
    async def setup_wizard(self, interaction: discord.Interaction):
        await interaction.response.defer()
        if not _require_perm(interaction, "manage_guild"):
            lang = await get_lang(interaction)
            await _deny(interaction, "Manage Server", lang)
            return
        config = await db.get_automod_config(interaction.guild_id, clone_id=_clone_id_of(interaction))
        view = build_automod_wizard_view(interaction.guild_id, _clone_id_of(interaction), interaction.user.id, config)
        await interaction.followup.send(view=view)
        sent = await interaction.original_response()
        await remember_automod_wizard_message(
            interaction.guild_id, _clone_id_of(interaction), interaction.user.id, sent.channel.id, sent.id
        )

    @group.command(name="panel", description="Toggle auto-mod filters on/off from buttons")
    async def panel(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        lang = await get_lang(interaction)
        if not _require_perm(interaction, "manage_guild"):
            await _deny(interaction, "Manage Server", lang)
            return
        config = await db.get_automod_config(interaction.guild_id, clone_id=_clone_id_of(interaction))
        view = AutomodPanelView(config, interaction.user.id, clone_id=_clone_id_of(interaction))
        await interaction.followup.send(view=view, ephemeral=True)

    @group.command(name="toggle", description="Turn a filter on or off")
    @app_commands.describe(filter="Which filter", enabled="On or off")
    @app_commands.choices(filter=[app_commands.Choice(name=f, value=f) for f in sorted(VALID_FILTERS)])
    async def toggle(self, interaction: discord.Interaction, filter: app_commands.Choice[str], enabled: bool):
        await interaction.response.defer(ephemeral=True)
        lang = await get_lang(interaction)
        if not _require_perm(interaction, "manage_guild"):
            await _deny(interaction, "Manage Server", lang)
            return
        field_map = {
            "word_filter": "word_filter_enabled",
            "anti_invite": "anti_invite_enabled",
            "anti_mention": "anti_mention_enabled",
            "spam": "spam_enabled",
        }
        await db.set_automod_config(interaction.guild_id, clone_id=_clone_id_of(interaction), **{field_map[filter.value]: enabled})
        if filter.value == "word_filter" and enabled:
            # Owner turned it on themselves — stop any pending reminder DMs
            # for this guild permanently (see _collect_word_filter_item).
            await db.set_automod_config(interaction.guild_id, clone_id=_clone_id_of(interaction), wordfilter_notice_count=MAX_WORDFILTER_NOTICES)
        await refresh_automod_wizard(interaction.client, interaction.guild_id, clone_id=_clone_id_of(interaction))
        state = await tr("enabled", lang) if enabled else await tr("disabled", lang)
        msg = await tr("✅ {filter} {state}.", lang, filter=filter.value, state=state)
        await interaction.followup.send(msg, ephemeral=True)

    @group.command(name="action", description="Set what happens when a filter is triggered")
    @app_commands.choices(action=[app_commands.Choice(name=a, value=a) for a in sorted(VALID_ACTIONS)])
    async def action(self, interaction: discord.Interaction, action: app_commands.Choice[str], timeout_minutes: app_commands.Range[int, 1, 40320] = 10):
        await interaction.response.defer(ephemeral=True)
        lang = await get_lang(interaction)
        if not _require_perm(interaction, "manage_guild"):
            await _deny(interaction, "Manage Server", lang)
            return
        await db.set_automod_config(interaction.guild_id, clone_id=_clone_id_of(interaction), action=action.value, timeout_minutes=timeout_minutes)
        await refresh_automod_wizard(interaction.client, interaction.guild_id, clone_id=_clone_id_of(interaction))
        msg = await tr("✅ Violations now trigger: **{action}**.", lang, action=action.value)
        await interaction.followup.send(msg, ephemeral=True)

    @group.command(name="mentionthreshold", description="Set how many mentions in one message counts as spam")
    async def mentionthreshold(self, interaction: discord.Interaction, count: app_commands.Range[int, 2, 50]):
        await interaction.response.defer(ephemeral=True)
        lang = await get_lang(interaction)
        if not _require_perm(interaction, "manage_guild"):
            await _deny(interaction, "Manage Server", lang)
            return
        await db.set_automod_config(interaction.guild_id, clone_id=_clone_id_of(interaction), anti_mention_threshold=count)
        await refresh_automod_wizard(interaction.client, interaction.guild_id, clone_id=_clone_id_of(interaction))
        msg = await tr("✅ Mass-mention threshold set to {count}.", lang, count=count)
        await interaction.followup.send(msg, ephemeral=True)

    @group.command(name="minaccountage", description="Kick new joiners whose account is younger than this (raid protection). 0 disables it.")
    async def minaccountage(self, interaction: discord.Interaction, hours: app_commands.Range[int, 0, 8760]):
        await interaction.response.defer(ephemeral=True)
        lang = await get_lang(interaction)
        if not _require_perm(interaction, "manage_guild"):
            await _deny(interaction, "Manage Server", lang)
            return
        await db.set_automod_config(interaction.guild_id, clone_id=_clone_id_of(interaction), min_account_age_hours=hours)
        msg = (
            await tr("✅ Minimum account age gate set to {hours}h.", lang, hours=hours) if hours
            else await tr("✅ Minimum account age gate disabled.", lang)
        )
        await interaction.followup.send(msg, ephemeral=True)

    @group.command(name="setlogchannel", description="Set (or clear) the channel auto-mod and moderation actions log to")
    async def setlogchannel(self, interaction: discord.Interaction, channel: discord.TextChannel = None):
        await interaction.response.defer(ephemeral=True)
        lang = await get_lang(interaction)
        if not _require_perm(interaction, "manage_guild"):
            await _deny(interaction, "Manage Server", lang)
            return
        # Picking a channel by hand (or explicitly clearing it) means the
        # admin is now driving — stop the auto-created-channel reminder
        # DMs to the owner regardless of which channel this is.
        await db.set_automod_config(
            interaction.guild_id, clone_id=_clone_id_of(interaction),
            log_channel_id=channel.id if channel else None,
            log_channel_auto_created=False, log_channel_notice_count=0, log_channel_last_notice_at=None,
        )
        await refresh_automod_wizard(interaction.client, interaction.guild_id, clone_id=_clone_id_of(interaction))
        msg = (
            await tr("✅ Log channel set to {channel}.", lang, channel=channel.mention) if channel
            else await tr("✅ Log channel cleared.", lang)
        )
        await interaction.followup.send(msg, ephemeral=True)

    @group.command(name="dashboard", description="Get a link to configure auto-moderation from a web dashboard")
    async def dashboard(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        lang = await get_lang(interaction)
        if not _require_perm(interaction, "manage_guild"):
            await _deny(interaction, "Manage Server", lang)
            return
        clone_id = _clone_id_of(interaction)
        token = await db.get_or_create_dashboard_token(interaction.guild_id, clone_id=clone_id)
        url = f"{DASHBOARD_BASE_URL}/dashboard/{interaction.guild_id}?token={token}"
        if clone_id is not None:
            url += f"&clone_id={clone_id}"
        msg = await tr(
            "🔧 Dashboard link (keep this private — it grants config access, same as a password):\n{url}",
            lang, url=url
        )
        await interaction.followup.send(msg, ephemeral=True)

    bw_group = app_commands.Group(name="bannedword", description="Manage the blocked word/phrase list", parent=group)

    @bw_group.command(name="add", description="Add a blocked word or phrase")
    async def bw_add(self, interaction: discord.Interaction, word: str):
        await interaction.response.defer(ephemeral=True)
        lang = await get_lang(interaction)
        if not _require_perm(interaction, "manage_guild"):
            await _deny(interaction, "Manage Server", lang)
            return
        ok = await db.add_automod_banned_word(interaction.guild_id, word, clone_id=_clone_id_of(interaction))
        if ok:
            await refresh_automod_wizard(interaction.client, interaction.guild_id, clone_id=_clone_id_of(interaction))
        msg = await tr("✅ Added.", lang) if ok else await tr("❌ Already on the list (or invalid).", lang)
        await interaction.followup.send(msg, ephemeral=True)

    @bw_group.command(name="preset", description="Bulk-load a curated bad-words list into the filter")
    async def bw_preset(self, interaction: discord.Interaction):
        lang = await get_lang(interaction)
        if not _require_perm(interaction, "manage_guild"):
            await _deny(interaction, "Manage Server", lang)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            with open(PRESET_BANNED_WORDS_PATH, encoding="utf-8") as f:
                preset_words = [line.strip() for line in f if line.strip()]
        except OSError:
            msg = await tr("❌ Preset list file is missing on the bot host.", lang)
            await interaction.followup.send(msg, ephemeral=True)
            return
        added = await db.add_automod_banned_words_bulk(
            interaction.guild_id, preset_words, clone_id=_clone_id_of(interaction)
        )
        await refresh_automod_wizard(interaction.client, interaction.guild_id, clone_id=_clone_id_of(interaction))
        msg = await tr(
            "✅ Added {added} word(s)/phrase(s) from the preset list ({skipped} already on your list).",
            lang, added=added, skipped=len(preset_words) - added,
        )
        await interaction.followup.send(msg, ephemeral=True)

    @bw_group.command(name="remove", description="Remove a blocked word or phrase")
    async def bw_remove(self, interaction: discord.Interaction, word: str):
        await interaction.response.defer(ephemeral=True)
        lang = await get_lang(interaction)
        if not _require_perm(interaction, "manage_guild"):
            await _deny(interaction, "Manage Server", lang)
            return
        ok = await db.remove_automod_banned_word(interaction.guild_id, word, clone_id=_clone_id_of(interaction))
        if ok:
            await refresh_automod_wizard(interaction.client, interaction.guild_id, clone_id=_clone_id_of(interaction))
        msg = await tr("✅ Removed.", lang) if ok else await tr("That word wasn't on the list.", lang)
        await interaction.followup.send(msg, ephemeral=True)

    @bw_group.command(name="list", description="List blocked words/phrases")
    async def bw_list(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        lang = await get_lang(interaction)
        config = await db.get_automod_config(interaction.guild_id, clone_id=_clone_id_of(interaction))
        words = config.get("banned_words", [])
        line = ", ".join(f"`{w}`" for w in words) if words else await tr("No blocked words configured.", lang)
        buttons = [
            refresh_button(self, "bw_list"),
            ActionButton("Automod status", discord.ButtonStyle.secondary, self, "status", emoji="🛡️"),
        ]
        card = NavCardView("Blocked words", [line], discord.Color.blurple(), buttons)
        await interaction.followup.send(view=card, ephemeral=True)

    # --- /automod owner (bot owner only) --------------------------------

    owner_group = app_commands.Group(name="owner", description="Bot-owner controls for automod reminder DMs", parent=group)

    @owner_group.command(
        name="cleanupreminders",
        description="Delete automod reminder DMs the bot has sent, old and new (owner only)",
    )
    async def owner_cleanup_reminders(self, interaction: discord.Interaction):
        """One-off + ongoing cleanup, same spirit as bump.py's
        /bumpadmin cleanup_reminders: deletes both

          1. Combined reminder DMs this cog tracks in
             discord_automod_reminder_batches (exact message ids — see
             _send_combined_reminder), and
          2. Any of the OLD standalone per-guild-per-type DMs still
             sitting in an owner's DMs from before that combining change,
             recognized by embed title (LEGACY_REMINDER_EMBED_TITLES)
             since no message ids were ever tracked for those.

        Safe to run more than once — nothing here fails if a message or
        row is already gone by the time this runs."""
        if not _require_bot_owner(interaction.user.id):
            await interaction.response.send_message("This is owner-only.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)

        clone_id = getattr(self.bot, "clone_id", None)
        deleted = 0

        # 1. Tracked combined-DM batches — exact channel_id/message_id.
        batches = await db.list_automod_reminder_batches(clone_id)
        resolved_batch_ids = []
        for batch in batches:
            try:
                channel = self.bot.get_channel(int(batch["channel_id"])) or await self.bot.fetch_channel(int(batch["channel_id"]))
                message = await channel.fetch_message(int(batch["message_id"]))
                await message.delete()
                deleted += 1
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass  # already deleted, DM channel gone, etc. — fine either way
            resolved_batch_ids.append(batch["id"])
        await db.delete_automod_reminder_batches(resolved_batch_ids)

        # 2. Legacy standalone DMs — scan each current guild owner's DM
        # history for the bot's own messages matching the old embed titles.
        # Best-effort per owner: one owner's DMs being unreachable doesn't
        # stop the rest from being checked.
        checked_owners = set()
        for guild in list(self.bot.guilds):
            owner = await self._resolve_owner(guild)
            if owner is None or owner.id in checked_owners:
                continue
            checked_owners.add(owner.id)
            try:
                dm_channel = owner.dm_channel or await owner.create_dm()

                def _is_legacy_reminder(m: discord.Message) -> bool:
                    return (
                        m.author.id == self.bot.user.id
                        and bool(m.embeds)
                        and any(e.title in LEGACY_REMINDER_EMBED_TITLES for e in m.embeds)
                    )

                async for msg in dm_channel.history(limit=200):
                    if _is_legacy_reminder(msg):
                        try:
                            await msg.delete()
                            deleted += 1
                        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                            pass
            except (discord.Forbidden, discord.HTTPException):
                continue

        await interaction.followup.send(
            f"🧹 Deleted **{deleted}** automod reminder message(s) across **{len(checked_owners)}** owner DM(s).",
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(AutomodCog(bot))
