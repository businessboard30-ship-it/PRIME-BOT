# FULL PATH: PRIME-BOT-main/discord_bot/cogs/roast_arena.py

# path: discord_bot/cogs/roast_arena.py

"""
Inter-server roast battles — a SEPARATE feature from the single-server
bot-vs-member roast in discord_bot/cogs/roast.py. Here two *servers* fight:
a member in server A challenges server B, one member on each side is the
contestant, and a live audience votes until the clock hits 0:00.

Lifecycle (rows in discord_roast_arena_challenges, see
database/migrations/003_roast_arena.sql):

  1. /roastarena challenge  (any member, guild must be enabled)
     → picks a random OTHER opted-in server as the opponent, creates a
       'pending_approval' row with the caller as challenger contestant, and
       DMs every admin of the challenged server an approve/decline prompt
       (buttons live in _views_roast_arena_challenge.py). Nothing is posted
       in the challenged server until an admin approves — the consent gate.

  2. approve → 'awaiting_accept': the bot posts a public "accept the
     challenge" button in the challenged server. The FIRST member to click
     becomes that server's contestant (claim_roast_arena_accept is atomic, so
     the race resolves to exactly one).

  3. accept → 'active': the battleground is resolved (the single shared
     battleground approved via /roastarena apply, else the owner/support
     broadcast channel, else any sendable channel), a live vote panel is
     posted with two persistent vote buttons, and battle_ends_at is stamped.
     Other opted-in servers get an event-invite DM (buttons in
     _views_roast_arena_consent.py) unless they've hit don't-ask-again /
     remind-me-later.

  4. _poller (every POLL_INTERVAL_SECONDS) refreshes the countdown on every
     active panel and, once battle_ends_at passes, tallies the votes, marks
     the row 'completed', locks the panel, and announces the winner. It also
     expires stale 'pending_approval' / 'awaiting_accept' rows.

Every button in the two _views_roast_arena_* modules is a persistent
DynamicItem registered in bot.py, so the whole flow survives a restart; those
callbacks route back here via get_cog("RoastArenaCog") to the on_* / handle_*
methods below.
"""

import json
import logging
import random
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

from config import DISCORD_CLONE_ADMIN_IDS, OWNER_BROADCAST_CHANNEL_ID
from database import db
from discord_bot.cogs._dm_support import GuildOnlyCog
from discord_bot.cogs._views_roast_arena_challenge import (
    build_accept_view,
    build_approval_view,
)
from discord_bot.cogs._views_roast_arena_panel import build_battle_panel
from discord_bot.cogs._views_roast_arena_consent import (
    REMIND_LATER_HOURS,
    build_consent_embed,
    build_consent_view,
    build_event_invite_embed,
    build_event_invite_view,
)
from discord_bot.cogs._views_roast_arena_host_wizard import (
    build_apply_wizard_view,
    build_review_embed,
    build_review_view,
)

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 30
# How long an admin has to approve an incoming challenge before it lapses.
APPROVAL_EXPIRY_MINUTES = 30
# After approval, how long the challenged server has for someone to accept.
ACCEPT_EXPIRY_MINUTES = 15
# Length of the live vote once both contestants are locked in.
BATTLE_DURATION_MINUTES = 10


def _clone_id_of(bot: commands.Bot):
    return getattr(bot, "clone_id", None)


def _is_admin_member(member: discord.Member) -> bool:
    return member.guild_permissions.administrator or member.id in DISCORD_CLONE_ADMIN_IDS


def _side_name(guild: "discord.Guild | None", fallback: str) -> str:
    return guild.name if guild else fallback


