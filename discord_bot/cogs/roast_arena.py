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
        if guild is None:
            return None
        if prefer_channel_id:
            ch = guild.get_channel(prefer_channel_id)
            if isinstance(ch, discord.TextChannel) and ch.permissions_for(guild.me).send_messages:
                return ch
        for c in guild.text_channels:
            if c.permissions_for(guild.me).send_messages:
                return c
        return None

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

    def _build_panel(self, challenge: dict, counts: dict, *, ended: bool = False):
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
        )

    async def _edit_panel(self, challenge: dict, *, ended: bool = False):
        channel_id = challenge.get("battleground_channel_id")
        message_id = challenge.get("panel_message_id")
        if not channel_id or not message_id:
            return
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            return
        try:
            message = await channel.fetch_message(message_id)
        except discord.HTTPException:
            return
        counts = await db.count_roast_arena_votes(challenge["id"])
        panel = self._build_panel(challenge, counts, ended=ended)
        # Components V2 messages can't carry an embed alongside their
        # components — pass embed=None explicitly, and rebuild the whole
        # LayoutView every tick since there's no in-place field edit like
        # the old embed.set_field_at path had.
        try:
            await message.edit(embed=None, view=panel)
        except discord.HTTPException:
            logger.warning(f"[arena] failed to edit panel challenge={challenge['id']}")

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
        if interaction.user.id not in DISCORD_CLONE_ADMIN_IDS:
            await interaction.response.send_message("🚫 Only the bot owner can review this.", ephemeral=True)
            return
        request = await db.get_roast_arena_host_request(request_id)
        if not request or request["status"] != "pending":
            await interaction.response.send_message("This application was already resolved.", ephemeral=True)
            return
        await db.resolve_roast_arena_host_request(
            request_id, status="approved" if approve else "denied", reviewed_by_user_id=interaction.user.id
        )
        if approve:
            await db.set_roast_arena_host(request["guild_id"], request["channel_id"], interaction.user.id)
        guild = self.bot.get_guild(request["guild_id"])
        guild_name = guild.name if guild else f"Guild {request['guild_id']}"
        verdict = "✅ Approved" if approve else "✋ Denied"
        await interaction.response.edit_message(
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

        candidates = await db.list_optedin_roast_arena_guilds(clone_id, exclude_guild_id=guild.id)
        # Only servers the bot can actually reach right now.
        reachable = [c for c in candidates if self.bot.get_guild(c["guild_id"]) is not None]
        if not reachable:
            await interaction.followup.send(
                "No other server is opted into roast battles yet. Invite a rival server and have them run "
                "`/roastarena enable` — then challenge again!",
                ephemeral=True,
            )
            return

        target = random.choice(reachable)
        challenged_guild = self.bot.get_guild(target["guild_id"])
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=APPROVAL_EXPIRY_MINUTES)
        challenge_id = await db.create_roast_arena_challenge(
            clone_id=clone_id,
            challenger_guild_id=guild.id,
            challenger_user_id=interaction.user.id,
            challenged_guild_id=challenged_guild.id,
            challenger_contestant_id=interaction.user.id,
            expires_at=expires_at,
        )

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
                f"Couldn't reach any admin of **{challenged_guild.name}** (their DMs are closed). Try again later.",
                ephemeral=True,
            )
            return

        logger.info(
            f"[arena] challenge={challenge_id} {guild.id} -> {challenged_guild.id} "
            f"by user={interaction.user.id}, DMed {sent} admins"
        )
        await interaction.followup.send(
            f"🔥 Challenge sent to **{challenged_guild.name}**! You're your server's roaster. "
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
        # battle panel ends up once someone accepts.
        post_channel = self._sendable_channel(challenged_guild)
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
            await post_channel.send(embed=embed, view=build_accept_view(challenge_id))
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
        # Let the challenger's server know quietly.
        challenger_guild = self.bot.get_guild(challenge["challenger_guild_id"])
        challenger = challenger_guild.get_member(challenge["challenger_user_id"]) if challenger_guild else None
        if challenger:
            try:
                await challenger.send(
                    "Your roast challenge was politely declined by the other server. Try challenging again later!"
                )
            except discord.HTTPException:
                pass
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
        try:
            panel = await battleground.send(view=panel_view)
        except discord.HTTPException:
            await db.update_roast_arena_challenge(
                challenge_id, status="expired", resolved_at=datetime.now(timezone.utc)
            )
            await interaction.followup.send("Couldn't post the battle panel — check my channel permissions.", ephemeral=True)
            return

        await db.update_roast_arena_challenge(
            challenge_id,
            battleground_guild_id=battleground.guild.id,
            battleground_channel_id=battleground.id,
            panel_message_id=panel.id,
        )
        await interaction.followup.send(
            f"🔥 You're **{challenged_name}**'s roaster! The battle is live in {battleground.mention}.",
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
        others = await db.list_optedin_roast_arena_guilds(clone_id, exclude_guild_id=None)
        battling = {challenge["challenger_guild_id"], challenge["challenged_guild_id"]}
        for cfg in others:
            gid = cfg["guild_id"]
            if gid in battling or cfg.get("dont_ask_again"):
                continue
            remind_after = cfg.get("remind_after")
            if remind_after and remind_after > now:
                continue
            guild = self.bot.get_guild(gid)
            if guild is None:
                continue
            await self._dm_admins(
                guild,
                embed=build_event_invite_embed(challenger_name),
                view=build_event_invite_view(challenge["id"], gid),
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
        except Exception:
            logger.exception("[arena] poller tick failed")

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
        channel_id = challenge.get("battleground_channel_id")
        if not channel_id:
            return
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            return
        challenger_name, challenged_name = self._contestant_names(challenge)
        side = challenge.get("winner_side")
        if side == "challenger":
            line = f"🏆 **{challenger_name}** wins the roast battle! ({counts['challenger']}–{counts['challenged']})"
        elif side == "challenged":
            line = f"🏆 **{challenged_name}** wins the roast battle! ({counts['challenged']}–{counts['challenger']})"
        else:
            line = f"🤝 It's a **draw** — {counts['challenger']}–{counts['challenged']}. Rematch?"
        try:
            await channel.send(line)
        except discord.HTTPException:
            pass

    @_poller.before_loop
    async def _before_poller(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(RoastArenaCog(bot))
