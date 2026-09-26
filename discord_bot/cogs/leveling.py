# path: discord_bot/cogs/leveling.py

"""
Message-based leveling — Discord equivalent of ProBot's XP system. Award is
capped by a per-user cooldown (not a per-server global cooldown) so it can't
be farmed by rapid-fire short messages, and deliberately does NOT touch
modules/economy's currency (see discord-bot-expansion-spec.md's decision to
keep XP and economy currency as two separate systems).

Level-up role rewards currently STACK (grant every configured role at or
below the new level that the member doesn't already have, rather than only
the exact-match one) — but stack-vs-replace was explicitly left as an open
question for the project owner in discord-bot-expansion-spec.md §4.3, not
decided. This was implemented as "stack" without that confirmation. Treat
as provisional: if the owner says "replace", `_grant_level_roles` below
needs to remove any lower-level role it previously granted, not just add
the new one.
"""

import asyncio
import io
import logging
import random

import aiohttp
import discord
from discord_bot import perm_check
from discord import app_commands
from discord.ext import commands, tasks
from discord_bot.cogs._dm_support import GuildOnlyCog

from database import db
from modules import leveling
from modules import clan_cards
from modules import godhood_cards
from modules.level_card import (
    render_level_card, render_level_card_evolved,
    render_level_card_tiered, get_tier_image_for_level,
)
from discord_bot.cogs._views_shared import ActionButton, NavCardView
from discord_bot.cogs._views_leveling_leaderboard import build_leaderboard_view
from config import DISCORD_CLONE_ADMIN_IDS
from discord_bot.cogs._views_leveling_wizard import (
    build_wizard_view as build_leveling_wizard_view,
    remember_wizard_message as remember_leveling_wizard_message,
    refresh_posted_wizard as refresh_leveling_wizard,
)

logger = logging.getLogger(__name__)

XP_MIN = 15
XP_MAX = 25
XP_COOLDOWN_SECONDS = 60

XP_RATE_MULTIPLIERS = {"slow": 0.5, "default": 1.0, "fast": 1.5}

# Maximum combined XP multiplier (xp_rate * per-user boost * server boost).
# Prevents runaway stacking from making the leaderboard uncompetitive.
MAX_XP_MULTIPLIER = 10.0


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


def _clone_id_of(interaction: discord.Interaction):
    """Same convention as premium.py: None on the main bot, the clone's row
    id on a clone process. Threaded into every XP/level-role query so a
    clone's leveling data never mixes with the main bot's (or another
    clone's) in a guild both are running in."""
    return getattr(interaction.client, "clone_id", None)


async def _deny(interaction: discord.Interaction, perm_name: str):
    msg = f"You need the **{perm_name}** permission to do that."
    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)


def _progress_bar(current: int, needed: int, width: int = 20) -> str:
    filled = int(width * (current / needed)) if needed else 0
    return "█" * filled + "░" * (width - filled)