class RoastArenaCog(GuildOnlyCog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Panels we've already resolved this process — a cheap guard so two
        # overlapping poller ticks can't both announce the same winner before
        # the DB status flips to 'completed'.
        self._resolving: set[int] = set()
        self._poller.start()

    def cog_unload(self):
        self._poller.cancel()

    # ─────────────────────────────────────────────────────────────────────
    # Small helpers
    # ─────────────────────────────────────────────────────────────────────
    async def get_config(self, guild_id: int):
        return await db.get_roast_arena_config(guild_id, _clone_id_of(self.bot))

    def _sendable_channel(
        self, guild: "discord.Guild | None", prefer_channel_id: "int | None" = None
    ) -> "discord.TextChannel | None":
        """Picks a channel to post in. If prefer_channel_id is given (the
        guild's own configured battleground_channel_id — see
        discord_roast_arena_config — or a challenge's already-resolved
        location) and the bot can still post there, use it outright, even if
        @everyone can't see it — an admin explicitly chose that channel.

        Otherwise, pick the first channel that's actually visible to regular
        members (@everyone has view_channel + send_messages), not just the
        first one the bot happens to have send permission in — a staff-only
        mod-log channel is often the alphabetically-first channel the bot can
        post in, which is exactly the "posted where members aren't allowed"
        bug. Falls back to any bot-postable channel only if literally nothing
        is member-visible, so a battle never silently fails to post."""
        if guild is None:
            return None
        if prefer_channel_id:
            ch = guild.get_channel(prefer_channel_id)
            if isinstance(ch, discord.TextChannel) and ch.permissions_for(guild.me).send_messages:
                return ch
        everyone = guild.default_role
        bot_postable = []
        for c in guild.text_channels:
            if not c.permissions_for(guild.me).send_messages:
                continue
            bot_postable.append(c)
            # overwrites_for the @everyone role catches explicit denies
            # (private/staff-only channels); anything not explicitly denied
            # is treated as member-visible.
            everyone_overwrite = c.overwrites_for(everyone)
            if everyone_overwrite.view_channel is not False and everyone_overwrite.send_messages is not False:
                return c
        return bot_postable[0] if bot_postable else None

    async def _resolve_battleground(
        self, challenge: dict, challenger_guild, challenged_guild
    ) -> "discord.TextChannel | None":
        """The arena has exactly ONE shared battleground at a time (see
        discord_roast_arena_host / _views_roast_arena_host_wizard.py) — every
        battle, regardless of which clone raised it, lands there. Falls back
        to OWNER_BROADCAST_CHANNEL_ID if no host has been approved yet, then
        to any sendable channel in either battling server as a last resort
        so a battle never silently fails to post."""
        host = await db.get_roast_arena_host()
        host_channel_id = host.get("channel_id")
        if host_channel_id:
            ch = self.bot.get_channel(host_channel_id)
            if isinstance(ch, discord.TextChannel) and ch.permissions_for(ch.guild.me).send_messages:
                return ch
        if OWNER_BROADCAST_CHANNEL_ID:
            ch = self.bot.get_channel(OWNER_BROADCAST_CHANNEL_ID)
            if isinstance(ch, discord.TextChannel) and ch.permissions_for(ch.guild.me).send_messages:
                return ch
        return self._sendable_channel(challenged_guild) or self._sendable_channel(challenger_guild)

    async def get_arena_host(self) -> dict:
        return await db.get_roast_arena_host()

    async def get_pending_host_request(self, guild_id: int):
        return await db.get_pending_roast_arena_host_request(guild_id)

    def _contestant_names(self, challenge: dict) -> "tuple[str, str]":
        challenger_guild = self.bot.get_guild(challenge["challenger_guild_id"])
        challenged_guild = self.bot.get_guild(challenge["challenged_guild_id"])
        return (
            _side_name(challenger_guild, "Challenger"),
            _side_name(challenged_guild, "Challenged"),
        )

    def _build_panel(self, challenge: dict, counts: dict, *, ended: bool = False, battleground_url: "str | None" = None):
        """Builds the Components V2 vs-card + vote/timer panel (see
        _views_roast_arena_panel.py). Resolves both contestants' Member
        objects here (for their avatars) — either can be None (contestant
        slot unfilled pre-accept, member left, or the guild isn't cached),
        which build_battle_panel handles by falling back to the guild icon
        or dropping the avatar entirely."""
        challenger_name, challenged_name = self._contestant_names(challenge)
        challenger_guild = self.bot.get_guild(challenge["challenger_guild_id"])
        challenged_guild = self.bot.get_guild(challenge["challenged_guild_id"])
        challenger_member = (
            challenger_guild.get_member(challenge["challenger_contestant_id"])
            if challenger_guild and challenge.get("challenger_contestant_id")
            else None
        )
        challenged_member = (
            challenged_guild.get_member(challenge["challenged_contestant_id"])
            if challenged_guild and challenge.get("challenged_contestant_id")
            else None
        )
        return build_battle_panel(
            challenge,
            counts,
            challenger_name=challenger_name,
            challenged_name=challenged_name,
            challenger_member=challenger_member,
            challenged_member=challenged_member,
            challenger_guild=challenger_guild,
            challenged_guild=challenged_guild,
            ended=ended,
            battleground_url=battleground_url,
        )

    def _everyone_prefix(self, channel: "discord.TextChannel | None") -> str:
        """Returns '@everyone ' if the bot actually has permission to ping
        everyone in this channel (Mention Everyone permission, respected by
        Discord regardless of what's in AllowedMentions), else ''. Callers
        pass discord.AllowedMentions(everyone=True) alongside this so the
        ping isn't silently swallowed by the bot's default mention settings."""
        if channel is None:
            return ""
        try:
            if channel.permissions_for(channel.guild.me).mention_everyone:
                return "@everyone "
        except AttributeError:
            pass
        return ""

    async def _post_mirror_panel(
        self, challenge: dict, side: str, guild: "discord.Guild | None",
        counts: dict, battleground_url: "str | None",
    ) -> "tuple[int, int] | None":
        """Posts a copy of the live panel into `guild` (the challenger's or
        challenged side's own server) so members there don't need access to
        the shared battleground guild to watch the countdown or vote — the
        vote buttons are keyed by challenge_id, so a vote cast here counts
        the same as one cast on the battleground copy. Returns
        (channel_id, message_id) on success, or None if there's nowhere to
        post or the guild isn't cached in this process (e.g. cross-clone —
        the outbox pattern used for challenge delivery isn't needed here
        since a missing mirror just means fewer places to vote from, not a
        broken flow)."""
        if guild is None:
            return None
        cfg = await db.get_roast_arena_config(guild.id, challenge.get("clone_id"))
        channel = self._sendable_channel(guild, prefer_channel_id=(cfg or {}).get("battleground_channel_id"))
        if channel is None:
            return None
        # Don't double-post if this side's own channel IS the battleground.
        if channel.id == challenge.get("battleground_channel_id"):
            return None
        panel_view = self._build_panel(challenge, counts, battleground_url=battleground_url)
        ping = self._everyone_prefix(channel)
        if ping:
            # Components V2 (LayoutView) messages can't carry `content`
            # alongside their components, so the ping has to be its own
            # message right before the panel rather than a prefix on it.
            try:
                await channel.send(ping, allowed_mentions=discord.AllowedMentions(everyone=True))
            except discord.HTTPException:
                pass
        try:
            message = await channel.send(view=panel_view)
        except discord.HTTPException:
            logger.warning(f"[arena] failed to post {side} mirror panel challenge={challenge['id']} guild={guild.id}")
            return None
        return channel.id, message.id

    async def _edit_panel(self, challenge: dict, *, ended: bool = False):
        """Edits every live copy of the panel — the shared battleground AND
        both mirrored copies in the contesting guilds (see
        _post_mirror_panel) — so the countdown/vote counts stay in sync
        everywhere the panel was posted, not just the battleground."""
        counts = await db.count_roast_arena_votes(challenge["id"])
        locations = [
            (challenge.get("battleground_channel_id"), challenge.get("panel_message_id"), None),
            (
                challenge.get("challenger_panel_channel_id"), challenge.get("challenger_panel_message_id"),
                self._battleground_url(challenge),
            ),
            (
                challenge.get("challenged_panel_channel_id"), challenge.get("challenged_panel_message_id"),
                self._battleground_url(challenge),
            ),
        ]
        any_edited = False
        for channel_id, message_id, battleground_url in locations:
            if not channel_id or not message_id:
                continue
            channel = self.bot.get_channel(channel_id)
            if channel is None:
                continue
            try:
                message = await channel.fetch_message(message_id)
            except discord.HTTPException:
                continue
            panel = self._build_panel(challenge, counts, ended=ended, battleground_url=battleground_url)
            # Components V2 messages can't carry an embed alongside their
            # components — pass embed=None explicitly, and rebuild the whole
            # LayoutView every tick since there's no in-place field edit like
            # the old embed.set_field_at path had.
            try:
                await message.edit(embed=None, view=panel)
                any_edited = True
            except discord.HTTPException:
                logger.warning(f"[arena] failed to edit panel challenge={challenge['id']} channel={channel_id}")
        if not any_edited:
            logger.warning(f"[arena] no panel copies reachable to edit for challenge={challenge['id']}")

    def _battleground_url(self, challenge: dict) -> "str | None":
        channel_id = challenge.get("battleground_channel_id")
        message_id = challenge.get("panel_message_id")
        guild_id = challenge.get("battleground_guild_id")
        if not (channel_id and message_id and guild_id):
            return None
        return f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"

    async def _dm_admins(self, guild: discord.Guild, *, embed: discord.Embed, view: discord.ui.View) -> int:
        admins = [m for m in guild.members if not m.bot and _is_admin_member(m)]
        sent = 0
        for admin in admins:
            try:
                await admin.send(embed=embed, view=view)
                sent += 1
            except discord.Forbidden:
                continue
            except discord.HTTPException:
                logger.warning(f"[arena] admin DM failed guild={guild.id} admin={admin.id}")
        return sent

    # ─────────────────────────────────────────────────────────────────────
    # Slash commands
    # ─────────────────────────────────────────────────────────────────────
    arena = app_commands.Group(
        name="roastarena",
        description="Inter-server roast battles — challenge another server and let the crowd vote.",
        guild_only=True,
    )

    @arena.command(name="enable", description="Admin: opt this server into inter-server roast battles.")
    async def enable(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        if not _is_admin_member(interaction.user):
            await interaction.followup.send("🚫 Admins only.", ephemeral=True)
            return
        await db.upsert_roast_arena_config(
            interaction.guild.id, _clone_id_of(self.bot), enabled=True, consent_prompted=True
        )
        await interaction.followup.send(
            "✅ Roast battles are **enabled**. Any member can now run `/roastarena challenge`, "
            "and you'll approve every incoming challenge before anything posts.",
            ephemeral=True,
        )

    @arena.command(name="disable", description="Admin: opt this server out of inter-server roast battles.")
    async def disable(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        if not _is_admin_member(interaction.user):
            await interaction.followup.send("🚫 Admins only.", ephemeral=True)
            return
        await db.upsert_roast_arena_config(
            interaction.guild.id, _clone_id_of(self.bot), enabled=False
        )
        await interaction.followup.send("✅ Roast battles are now **off** for this server.", ephemeral=True)

    @arena.command(
        name="apply",
        description="Admin: apply for THIS channel to become the shared roast-arena battleground.",
    )
    async def apply(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        if not _is_admin_member(interaction.user):
            await interaction.followup.send("🚫 Admins only.", ephemeral=True)
            return
        if not isinstance(interaction.channel, discord.TextChannel):
            await interaction.followup.send("Run this in a normal text channel.", ephemeral=True)
            return
        host = await self.get_arena_host()
        pending = await self.get_pending_host_request(interaction.guild.id)
        view = build_apply_wizard_view(
            interaction.guild.id, interaction.user.id, interaction.channel.id, host, pending
        )
        await interaction.followup.send(view=view, ephemeral=True)

    # ─────────────────────────────────────────────────────────────────────
    # Apply-to-host wizard callbacks (see _views_roast_arena_host_wizard.py)
    # ─────────────────────────────────────────────────────────────────────
    async def on_host_apply(
        self, interaction: discord.Interaction, guild_id: int, channel_id: int, applicant_id: int
    ) -> None:
        request = await db.create_roast_arena_host_request(guild_id, channel_id, applicant_id)
        guild = self.bot.get_guild(guild_id)
        guild_name = guild.name if guild else f"Guild {guild_id}"
        channel_mention = f"<#{channel_id}>"
        embed = build_review_embed(guild_name, channel_mention, interaction.user)
        view = build_review_view(request["id"])
        sent = 0
        for owner_id in DISCORD_CLONE_ADMIN_IDS:
            owner = self.bot.get_user(owner_id)
            if owner is None:
                continue
            try:
                await owner.send(embed=embed, view=view)
                sent += 1
            except discord.HTTPException:
                logger.warning(f"[arena] host-apply DM failed owner={owner_id}")
        logger.info(f"[arena] host application id={request['id']} guild={guild_id} notified={sent}")

    async def on_host_review(self, interaction: discord.Interaction, request_id: int, *, approve: bool) -> None:
        await interaction.response.defer()
        if interaction.user.id not in DISCORD_CLONE_ADMIN_IDS:
            await interaction.followup.send("🚫 Only the bot owner can review this.", ephemeral=True)
            return
        request = await db.get_roast_arena_host_request(request_id)
        if not request or request["status"] != "pending":
            await interaction.followup.send("This application was already resolved.", ephemeral=True)
            return
        await db.resolve_roast_arena_host_request(
            request_id, status="approved" if approve else "denied", reviewed_by_user_id=interaction.user.id
        )
        if approve:
            await db.set_roast_arena_host(request["guild_id"], request["channel_id"], interaction.user.id)
        guild = self.bot.get_guild(request["guild_id"])
        guild_name = guild.name if guild else f"Guild {request['guild_id']}"
        verdict = "✅ Approved" if approve else "✋ Denied"
        await interaction.edit_original_response(
            content=f"{verdict} — **{guild_name}**'s application for <#{request['channel_id']}>.",
            embed=None, view=None,
        )
        if approve and guild:
            channel = self.bot.get_channel(request["channel_id"])
            if isinstance(channel, discord.TextChannel):
                try:
                    await channel.send("🏆 This channel is now the official roast arena battleground!")
                except discord.HTTPException:
                    pass
        logger.info(f"[arena] host request={request_id} {'approved' if approve else 'denied'} by={interaction.user.id}")

    @arena.command(name="status", description="Show this server's roast-battle status.")
    async def status(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        cfg = await self.get_config(interaction.guild.id)
        active = await db.get_active_roast_arena_challenge_for_guild(
            interaction.guild.id, _clone_id_of(self.bot)
        )
        host = await self.get_arena_host()
        embed = discord.Embed(title="⚔️ Roast arena status", color=discord.Color.blurple())
        embed.add_field(name="Enabled", value="yes" if cfg.get("enabled") else "no", inline=True)
        bg = host.get("channel_id")
        embed.add_field(
            name="Shared battleground",
            value=(f"<#{bg}>" if bg else "support server (default — no host approved yet)"),
            inline=True,
        )
        embed.add_field(
            name="In progress",
            value=(f"yes — status `{active['status']}`" if active else "none"),
            inline=False,
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @arena.command(
        name="challenge",
        description="Challenge a random opted-in server to a roast battle. You'll be your server's roaster.",
    )
    async def challenge(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        clone_id = _clone_id_of(self.bot)
        guild = interaction.guild

        cfg = await self.get_config(guild.id)
        if not cfg.get("enabled"):
            # Not enabled yet: offer the consent DM to an admin, otherwise
            # point the member at an admin.
            if _is_admin_member(interaction.user):
                try:
                    await interaction.user.send(
                        embed=build_consent_embed(guild), view=build_consent_view(guild.id)
                    )
                    await db.upsert_roast_arena_config(guild.id, clone_id, consent_prompted=True)
                    await interaction.followup.send(
                        "📨 Roast battles aren't enabled here yet — I've DMed you a one-tap enable prompt.",
                        ephemeral=True,
                    )
                except discord.Forbidden:
                    await interaction.followup.send(
                        "Roast battles aren't enabled here. Enable them with `/roastarena enable` "
                        "(I couldn't DM you — open your DMs to use the button flow).",
                        ephemeral=True,
                    )
                return
            await interaction.followup.send(
                "Roast battles aren't enabled on this server yet. Ask an admin to run `/roastarena enable`.",
                ephemeral=True,
            )
            return

        existing = await db.get_active_roast_arena_challenge_for_guild(guild.id, clone_id)
        if existing:
            await interaction.followup.send(
                "Your server already has a roast battle in progress — let that one finish first.",
                ephemeral=True,
            )
            return

        # Pooled across every clone + the main bot — the arena is one shared
        # pool now, not siloed per-bot. Note this does NOT mean this process
        # can reach every candidate directly (see the reachable/relay branch
        # below): any_clone only widens which rows the query returns.
        candidates = await db.list_optedin_roast_arena_guilds(
            clone_id, exclude_guild_id=guild.id, any_clone=True
        )
        if not candidates:
            await interaction.followup.send(
                "No other server is opted into roast battles yet. Invite a rival server and have them run "
                "`/roastarena enable` — then challenge again!",
                ephemeral=True,
            )
            return

        target = random.choice(candidates)
        target_guild_id = target["guild_id"]
        challenged_guild = self.bot.get_guild(target_guild_id)
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=APPROVAL_EXPIRY_MINUTES)
        challenge_id = await db.create_roast_arena_challenge(
            clone_id=clone_id,
            challenger_guild_id=guild.id,
            challenger_user_id=interaction.user.id,
            challenged_guild_id=target_guild_id,
            challenger_contestant_id=interaction.user.id,
            expires_at=expires_at,
        )
        challenged_name = challenged_guild.name if challenged_guild else "the other server"

        if challenged_guild is not None:
            # Same-process match (same bot/clone on both sides) — DM the
            # admins directly, exactly as before.
            embed = discord.Embed(
                title="⚔️ Your server has been challenged to a roast battle!",
                description=(
                    f"**{guild.name}** wants to roast **{challenged_guild.name}**.\n\n"
                    f"Their roaster: **{interaction.user.display_name}**.\n\n"
                    "Approve to let your members pick a roaster and fight back — decline and nothing happens."
                ),
                color=discord.Color.red(),
            )
            embed.set_footer(text=f"Challenge #{challenge_id} · expires in {APPROVAL_EXPIRY_MINUTES} min if no admin responds")
            sent = await self._dm_admins(
                challenged_guild, embed=embed, view=build_approval_view(challenge_id)
            )
            if sent == 0:
                await db.update_roast_arena_challenge(challenge_id, status="expired", resolved_at=datetime.now(timezone.utc))
                await interaction.followup.send(
                    f"Couldn't reach any admin of **{challenged_name}** (their DMs are closed). Try again later.",
                    ephemeral=True,
                )
                return
            logger.info(
                f"[arena] challenge={challenge_id} {guild.id} -> {target_guild_id} "
                f"by user={interaction.user.id}, DMed {sent} admins (same-process)"
            )
        else:
            # Cross-clone match: this process has no gateway connection to
            # target_guild_id, so it can't fetch its members to DM. Hand the
            # job to whichever process DOES hold that guild via the outbox —
            # every process's _poller drains it on its next tick.
            await db.enqueue_roast_arena_action(
                target_guild_id,
                "dm_challenge_approval",
                {
                    "challenge_id": challenge_id,
                    "challenger_guild_name": guild.name,
                    "challenger_display_name": interaction.user.display_name,
                    "approval_expiry_minutes": APPROVAL_EXPIRY_MINUTES,
                },
            )
            logger.info(
                f"[arena] challenge={challenge_id} {guild.id} -> {target_guild_id} "
                f"by user={interaction.user.id}, relayed via outbox (cross-clone)"
            )

        await interaction.followup.send(
            f"🔥 Challenge sent to **{challenged_name}**! You're your server's roaster. "
            "You'll be notified here once an admin over there approves.",
            ephemeral=True,
        )

    # ─────────────────────────────────────────────────────────────────────
    # Button entrypoints (called from the persistent DynamicItem views)
    # ─────────────────────────────────────────────────────────────────────
    async def on_admin_approve(self, interaction: discord.Interaction, challenge_id: int):
        await interaction.response.defer()
        new_expires = datetime.now(timezone.utc) + timedelta(minutes=ACCEPT_EXPIRY_MINUTES)
        challenge = await db.claim_roast_arena_approval(challenge_id, new_expires)
        if challenge is None:
            await interaction.edit_original_response(
                content="This challenge was already handled or has expired.", embed=None, view=None
            )
            return

        challenged_guild = self.bot.get_guild(challenge["challenged_guild_id"])
        challenger_guild = self.bot.get_guild(challenge["challenger_guild_id"])
        # Just needs any postable channel in the challenged server itself for
        # the accept-button prompt — this is separate from the shared arena
        # battleground (_resolve_battleground), which is where the actual
        # battle panel ends up once someone accepts. Honor the server's own
        # configured battleground channel if it set one via /roastarena apply.
        challenged_cfg = await db.get_roast_arena_config(challenge["challenged_guild_id"], challenge.get("clone_id"))
        post_channel = self._sendable_channel(
            challenged_guild, prefer_channel_id=(challenged_cfg or {}).get("battleground_channel_id")
        )
        if post_channel is None:
            await db.update_roast_arena_challenge(
                challenge_id, status="expired", resolved_at=datetime.now(timezone.utc)
            )
            await interaction.edit_original_response(
                content="Approved, but I couldn't find a channel here I can post in. "
                "Make sure I have permission to send messages in at least one text channel.",
                embed=None, view=None,
            )
            return

        challenger_name = _side_name(challenger_guild, "the challenger")
        embed = discord.Embed(
            title="⚔️ Roast battle incoming!",
            description=(
                f"**{challenger_name}** has challenged us to a roast battle. "
                "The first person to hit **accept** below becomes our roaster.\n\n"
                "Bring your best material. 🔥"
            ),
            color=discord.Color.red(),
        )
        try:
            await post_channel.send(
                self._everyone_prefix(post_channel) or None,
                embed=embed, view=build_accept_view(challenge_id),
                allowed_mentions=discord.AllowedMentions(everyone=True),
            )
        except discord.HTTPException:
            await interaction.edit_original_response(
                content="Approved, but posting the accept button failed — check my permissions in that channel.",
                embed=None, view=None,
            )
            return

        await interaction.edit_original_response(
            content=f"✅ Approved! Members can now accept in {post_channel.mention}.",
            embed=None, view=None,
        )
        logger.info(f"[arena] challenge={challenge_id} approved, accept posted in {post_channel.id}")

    async def on_admin_decline(self, interaction: discord.Interaction, challenge_id: int):
        await interaction.response.defer()
        challenge = await db.get_roast_arena_challenge(challenge_id)
        if not challenge or challenge["status"] not in ("pending_approval",):
            await interaction.edit_original_response(
                content="This challenge was already handled or has expired.", embed=None, view=None
            )
            return
        await db.update_roast_arena_challenge(
            challenge_id, status="declined", resolved_at=datetime.now(timezone.utc)
        )
        await interaction.edit_original_response(
            content="✋ Declined — nothing was posted in your server.", embed=None, view=None
        )
        # Let the challenger's server know quietly. If the challenger's guild
        # is on a different clone/process than the one handling this decline
        # (cross-clone match), relay it through the outbox instead.
        challenger_guild = self.bot.get_guild(challenge["challenger_guild_id"])
        if challenger_guild:
            challenger = challenger_guild.get_member(challenge["challenger_user_id"])
            if challenger:
                try:
                    await challenger.send(
                        "Your roast challenge was politely declined by the other server. Try challenging again later!"
                    )
                except discord.HTTPException:
                    pass
        else:
            await db.enqueue_roast_arena_action(
                challenge["challenger_guild_id"],
                "notify_decline",
                {"challenger_user_id": challenge["challenger_user_id"]},
            )
        logger.info(f"[arena] challenge={challenge_id} declined")

    async def on_member_accept(self, interaction: discord.Interaction, challenge_id: int):
        await interaction.response.defer()
        battle_ends_at = datetime.now(timezone.utc) + timedelta(minutes=BATTLE_DURATION_MINUTES)
        challenge = await db.claim_roast_arena_accept(challenge_id, interaction.user.id, battle_ends_at)
        if challenge is None:
            # Someone already accepted, or it lapsed.
            existing = await db.get_roast_arena_challenge(challenge_id)
            msg = (
                "Someone on your server already accepted this one!"
                if existing and existing["status"] == "active"
                else "This challenge is no longer open."
            )
            await interaction.followup.send(msg, ephemeral=True)
            return

        challenger_guild = self.bot.get_guild(challenge["challenger_guild_id"])
        challenged_guild = self.bot.get_guild(challenge["challenged_guild_id"])
        battleground = await self._resolve_battleground(challenge, challenger_guild, challenged_guild)
        if battleground is None:
            await db.update_roast_arena_challenge(
                challenge_id, status="expired", resolved_at=datetime.now(timezone.utc)
            )
            await interaction.followup.send(
                "You accepted, but I couldn't find a battleground channel. The bot owner should approve a "
                "battleground with `/roastarena apply`.",
                ephemeral=True,
            )
            return

        challenger_name, challenged_name = self._contestant_names(challenge)
        counts = {"challenger": 0, "challenged": 0}
        # Stamp battleground so the panel's countdown renders before we persist.
        challenge["battle_ends_at"] = battle_ends_at
        panel_view = self._build_panel(challenge, counts)
        ping = self._everyone_prefix(battleground)
        if ping:
            try:
                await battleground.send(ping, allowed_mentions=discord.AllowedMentions(everyone=True))
            except discord.HTTPException:
                pass
        try:
            panel = await battleground.send(view=panel_view)
        except discord.HTTPException:
            await db.update_roast_arena_challenge(
                challenge_id, status="expired", resolved_at=datetime.now(timezone.utc)
            )
            await interaction.followup.send("Couldn't post the battle panel — check my channel permissions.", ephemeral=True)
            return

        challenge["battleground_guild_id"] = battleground.guild.id
        challenge["battleground_channel_id"] = battleground.id
        challenge["panel_message_id"] = panel.id
        battleground_url = self._battleground_url(challenge)

        # Mirror the same live panel into BOTH contesting guilds — the
        # shared battleground alone is only visible to whichever guild hosts
        # it, so without this, members of the other server have no way to
        # watch the countdown or vote at all. Voting from a mirror counts
        # identically to voting on the battleground copy (same challenge_id).
        challenger_mirror = await self._post_mirror_panel(
            challenge, "challenger", challenger_guild, counts, battleground_url
        )
        challenged_mirror = await self._post_mirror_panel(
            challenge, "challenged", challenged_guild, counts, battleground_url
        )

        update_fields = dict(
            battleground_guild_id=battleground.guild.id,
            battleground_channel_id=battleground.id,
            panel_message_id=panel.id,
        )
        if challenger_mirror:
            update_fields["challenger_panel_channel_id"], update_fields["challenger_panel_message_id"] = challenger_mirror
        if challenged_mirror:
            update_fields["challenged_panel_channel_id"], update_fields["challenged_panel_message_id"] = challenged_mirror
        await db.update_roast_arena_challenge(challenge_id, **update_fields)

        mirror_note = ""
        if not challenger_mirror and not challenged_mirror:
            mirror_note = " (couldn't find a member-visible channel to also post it in either server — check my channel permissions)"
        await interaction.followup.send(
            f"🔥 You're **{challenged_name}**'s roaster! The battle is live in {battleground.mention}"
            f"{mirror_note}.",
            ephemeral=True,
        )
        logger.info(
            f"[arena] challenge={challenge_id} accepted by user={interaction.user.id}, "
            f"panel={panel.id} in channel={battleground.id}"
        )
        # Refresh from DB so the invite broadcast has the stored battleground.
        fresh = await db.get_roast_arena_challenge(challenge_id)
        if fresh:
            await self._broadcast_event_invites(fresh)

    async def handle_vote(self, interaction: discord.Interaction, challenge_id: int, side: str):
        challenge = await db.get_roast_arena_challenge(challenge_id)
        if not challenge or challenge["status"] != "active":
            await interaction.response.send_message("This battle has ended — voting is closed.", ephemeral=True)
            return
        ends_at = challenge.get("battle_ends_at")
        if ends_at and datetime.now(timezone.utc) >= ends_at:
            await interaction.response.send_message("Voting just closed — the clock hit 0:00.", ephemeral=True)
            return
        await db.record_roast_arena_vote(challenge_id, interaction.user.id, side)
        challenger_name, challenged_name = self._contestant_names(challenge)
        picked = challenger_name if side == "challenger" else challenged_name
        await interaction.response.send_message(
            f"🗳️ Vote counted for **{picked}**. You can change it until the clock hits 0:00.",
            ephemeral=True,
        )
        # Nudge the panel so counts feel live (poller also refreshes on a tick).
        await self._edit_panel(challenge)

    # ─────────────────────────────────────────────────────────────────────
    # Event-invite broadcast to other opted-in servers
    # ─────────────────────────────────────────────────────────────────────
    async def _broadcast_event_invites(self, challenge: dict):
        clone_id = _clone_id_of(self.bot)
        challenger_guild = self.bot.get_guild(challenge["challenger_guild_id"])
        challenger_name = _side_name(challenger_guild, "A server")
        now = datetime.now(timezone.utc)
        # Pooled across every clone + the main bot, same as the challenge
        # picker — any opted-in server anywhere in the shared arena should
        # hear about a live battle, not just ones on this bot instance.
        others = await db.list_optedin_roast_arena_guilds(
            clone_id, exclude_guild_id=None, any_clone=True
        )
        battling = {challenge["challenger_guild_id"], challenge["challenged_guild_id"]}

        # A guild opted in on more than one clone (e.g. the main bot AND a
        # clone both added to the same Discord server) comes back from
        # list_optedin_roast_arena_guilds as ONE ROW PER CLONE, all sharing
        # the same guild_id. Without deduping here, the loop below hits
        # self.bot.get_guild(gid) — the SAME guild object — once per row,
        # sending one duplicate DM per clone, all through this one process's
        # token, all with the identical arenainvite:*:{gid} custom_id. That's
        # the visible "don't ask again seems to fail" symptom: clicking it
        # on ONE duplicate only flips dont_ask_again on the ONE clone_id row
        # tied to whoever delivered that click, so the sibling clone's row(s)
        # stay live and the same guild gets invited (and DMed again) next
        # time — it looks like the button didn't take, or like the flow
        # hangs/times out under the pile-up of duplicate sends.
        #
        # Fix: collapse to one row per guild_id, and only skip the guild
        # entirely once EVERY one of its rows is suppressed (all dont_ask_again,
        # or all still within their remind_after window) — one clone opting
        # back in shouldn't get silently overridden by a sibling clone's
        # earlier opt-out.
        by_guild: dict[int, list[dict]] = {}
        for cfg in others:
            by_guild.setdefault(cfg["guild_id"], []).append(cfg)

        for gid, rows in by_guild.items():
            if gid in battling:
                continue

            def _suppressed(row: dict) -> bool:
                if row.get("dont_ask_again"):
                    return True
                remind_after = row.get("remind_after")
                return bool(remind_after and remind_after > now)

            if all(_suppressed(row) for row in rows):
                continue

            guild = self.bot.get_guild(gid)
            if guild is not None:
                await self._dm_admins(
                    guild,
                    embed=build_event_invite_embed(challenger_name),
                    view=build_event_invite_view(challenge["id"], gid),
                )
            else:
                # This process doesn't hold gid's gateway connection — relay
                # the invite through the outbox for whichever process does.
                await db.enqueue_roast_arena_action(
                    gid,
                    "event_invite",
                    {"challenge_id": challenge["id"], "challenger_name": challenger_name},
                )

    # ─────────────────────────────────────────────────────────────────────
    # Poller — countdown refresh, resolution, and expiry
    # ─────────────────────────────────────────────────────────────────────
    @tasks.loop(seconds=POLL_INTERVAL_SECONDS)
    async def _poller(self):
        try:
            now = datetime.now(timezone.utc)

            # 1. Expire stale pre-battle rows.
            for challenge in await db.list_roast_arena_challenges_by_status(
                ("pending_approval", "awaiting_accept")
            ):
                expires_at = challenge.get("expires_at")
                if expires_at and expires_at <= now:
                    await db.update_roast_arena_challenge(
                        challenge["id"], status="expired", resolved_at=now
                    )
                    logger.info(f"[arena] challenge={challenge['id']} expired ({challenge['status']})")

            # 2. Refresh / resolve active battles.
            for challenge in await db.list_roast_arena_challenges_by_status(("active",)):
                ends_at = challenge.get("battle_ends_at")
                if ends_at and ends_at <= now:
                    await self._resolve_battle(challenge)
                else:
                    await self._edit_panel(challenge)

            # 3. Drain any cross-clone outbox actions targeting a guild this
            # process actually has cached, and clean up orphaned rows.
            await self._drain_arena_actions()
            await db.expire_stale_roast_arena_actions()
        except Exception:
            logger.exception("[arena] poller tick failed")

    # ─────────────────────────────────────────────────────────────────────
    # Cross-clone outbox relay
    # ─────────────────────────────────────────────────────────────────────
    async def _drain_arena_actions(self):
        """Claims and executes any pending discord_roast_arena_outbox row
        targeting a guild THIS process currently has in self.bot.guilds.
        Every clone process (and the main bot) runs this same poller tick, so
        whichever process actually holds the target guild's gateway
        connection is the one that ends up executing a given row — see
        database/migrations/005_roast_arena_outbox.sql."""
        reachable_ids = [g.id for g in self.bot.guilds]
        if not reachable_ids:
            return
        actions = await db.claim_roast_arena_actions(reachable_ids)
        for action in actions:
            try:
                await self._execute_arena_action(action)
            except Exception:
                logger.exception(f"[arena] outbox action={action['id']} type={action['action_type']} failed")
                await db.complete_roast_arena_action(action["id"], success=False, result={"error": "exception"})

    async def _execute_arena_action(self, action: dict):
        action_type = action["action_type"]
        payload = action.get("payload") or {}
        if isinstance(payload, str):
            payload = json.loads(payload)
        guild = self.bot.get_guild(action["target_guild_id"])
        if guild is None:
            # Lost the guild between claim and execute (e.g. kicked) — let it
            # fail rather than silently dropping it.
            await db.complete_roast_arena_action(action["id"], success=False, result={"error": "guild_unreachable"})
            return

        if action_type == "dm_challenge_approval":
            challenge_id = payload["challenge_id"]
            challenge = await db.get_roast_arena_challenge(challenge_id)
            if not challenge or challenge["status"] != "pending_approval":
                await db.complete_roast_arena_action(action["id"], success=False, result={"error": "challenge_gone"})
                return
            embed = discord.Embed(
                title="⚔️ Your server has been challenged to a roast battle!",
                description=(
                    f"**{payload.get('challenger_guild_name', 'A server')}** wants to roast **{guild.name}**.\n\n"
                    f"Their roaster: **{payload.get('challenger_display_name', 'someone')}**.\n\n"
                    "Approve to let your members pick a roaster and fight back — decline and nothing happens."
                ),
                color=discord.Color.red(),
            )
            expiry = payload.get("approval_expiry_minutes", APPROVAL_EXPIRY_MINUTES)
            embed.set_footer(text=f"Challenge #{challenge_id} · expires in {expiry} min if no admin responds")
            sent = await self._dm_admins(guild, embed=embed, view=build_approval_view(challenge_id))
            if sent == 0:
                await db.update_roast_arena_challenge(
                    challenge_id, status="expired", resolved_at=datetime.now(timezone.utc)
                )
            logger.info(f"[arena] challenge={challenge_id} relayed dm_challenge_approval DMed={sent}")
            await db.complete_roast_arena_action(action["id"], success=True, result={"admins_dmed": sent})

        elif action_type == "notify_decline":
            member = guild.get_member(payload.get("challenger_user_id"))
            sent = False
            if member:
                try:
                    await member.send(
                        "Your roast challenge was politely declined by the other server. Try challenging again later!"
                    )
                    sent = True
                except discord.HTTPException:
                    pass
            await db.complete_roast_arena_action(action["id"], success=sent, result={"notified": sent})

        elif action_type == "event_invite":
            challenge_id = payload.get("challenge_id")
            challenger_name = payload.get("challenger_name", "A server")
            sent = await self._dm_admins(
                guild,
                embed=build_event_invite_embed(challenger_name),
                view=build_event_invite_view(challenge_id, guild.id),
            )
            await db.complete_roast_arena_action(action["id"], success=sent > 0, result={"admins_dmed": sent})

        else:
            logger.warning(f"[arena] unknown outbox action_type={action_type} id={action['id']}")
            await db.complete_roast_arena_action(action["id"], success=False, result={"error": "unknown_action_type"})

    async def _resolve_battle(self, challenge: dict):
        challenge_id = challenge["id"]
        if challenge_id in self._resolving:
            return
        self._resolving.add(challenge_id)
        try:
            counts = await db.count_roast_arena_votes(challenge_id)
            if counts["challenger"] > counts["challenged"]:
                winner = "challenger"
            elif counts["challenged"] > counts["challenger"]:
                winner = "challenged"
            else:
                winner = "draw"
            await db.update_roast_arena_challenge(
                challenge_id,
                status="completed",
                winner_side=winner,
                resolved_at=datetime.now(timezone.utc),
            )
            resolved = await db.get_roast_arena_challenge(challenge_id)
            if resolved:
                await self._edit_panel(resolved, ended=True)
                await self._announce_winner(resolved, counts)
            logger.info(f"[arena] challenge={challenge_id} completed winner={winner} counts={counts}")
        finally:
            self._resolving.discard(challenge_id)

    async def _announce_winner(self, challenge: dict, counts: dict):
        challenger_name, challenged_name = self._contestant_names(challenge)
        side = challenge.get("winner_side")
        if side == "challenger":
            line = f"🏆 **{challenger_name}** wins the roast battle! ({counts['challenger']}–{counts['challenged']})"
        elif side == "challenged":
            line = f"🏆 **{challenged_name}** wins the roast battle! ({counts['challenged']}–{counts['challenger']})"
        else:
            line = f"🤝 It's a **draw** — {counts['challenger']}–{counts['challenged']}. Rematch?"
        # Announce in every channel the panel was posted to — battleground
        # plus both mirrors — not just the battleground.
        seen_channel_ids = set()
        for channel_id in (
            challenge.get("battleground_channel_id"),
            challenge.get("challenger_panel_channel_id"),
            challenge.get("challenged_panel_channel_id"),
        ):
            if not channel_id or channel_id in seen_channel_ids:
                continue
            seen_channel_ids.add(channel_id)
            channel = self.bot.get_channel(channel_id)
            if channel is None:
                continue
            try:
                await channel.send(
                    self._everyone_prefix(channel) + line,
                    allowed_mentions=discord.AllowedMentions(everyone=True),
                )
            except discord.HTTPException:
                pass

    @_poller.before_loop
    async def _before_poller(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(RoastArenaCog(bot))