class LevelingCog(GuildOnlyCog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # In-memory per-(guild_id, user_id) cooldown tracker. Resets on
        # restart, which just means one extra XP award is possible right
        # after a deploy — harmless, not worth a DB round-trip per message
        # just to persist a 60-second cooldown.
        # No more in-memory cooldown dict here — the cooldown is now
        # enforced atomically in db.add_xp (cooldown_seconds=), which
        # holds across every process, not just this one. See add_xp's
        # docstring for why an in-memory dict caused double level-up
        # messages whenever two bot processes briefly overlapped.
        if not self._leaderboard_autopost_loop.is_running():
            self._leaderboard_autopost_loop.start()
        if not self._godhood_deadline_loop.is_running():
            self._godhood_deadline_loop.start()

    def cog_unload(self):
        self._leaderboard_autopost_loop.cancel()
        self._godhood_deadline_loop.cancel()

    async def _ensure_announce_channel(self, guild: discord.Guild, config: dict, clone_id=None):
        """Serialises channel creation per (guild, clone) so several members
        levelling up at the same moment can't each create their own
        #level-ups channel. The fast path (channel already recorded and
        still exists) takes no lock. Otherwise we wait for the lock and then
        RE-READ the config — the `config` we were handed was fetched before
        any of the waiting tasks ran, so it can't be trusted to show a
        channel another task just created. See _ensure_announce_channel_locked
        for the actual resolution rules."""
        existing_id = config.get("announce_channel_id")
        if existing_id:
            found = guild.get_channel(int(existing_id))
            if found is not None:
                return found
        locks = self.__dict__.setdefault("_announce_locks", {})
        lock = locks.setdefault((guild.id, clone_id), asyncio.Lock())
        async with lock:
            fresh = await db.get_leveling_config(guild.id, clone_id=clone_id)
            return await self._ensure_announce_channel_locked(guild, fresh, clone_id)

    async def _ensure_announce_channel_locked(self, guild: discord.Guild, config: dict, clone_id=None):
        """Returns a channel to post level-ups in. If announce_channel_id
        is unset (or was auto-created and then deleted), creates a
        dedicated #level-ups channel once and remembers it — same pattern
        as automod's ensure_log_channel — instead of falling back to
        whatever channel the level-up message happened to land in, which
        is what was spamming random channels before this.

        If an admin deliberately picked a channel (announce_auto_created
        is False) and it later gets deleted, this does NOT silently
        replace their choice with a new auto-created one — falls back to
        message.channel for that one send, same as before, so it doesn't
        override a deliberate admin decision without them noticing.

        discord_leveling_config is keyed per (guild_id, clone_id), so the
        main bot and every clone running in the same guild each have
        their own row here — without the cross-clone check below, each
        one independently sees no announce_channel_id set on ITS row and
        creates its own separate #level-ups channel the first time
        someone levels up on that process. Before creating anything, we
        check every OTHER clone_id's config for this guild (via
        get_guild_leveling_announce_channels) for a channel that still
        actually exists, and adopt it into our own config instead of
        making a new one. A manually-picked (non-auto-created) channel
        wins over an auto-created one, since that reflects a deliberate
        admin choice made on whichever process the admin happened to
        configure.
        """
        existing_id = config.get("announce_channel_id")
        if existing_id:
            found = guild.get_channel(int(existing_id))
            if found is not None:
                return found
            if not config.get("announce_auto_created"):
                return None

        # No usable channel recorded under THIS clone_id yet — see if the
        # main bot or another clone in this same guild already has one
        # before creating a brand new #level-ups channel.
        candidates = await db.get_guild_leveling_announce_channels(guild.id)
        manual_match = None
        auto_match = None
        for row in candidates:
            if row["clone_id"] == clone_id:
                continue  # our own row, already checked above
            found = guild.get_channel(int(row["announce_channel_id"]))
            if found is None:
                continue  # stale reference on that clone's config; skip
            if row["announce_auto_created"]:
                auto_match = auto_match or found
            else:
                manual_match = manual_match or found
        reused = manual_match or auto_match
        if reused is not None:
            await db.set_leveling_config(
                guild.id, clone_id=clone_id,
                announce_channel_id=reused.id,
                announce_auto_created=(reused is auto_match),
            )
            return reused

        # A #level-ups channel may already exist (made by a process that beat
        # us to it, or after a DB reset) — adopt it instead of making a twin.
        same_name = discord.utils.get(guild.text_channels, name="level-ups")
        if same_name is not None and same_name.permissions_for(guild.me).send_messages:
            await db.set_leveling_config(
                guild.id, clone_id=clone_id,
                announce_channel_id=same_name.id, announce_auto_created=True,
            )
            return same_name

        if not guild.me.guild_permissions.manage_channels:
            return None

        try:
            channel = await guild.create_text_channel(
                "level-ups",
                reason="PRIME-BOT: auto-created level-up announcement channel",
            )
        except (discord.Forbidden, discord.HTTPException) as e:
            logger.info(f"[leveling] couldn't auto-create level-ups channel in {guild.id}: {e}")
            return None

        await db.set_leveling_config(
            guild.id, clone_id=clone_id,
            announce_channel_id=channel.id, announce_auto_created=True,
        )
        return channel

    async def _grant_level_roles(self, member: discord.Member, new_level: int, clone_id=None):
        role_rows = await db.get_level_roles(member.guild.id, clone_id=clone_id)
        for row in role_rows:
            if row["level"] > new_level:
                break
            role = member.guild.get_role(row["role_id"])
            if role and role not in member.roles:
                try:
                    await member.add_roles(role, reason=f"Reached level {new_level}")
                except discord.Forbidden:
                    logger.warning(f"[v0] Couldn't grant level role {role.id} in guild {member.guild.id} — check role hierarchy")
                    perm_check.flag(
                        member.guild.id, clone_id, f"level_role_{role.id}",
                        "Can't hand out a level reward — " + (perm_check.role_problem(member.guild, role) or "check my role hierarchy."),
                    )

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None:
            return
        clone_id = getattr(self.bot, "clone_id", None)
        current = await db.get_xp(message.guild.id, message.author.id, clone_id=clone_id)
        old_level = leveling.compute_level(current["total_xp"])
        config = await db.get_leveling_config(message.guild.id, clone_id=clone_id)
        multiplier = XP_RATE_MULTIPLIERS.get(config.get("xp_rate", "default"), 1.0)
        # Per-user paid XP boost (see leveling-boost-build-prompt.md §2) —
        # stacks multiplicatively on top of the guild's own xp_rate setting,
        # not instead of it. get_active_xp_boost already filters out expired
        # rows (expires_at > NOW()), so no separate expiry check is needed
        # here.
        boost = await db.get_active_xp_boost(message.guild.id, message.author.id, clone_id=clone_id)
        if boost:
            multiplier *= float(boost["multiplier"])
        guild_boost = await db.get_active_guild_xp_boost(message.guild.id, clone_id=clone_id)
        if guild_boost:
            multiplier *= float(guild_boost["multiplier"])
        # Cap total stacked multiplier so no single member can pull away
        # uncompetitively regardless of how many boosts are layered.
        multiplier = min(multiplier, MAX_XP_MULTIPLIER)
        gained = max(1, round(random.randint(XP_MIN, XP_MAX) * multiplier))
        new_level_guess = leveling.compute_level(current["total_xp"] + gained)
        # cooldown_seconds makes this atomic across processes — see
        # add_xp's docstring. If this call lost the race (already on
        # cooldown, including a duplicate process's call for the same
        # message), row is None and we must not send anything.
        row = await db.add_xp(
            message.guild.id, message.author.id, gained, new_level_guess,
            clone_id=clone_id, cooldown_seconds=XP_COOLDOWN_SECONDS,
        )
        if row is None:
            return
        new_level = row["level"]
        new_total = row["total_xp"]

        # Godhood trial-1 activity tracking (messages + XP gained) — fires
        # on every XP-earning message, independent of whether this message
        # also caused a level-up. See modules/godhood_cards.py's
        # GODHOOD_TRIALS[0] ('activity_combined').
        if isinstance(message.author, discord.Member):
            await self._track_godhood_activity(message.author, gained, clone_id=clone_id)

        if new_level > old_level and isinstance(message.author, discord.Member):
            announce_channel = await self._ensure_announce_channel(message.guild, config, clone_id=clone_id)
            if announce_channel is None:
                announce_channel = message.channel
            card_style = config.get("card_style", "card")
            if card_style != "off":
                await self._send_level_up_card(
                    announce_channel, message.author, new_level, new_total, card_style,
                )
            await self._grant_level_roles(message.author, new_level, clone_id=clone_id)
            # Clan flavor card — every 3 levels gained (3, 6, 9, ...),
            # regardless of card_style ("text"/"off" only silence the normal
            # tier card above, not this). See modules/clan_cards.py and
            # database.py's get_or_assign_clan_card.
            # Clan chiefs — 5 exclusive per-server seats, re-derived from
            # the top 5 of the XP leaderboard. Checked HERE (on level-up)
            # only, never polled — per the confirmed spec. Cheap even so:
            # this only touches the DB when a level-up already happened,
            # and recompute_clan_chiefs itself is a single top-5 query.
            # Run BEFORE the clan-card send below so a member who just
            # became chief this exact level-up still gets their card.
            chief_changes = await db.recompute_clan_chiefs(message.guild.id, clone_id=clone_id)
            for change in chief_changes:
                await self._announce_chief_change(announce_channel, message.guild, change, clone_id=clone_id)

            # Regular members get the clan card every 3 levels. Chiefs get
            # it on EVERY level-up (confirmed) — check the seat table
            # rather than chief_changes so an existing chief who didn't
            # move seats this level-up still gets one.
            is_chief_now = any(c["new_user_id"] == message.author.id for c in chief_changes) or (
                await db.get_chief_seat_for_user(message.guild.id, message.author.id, clone_id=clone_id) is not None
            )
            if is_chief_now or new_level % 3 == 0:
                await self._send_clan_message(announce_channel, message.author, clone_id=clone_id)

            # Godhood gauntlet — see modules/godhood_cards.py. Checked
            # HERE (on level-up) same as the chief recompute above: only
            # fires the "chosen" roll on GODHOOD_TRIGGER_LEVELS, and only
            # advances/checks an already-active gauntlet's progress on
            # 'level_reached' trials, which is every level-up by
            # definition of that trial type.
            await self._maybe_advance_godhood(announce_channel, message.author, new_level, clone_id=clone_id)

    async def _announce_chief_change(self, channel, guild: discord.Guild, change: dict, clone_id=None):
        """Plain mention, no @everyone — announces both the new chief and
        whoever they just displaced (if that seat was previously held).
        See database.py's recompute_clan_chiefs docstring for why a seat's
        clan_slug can be a clan the new holder isn't personally locked to
        (Option B, confirmed by owner)."""
        clan_slug = change["clan_slug"]
        new_id = change["new_user_id"]
        old_id = change["old_user_id"]
        try:
            if new_id is not None:
                await channel.send(f"👑 <@{new_id}> is now **Chief of {clan_slug}**!")
            if old_id is not None and old_id != new_id:
                await channel.send(f"<@{old_id}> has lost the **{clan_slug}** chief seat.")
        except discord.Forbidden:
            pass
        except Exception as e:
            logger.error(f"[v0] Failed to announce chief change ({clan_slug}) in guild {guild.id}: {e}")

    async def _send_level_up_card(self, channel, member: discord.Member, new_level: int, new_total_xp: int,
                                   card_style: str = "card", clone_id=None):
        """Renders and sends the level-up announcement. card_style == "text"
        skips the PIL render + avatar fetch entirely and just posts a plain
        message (people asked for this — some don't want the image spam,
        and it's also just faster / no PIL work per level-up). card_style
        == "card" is the original image-card behavior, unchanged. "off" is
        handled by the caller (on_message) before this is even called.

        The "⚡ Boost XP" button is no longer pitched here — it now shows up
        only on /leaderboard, so level-up cards don't carry it (or the
        discord_xp.boost_pitched bookkeeping/DB write that used to go with
        it — one less write per level-up)."""
        if card_style == "text":
            try:
                await channel.send(f"🎉 {member.mention} leveled up to **level {new_level}**!")
            except discord.Forbidden:
                pass
            return
        try:
            p = leveling.xp_progress(new_total_xp)
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    str(member.display_avatar.replace(size=256).url), timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    avatar_bytes = await resp.read()
            # Off-loaded to a thread — see welcome_card.py's render calls for
            # why: synchronous PIL work run inline would block the bot's
            # single event loop for everyone, not just this member.
            #
            # Level 10+ automatically switches to the free "evolving" card
            # — no config option, no payment gate (see leveling-boost-
            # build-prompt.md §1). This only decides which image renderer
            # runs; card_style ("card"/"text"/"off") semantics above are
            # untouched.
            tier_image = get_tier_image_for_level(new_level)
            if tier_image is not None:
                tier_filename, tier_label = tier_image
                card_bytes = await asyncio.to_thread(
                    render_level_card_tiered,
                    avatar_bytes, member.display_name, new_level,
                    p["current_xp_in_level"], p["xp_needed_for_next_level"],
                    tier_filename, tier_label,
                )
            elif new_level >= 10:
                progress_fraction = min(1.0, (new_level - 10) / 10)
                card_bytes = await asyncio.to_thread(
                    render_level_card_evolved,
                    avatar_bytes, member.display_name, new_level,
                    p["current_xp_in_level"], p["xp_needed_for_next_level"],
                    "#05070d", "#3B9AFF", progress_fraction,
                )
            else:
                card_bytes = await asyncio.to_thread(
                    render_level_card,
                    avatar_bytes, member.display_name, new_level,
                    p["current_xp_in_level"], p["xp_needed_for_next_level"],
                )
            file = discord.File(fp=io.BytesIO(card_bytes), filename="levelup.png")
            await channel.send(content=f"🎉 {member.mention} leveled up!", file=file)
        except discord.Forbidden:
            pass
        except Exception as e:
            logger.error(f"[v0] Failed to render/send level-up card for {member.id}: {e}")
            try:
                await channel.send(f"🎉 {member.mention} leveled up to **level {new_level}**!")
            except discord.Forbidden:
                pass

    async def _send_clan_message(self, channel, member: discord.Member, clone_id=None):
        """Sends the clan flavor card — every 3 levels for regular members,
        every level-up for chiefs (confirmed). Locked-random clan
        (get_or_assign_clan_card) so a member always gets the same card
        art. The "your clan is proud of you" message is plain message
        content mentioning the member — NOT drawn onto the card image (see
        modules/clan_cards.py's docstring for why).

        If the member currently holds a chief seat, the caption names the
        SEAT's clan_slug (what they're chief OF) rather than their own
        locked clan — those two can differ under Option B, and the point
        of this message for a chief is to celebrate the seat, not the
        locked-random card art it happens to be drawn on."""
        try:
            clan_filename = await db.get_or_assign_clan_card(
                member.guild.id, member.id, clone_id=clone_id,
            )
            clan_label = clan_cards.get_clan_label(clan_filename)
            # Placeholder crown badge (see modules/clan_cards.py's
            # _draw_crown_badge) if this member currently holds a chief
            # seat — swaps for the real chief art the moment it arrives.
            chief_seat = await db.get_chief_seat_for_user(member.guild.id, member.id, clone_id=clone_id)
            announced_clan = chief_seat["clan_slug"] if chief_seat else clan_label
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    str(member.display_avatar.replace(size=256).url), timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    avatar_bytes = await resp.read()
            card_bytes = await asyncio.to_thread(
                clan_cards.render_clan_card, avatar_bytes, clan_filename, chief_seat is not None,
            )
            file = discord.File(fp=io.BytesIO(card_bytes), filename="clan.png")
            caption = (
                f"👑 {member.mention} Your clan, **{announced_clan}**, is proud of their Chief. "
                f"Keep climbing. ⚡"
                if chief_seat else
                f"{member.mention} Your clan, **{announced_clan}**, is proud of you. "
                f"Level up more to become a god. ⚡"
            )
            await channel.send(content=caption, file=file)
        except discord.Forbidden:
            pass
        except Exception as e:
            logger.error(f"[v0] Failed to send clan card for {member.id}: {e}")

    async def _maybe_advance_godhood(self, channel, member: discord.Member, new_level: int, clone_id=None):
        """Entry point for the whole godhood gauntlet, called on every
        level-up. Three things can happen here, in order:

        1. No active gauntlet, new_level is a trigger level (10/11/20/21/
           30/31) -> roll for being "chosen" (get_or_assign_godhood_trial).
           On a hit, start trial 1 immediately and announce the choosing.
        2. Active gauntlet, current trial's target_type is 'level_reached'
           -> every level-up IS progress for that trial type, so check it
           against new_level directly (no separate progress hook needed
           for this trial shape — see modules/godhood_cards.py's
           GODHOOD_TRIALS, all of trials 2-5 are 'level_reached').
        3. Target met -> either start_next_godhood_trial (announce the new
           trial) or, if that was trial 5, record_godhood_completion and
           announce the hall-of-fame induction instead.

        Trial 1 ('activity_combined') is NOT advanced here — see
        _track_godhood_activity, called separately from on_message's XP
        gain above regardless of whether this level-up happened, since
        activity accrues on every message, not just level-ups.
        """
        try:
            active = await db.get_active_godhood_trial(member.guild.id, member.id, clone_id=clone_id)
            if active is None:
                chosen = await db.get_or_assign_godhood_trial(
                    member.guild.id, member.id, new_level, clone_id=clone_id,
                )
                if chosen is None:
                    return
                started = await db.start_next_godhood_trial(chosen["id"])
                await self._send_godhood_chosen_message(channel, member, started)
                return

            if active["trial_target_type"] != "level_reached":
                return  # trial 1 (activity_combined) advances via _track_godhood_activity instead

            row = await db.update_godhood_progress(active["id"], new_level)
            if row["trial_progress_amount"] < row["trial_target_amount"]:
                return

            if row["current_trial_no"] >= 5:
                await db.record_godhood_completion(
                    row["id"], member.guild.id, member.id, row["god_filename"], clone_id=clone_id,
                )
                await self._send_godhood_completion_message(channel, member, row)
            else:
                started = await db.start_next_godhood_trial(row["id"])
                await self._send_godhood_trial_advanced_message(channel, member, started)
        except Exception as e:
            logger.error(f"[v0] Godhood advance failed for {member.id}: {e}")

    async def _track_godhood_activity(self, member: discord.Member, xp_gained: int, clone_id=None):
        """Called from on_message on EVERY XP-earning message (not just
        level-ups), so trial 1's 'activity_combined' target (messages sent
        + XP gained since the trial started) accrues continuously rather
        than only on level-up ticks. No-ops instantly if there's no active
        trial-1 gauntlet for this member, so this is cheap on the common
        case of "member isn't in a gauntlet at all"."""
        try:
            active = await db.get_active_godhood_trial(member.guild.id, member.id, clone_id=clone_id)
            if active is None or active["trial_target_type"] != "activity_combined":
                return
            new_progress = active["trial_progress_amount"] + 1 + xp_gained
            row = await db.update_godhood_progress(active["id"], new_progress)
            if row["trial_progress_amount"] < row["trial_target_amount"]:
                return
            started = await db.start_next_godhood_trial(row["id"])
            announce_channel = member.guild.system_channel
            if started and announce_channel is not None:
                await self._send_godhood_trial_advanced_message(announce_channel, member, started)
        except Exception as e:
            logger.error(f"[v0] Godhood activity tracking failed for {member.id}: {e}")

    async def _render_godhood_card(self, member: discord.Member, god_filename: str, is_chief: bool = False) -> discord.File:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                str(member.display_avatar.replace(size=256).url), timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                avatar_bytes = await resp.read()
        card_bytes = await asyncio.to_thread(
            godhood_cards.render_godhood_card, avatar_bytes, god_filename, is_chief,
        )
        return discord.File(fp=io.BytesIO(card_bytes), filename="godhood.png")

    async def _send_godhood_chosen_message(self, channel, member: discord.Member, trial_row: dict):
        """The "you have been chosen" moment — trial 1 just started."""
        try:
            label = godhood_cards.get_godhood_label(trial_row["god_filename"])
            trial_spec = next(
                t for t in godhood_cards.GODHOOD_TRIALS if t["trial_no"] == trial_row["current_trial_no"]
            )
            file = await self._render_godhood_card(member, trial_row["god_filename"])
            await channel.send(
                content=(
                    f"⚡ {member.mention} has been chosen by **{label}** to inherit their cultivation. "
                    f"5 trials stand between you and godhood. "
                    f"**Trial {trial_spec['trial_no']}: {trial_spec['title']}** begins now — "
                    f"you have {godhood_cards.GODHOOD_TRIAL_DEADLINE_DAYS} days."
                ),
                file=file,
            )
        except discord.Forbidden:
            pass
        except Exception as e:
            logger.error(f"[v0] Failed to send godhood-chosen message for {member.id}: {e}")

    async def _send_godhood_trial_advanced_message(self, channel, member: discord.Member, trial_row: dict):
        try:
            next_trial = next(
                t for t in godhood_cards.GODHOOD_TRIALS if t["trial_no"] == trial_row["current_trial_no"]
            )
            label = godhood_cards.get_godhood_label(trial_row["god_filename"])
            file = await self._render_godhood_card(member, trial_row["god_filename"])
            await channel.send(
                content=(
                    f"🔥 {member.mention} has passed a trial of **{label}**! "
                    f"**Trial {next_trial['trial_no']}: {next_trial['title']}** begins now — "
                    f"you have {godhood_cards.GODHOOD_TRIAL_DEADLINE_DAYS} days."
                ),
                file=file,
            )
        except discord.Forbidden:
            pass
        except Exception as e:
            logger.error(f"[v0] Failed to send godhood-advanced message for {member.id}: {e}")

    async def _send_godhood_completion_message(self, channel, member: discord.Member, trial_row: dict):
        """Trial 5 just cleared — clan chief badge + permanent hall-of-fame
        slot (the actual end goal of the gauntlet)."""
        try:
            label = godhood_cards.get_godhood_label(trial_row["god_filename"])
            file = await self._render_godhood_card(member, trial_row["god_filename"], is_chief=True)
            await channel.send(
                content=(
                    f"👑 {member.mention} has completed the trials of **{label}** and ascended to "
                    f"**Clan Chief**! A permanent seat on the Hall of Fame is theirs. ⚡"
                ),
                file=file,
            )
        except discord.Forbidden:
            pass
        except Exception as e:
            logger.error(f"[v0] Failed to send godhood-completion message for {member.id}: {e}")

    @app_commands.command(name="rank", description="Show your (or someone else's) level and XP")
    @app_commands.guild_only()
    @app_commands.describe(member="Member to check (optional)")
    async def rank(self, interaction: discord.Interaction, member: discord.Member = None):
        await interaction.response.defer()
        target = member or interaction.user
        clone_id = _clone_id_of(interaction)
        row = await db.get_xp(interaction.guild_id, target.id, clone_id=clone_id)
        p = leveling.xp_progress(row["total_xp"])
        bar = _progress_bar(p["current_xp_in_level"], p["xp_needed_for_next_level"])
        line = (
            f"`{bar}` {p['current_xp_in_level']}/{p['xp_needed_for_next_level']} XP "
            f"({p['total_xp']} total)"
        )
        lines = [line]
        chief_seat = await db.get_chief_seat_for_user(interaction.guild_id, target.id, clone_id=clone_id)
        if chief_seat:
            lines.append(f"👑 Chief of **{chief_seat['clan_slug']}**")
        buttons = [ActionButton("Leaderboard", discord.ButtonStyle.secondary, self, "leaderboard", emoji="🏆")]
        card = NavCardView(f"{target.display_name} — level {p['level']}", lines, discord.Color.blurple(), buttons)
        await interaction.followup.send(view=card)

    # New command (never previously registered under any shape), so unlike
    # /leaderboard's history below there's no CommandSignatureMismatch risk
    # in making this a Group from day one.
    clan_group = app_commands.guild_only()(app_commands.Group(name="clan", description="Clans & clan chiefs"))

    @clan_group.command(name="view", description="Show your (or someone else's) locked clan and chief status")
    @app_commands.describe(member="Member to check (optional)")
    async def clan_view(self, interaction: discord.Interaction, member: discord.Member = None):
        await interaction.response.defer()
        target = member or interaction.user
        clone_id = _clone_id_of(interaction)
        clan_filename = await db.get_or_assign_clan_card(interaction.guild_id, target.id, clone_id=clone_id)
        clan_label = clan_cards.get_clan_label(clan_filename)
        lines = [f"Clan: **{clan_label}**"]
        chief_seat = await db.get_chief_seat_for_user(interaction.guild_id, target.id, clone_id=clone_id)
        if chief_seat:
            lines.append(f"👑 Chief of **{chief_seat['clan_slug']}**")
        buttons = [ActionButton("Members", discord.ButtonStyle.secondary, self, "clan_members_button",
                                 emoji="👥", args=(clan_filename,))]
        card = NavCardView(f"{target.display_name}'s clan", lines, discord.Color.blurple(), buttons)
        await interaction.followup.send(view=card)

    @clan_group.command(name="members", description="See who's in a clan, ranked by XP")
    @app_commands.describe(clan="Which clan to view")
    @app_commands.choices(clan=[
        app_commands.Choice(name=label, value=filename) for filename, label, *_ in clan_cards.CLAN_CARDS
    ])
    async def clan_members(self, interaction: discord.Interaction, clan: app_commands.Choice[str]):
        await interaction.response.defer()
        clone_id = _clone_id_of(interaction)
        await self._send_clan_members_page(interaction, clan.value, clone_id, page=0)

    async def clan_members_button(self, interaction: discord.Interaction, clan_filename: str):
        """ActionButton target for /clan view's Members button — not a
        slash command itself, see _views_shared.py's ActionButton
        docstring for why plain helper methods (vs @app_commands.command
        methods) are called directly rather than through .callback()."""
        await interaction.response.defer()
        await self._send_clan_members_page(interaction, clan_filename, _clone_id_of(interaction), page=0)

    async def _send_clan_members_page(self, interaction: discord.Interaction, clan_filename: str,
                                       clone_id, page: int, edit: bool = False):
        """Shared render for /clan members and the leaderboard's Clans
        button dropdown — both end up here so the two entry points can't
        drift apart. `edit` picks response.edit_message vs a fresh
        followup, since the button path is an existing ephemeral message
        and the slash-command path is a brand new one."""
        clan_label = clan_cards.get_clan_label(clan_filename)
        per_page = 10
        members = await db.get_clan_members(
            interaction.guild_id, clan_filename, clone_id=clone_id, limit=per_page, offset=page * per_page,
        )
        total = await db.get_clan_member_count(interaction.guild_id, clan_filename, clone_id=clone_id)
        if not members:
            lines = ["Nobody's been assigned to this clan here yet."]
        else:
            lines = []
            for i, m in enumerate(members, start=page * per_page + 1):
                member_obj = interaction.guild.get_member(m["user_id"])
                name = member_obj.display_name if member_obj else f"<@{m['user_id']}>"
                lines.append(f"`#{i}` {name} — Lvl `{m['level']}` ({m['total_xp']} xp)")
        title = f"**{clan_label}** — {total} member{'s' if total != 1 else ''}"
        content = title + "\n\n" + "\n".join(lines)
        if edit:
            await interaction.response.edit_message(content=content, view=None)
        else:
            await interaction.followup.send(content=content)

    # Plain command — this used to briefly become a Group (show + autopost
    # subcommands) so the daily-post config could live under it, but that
    # changed its shape, and since this bot's DISCORD_DEV_GUILD_ID only
    # syncs to a single dev guild (see bot.py), every other server kept
    # Discord's old plain-command registration and crashed with
    # CommandSignatureMismatch. Reverting to a plain command like this
    # matches what's still globally registered everywhere, so no global
    # resync is needed. The daily-post channel is now configured through
    # the existing /leveling setup wizard instead (see
    # _views_leveling_wizard.py's "Step 5" ChannelSelect) — no new command
    # was added for it at all.
    @app_commands.command(name="leaderboard", description="Show this server's XP leaderboard")
    @app_commands.guild_only()
    async def leaderboard(self, interaction: discord.Interaction):
        await interaction.response.defer()
        clone_id = _clone_id_of(interaction)
        view = await build_leaderboard_view(
            interaction.client, interaction.guild, clone_id,
            mode="local", page=0, stats_for_user_id=interaction.user.id,
        )
        if view is None:
            await interaction.followup.send("No XP earned yet.", ephemeral=True)
            return
        await interaction.followup.send(view=view)

    @tasks.loop(minutes=30)
    async def _leaderboard_autopost_loop(self):
        """Wakes up every 30 min and asks the DB for guilds whose daily post
        is actually due (one batched query — see get_due_leaderboard_
        autoposts — never a per-guild query in a loop), so this stays cheap
        even with many guilds configured. A 30-min check interval against a
        24h post interval means posts land within ~30 min of "once a day",
        which is close enough — it doesn't need to be exact to the minute.

        This stays a live in-process loop rather than an external cron
        endpoint (like api/cron_expire_monetization.py) because posting
        needs build_leaderboard_view's Components-v2 view, which needs a
        connected discord.py Client with a populated member/guild cache —
        that only exists inside the running bot process, not a stateless
        serverless function."""
        try:
            await self._run_due_leaderboard_autoposts()
        except Exception as e:
            # Anything escaping here would otherwise hit tasks.loop's
            # default error handling, which just logs once and lets the
            # loop DIE PERMANENTLY — no auto-retry, no restart, and nothing
            # visibly wrong until someone asks "why has this never posted".
            # See the .error handler below for what actually restarts it.
            logger.error(f"[v0] _leaderboard_autopost_loop iteration failed: {e}")
            raise

    async def _run_due_leaderboard_autoposts(self):
        clone_id = getattr(self.bot, "clone_id", None)
        due = await db.get_due_leaderboard_autoposts(clone_id, limit=10)
        for cfg in due:
            guild = self.bot.get_guild(cfg["guild_id"])
            if guild is None:
                continue
            channel = guild.get_channel(cfg["post_channel_id"])
            if channel is None:
                continue
            try:
                view = await build_leaderboard_view(self.bot, guild, cfg["clone_id"], mode="local", page=0)
                if view is not None:
                    await channel.send(view=view)
            except discord.Forbidden:
                pass
            except Exception as e:
                logger.error(f"[v0] Failed to post daily leaderboard for guild {cfg['guild_id']}: {e}")
            finally:
                # Mark posted even on a failure above (missing perms, etc.)
                # so a permanently-broken channel doesn't get retried every
                # 30 minutes forever — same reasoning as autopost's
                # failure-count pattern, just simplified to "try once a day".
                await db.mark_leaderboard_posted(cfg["guild_id"], cfg["clone_id"])

    @_leaderboard_autopost_loop.error
    async def _leaderboard_autopost_loop_error(self, error: Exception):
        """discord.ext.tasks silently stops a loop forever the first time
        an iteration raises — no built-in retry. That's almost certainly
        why this has never visibly autoposted: one transient failure
        (a DB hiccup, a bad channel lookup, anything) and it went quiet
        with nothing louder than a log line buried in startup noise.
        Log it loudly and restart the loop so a one-off failure costs at
        most one missed cycle instead of the feature dying silently for
        the rest of the process's life."""
        logger.error(f"[v0] _leaderboard_autopost_loop crashed, restarting it: {error}")
        if not self._leaderboard_autopost_loop.is_running():
            self._leaderboard_autopost_loop.start()

    @_leaderboard_autopost_loop.before_loop
    async def _before_leaderboard_autopost_loop(self):
        await self.bot.wait_until_ready()

    @tasks.loop(hours=1)
    async def _godhood_deadline_loop(self):
        """Polls for expired godhood trial deadlines once an hour — a
        member who goes quiet mid-trial (stops sending messages/gaining
        levels entirely) still needs their trial_deadline_at enforced, so
        this can't be a check that only runs inline on activity events
        (same reasoning as voice_xp.py's _tick loop, applied to a much
        longer interval since a 3-day deadline doesn't need minute-level
        precision)."""
        try:
            failed_rows = await db.fail_expired_godhood_trials()
            for row in failed_rows:
                await self._announce_godhood_failure(row)
        except Exception as e:
            logger.error(f"[v0] _godhood_deadline_loop iteration failed: {e}")
            raise

    async def _announce_godhood_failure(self, trial_row: dict):
        guild = self.bot.get_guild(trial_row["guild_id"])
        if guild is None:
            return
        channel = guild.system_channel
        if channel is None:
            return
        try:
            label = godhood_cards.get_godhood_label(trial_row["god_filename"])
            await channel.send(
                f"💀 <@{trial_row['user_id']}> failed to complete their trial of **{label}** in time. "
                f"The gauntlet ends here — a new god may choose them again once they reach a higher level tier."
            )
        except discord.Forbidden:
            pass
        except Exception as e:
            logger.error(f"[v0] Failed to announce godhood failure for {trial_row.get('user_id')}: {e}")

    @_godhood_deadline_loop.error
    async def _godhood_deadline_loop_error(self, error: Exception):
        """Same silent-death trap as _leaderboard_autopost_loop_error —
        discord.ext.tasks stops a loop forever after one raised iteration,
        so this restarts it instead of letting deadline enforcement quietly
        stop working for the rest of the process's life."""
        logger.error(f"[v0] _godhood_deadline_loop crashed, restarting it: {error}")
        if not self._godhood_deadline_loop.is_running():
            self._godhood_deadline_loop.start()

    @_godhood_deadline_loop.before_loop
    async def _before_godhood_deadline_loop(self):
        await self.bot.wait_until_ready()

    group = app_commands.guild_only()(app_commands.Group(name="levelrole", description="Configure level-up role rewards"))

    @group.command(name="setup", description="Set up leveling with a guided step-by-step wizard")
    async def leveling_setup(self, interaction: discord.Interaction):
        await interaction.response.defer()
        if not _require_perm(interaction, "manage_roles"):
            await _deny(interaction, "Manage Roles")
            return
        clone_id = _clone_id_of(interaction)
        config = await db.get_leveling_config(interaction.guild_id, clone_id=clone_id)
        role_rows = await db.get_level_roles(interaction.guild_id, clone_id=clone_id)
        view = build_leveling_wizard_view(interaction.guild_id, clone_id, interaction.user.id, config, role_rows)
        await interaction.followup.send(view=view)
        sent = await interaction.original_response()
        await remember_leveling_wizard_message(interaction.guild_id, clone_id, interaction.user.id, sent.channel.id, sent.id)

    @group.command(name="giftboost", description="[Bot owner] Gift a member a temporary XP boost")
    @app_commands.describe(
        member="Member to gift the boost to",
        multiplier="XP multiplier (e.g. 2 = double XP)",
        days="How many days the boost lasts",
    )
    async def giftboost(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        multiplier: app_commands.Range[float, 1.0, 100.0],
        days: app_commands.Range[int, 1, 365],
    ):
        await interaction.response.defer(ephemeral=True)
        if interaction.user.id not in DISCORD_CLONE_ADMIN_IDS:
            await interaction.followup.send("This command is restricted to the bot owner.", ephemeral=True)
            return
        # Reuses the same activate_xp_boost the paid-boost payment flow
        # calls (payments_manual.py's UNLOCK_HANDLERS["xp_boost"]) — a
        # gifted boost behaves identically to a purchased one, including
        # ON CONFLICT re-activation replacing rather than stacking a prior
        # boost for that member.
        row = await db.activate_xp_boost(
            interaction.guild_id, member.id, float(multiplier), days,
            clone_id=_clone_id_of(interaction),
        )
        await interaction.followup.send(
            f"🎁 Gifted **{member.display_name}** a **{row['multiplier']}x** XP boost "
            f"for **{days}** day{'s' if days != 1 else ''} (expires <t:{int(row['expires_at'].timestamp())}:R>).",
            ephemeral=True,
        )

    @group.command(name="add", description="Grant a role automatically at a given level")
    async def add(self, interaction: discord.Interaction, level: app_commands.Range[int, 1, 1000], role: discord.Role):
        await interaction.response.defer()
        if not _require_perm(interaction, "manage_roles"):
            await _deny(interaction, "Manage Roles")
            return
        if role >= interaction.guild.me.top_role:
            await interaction.followup.send(
                "That role is above (or equal to) my own top role — move my role above it first.", ephemeral=True
            )
            return
        ok = await db.add_level_role(interaction.guild_id, level, role.id, clone_id=_clone_id_of(interaction))
        if ok:
            await refresh_leveling_wizard(interaction.client, interaction.guild_id, clone_id=_clone_id_of(interaction))
        await interaction.followup.send(
            f"✅ Level {level} now grants **{role.name}**." if ok else "❌ Couldn't save that.", ephemeral=True
        )

    @group.command(name="remove", description="Remove a level-up role reward")
    async def remove(self, interaction: discord.Interaction, level: app_commands.Range[int, 1, 1000]):
        await interaction.response.defer()
        if not _require_perm(interaction, "manage_roles"):
            await _deny(interaction, "Manage Roles")
            return
        ok = await db.remove_level_role(interaction.guild_id, level, clone_id=_clone_id_of(interaction))
        if ok:
            await refresh_leveling_wizard(interaction.client, interaction.guild_id, clone_id=_clone_id_of(interaction))
        await interaction.followup.send(
            "✅ Removed." if ok else "No reward was configured for that level.", ephemeral=True
        )

    @group.command(name="list", description="List configured level-up role rewards")
    async def list_roles(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        rows = await db.get_level_roles(interaction.guild_id, clone_id=_clone_id_of(interaction))
        if not rows:
            await interaction.followup.send("No level-up roles configured.", ephemeral=True)
            return
        embed = discord.Embed(title="Level-up role rewards", color=discord.Color.blurple())
        for r in rows:
            embed.add_field(name=f"Level {r['level']}", value=f"<@&{r['role_id']}>", inline=True)
        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(LevelingCog(bot))
