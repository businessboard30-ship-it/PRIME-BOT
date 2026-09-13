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
from discord import app_commands
from discord.ext import commands
from discord_bot.cogs._dm_support import GuildOnlyCog

from database import db
from modules import leveling
from modules.level_card import render_level_card, render_level_card_evolved, render_leaderboard_card
from discord_bot.cogs._views_shared import ActionButton, NavCardView
from discord_bot.cogs._views_leveling_boost import BoostXPButton, build_boost_xp_view
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

    async def _ensure_announce_channel(self, guild: discord.Guild, config: dict, clone_id=None):
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

        if new_level > old_level and isinstance(message.author, discord.Member):
            announce_channel = await self._ensure_announce_channel(message.guild, config, clone_id=clone_id)
            if announce_channel is None:
                announce_channel = message.channel
            card_style = config.get("card_style", "card")
            if card_style != "off":
                await self._send_level_up_card(
                    announce_channel, message.author, new_level, new_total, card_style,
                    boost_pitched=row.get("boost_pitched", False), clone_id=clone_id,
                )
            await self._grant_level_roles(message.author, new_level, clone_id=clone_id)

    async def _send_level_up_card(self, channel, member: discord.Member, new_level: int, new_total_xp: int,
                                   card_style: str = "card", boost_pitched: bool = False, clone_id=None):
        """Renders and sends the level-up announcement. card_style == "text"
        skips the PIL render + avatar fetch entirely and just posts a plain
        message (people asked for this — some don't want the image spam,
        and it's also just faster / no PIL work per level-up). card_style
        == "card" is the original image-card behavior, unchanged. "off" is
        handled by the caller (on_message) before this is even called.

        boost_pitched / clone_id: when new_level is still below 3 and the
        member hasn't been shown the "⚡ Boost XP" button yet (per
        discord_xp.boost_pitched), it's attached to this message and the
        flag is flipped so it isn't repeated on every level-up below 3 —
        see leveling-boost-build-prompt.md §2."""
        show_boost_button = new_level < 3 and not boost_pitched
        if card_style == "text":
            try:
                view = build_boost_xp_view(member.guild.id, clone_id) if show_boost_button else None
                await channel.send(f"🎉 {member.mention} leveled up to **level {new_level}**!", view=view)
                if show_boost_button:
                    await db.mark_boost_pitched(member.guild.id, member.id, clone_id=clone_id)
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
            if new_level >= 10:
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
            view = build_boost_xp_view(member.guild.id, clone_id) if show_boost_button else None
            await channel.send(content=f"🎉 {member.mention} leveled up!", file=file, view=view)
            if show_boost_button:
                await db.mark_boost_pitched(member.guild.id, member.id, clone_id=clone_id)
        except discord.Forbidden:
            pass
        except Exception as e:
            logger.error(f"[v0] Failed to render/send level-up card for {member.id}: {e}")
            try:
                await channel.send(f"🎉 {member.mention} leveled up to **level {new_level}**!")
            except discord.Forbidden:
                pass

    @app_commands.command(name="rank", description="Show your (or someone else's) level and XP")
    @app_commands.guild_only()
    @app_commands.describe(member="Member to check (optional)")
    async def rank(self, interaction: discord.Interaction, member: discord.Member = None):
        await interaction.response.defer()
        target = member or interaction.user
        row = await db.get_xp(interaction.guild_id, target.id, clone_id=_clone_id_of(interaction))
        p = leveling.xp_progress(row["total_xp"])
        bar = _progress_bar(p["current_xp_in_level"], p["xp_needed_for_next_level"])
        line = (
            f"`{bar}` {p['current_xp_in_level']}/{p['xp_needed_for_next_level']} XP "
            f"({p['total_xp']} total)"
        )
        buttons = [ActionButton("Leaderboard", discord.ButtonStyle.secondary, self, "leaderboard", emoji="🏆")]
        card = NavCardView(f"{target.display_name} — level {p['level']}", [line], discord.Color.blurple(), buttons)
        await interaction.followup.send(view=card)

    @app_commands.command(name="leaderboard", description="Show this server's top 10 XP earners")
    @app_commands.guild_only()
    async def leaderboard(self, interaction: discord.Interaction):
        await interaction.response.defer()
        clone_id = _clone_id_of(interaction)
        rows = await db.get_xp_leaderboard(interaction.guild_id, limit=10, clone_id=clone_id)
        if not rows:
            await interaction.followup.send("No XP earned yet.", ephemeral=True)
            return

        # Resolve member/role client-side, same as before — db.get_xp_
        # leaderboard's row shape (user_id, level, total_xp) is untouched.
        members = [interaction.guild.get_member(row["user_id"]) for row in rows]

        async def _fetch_avatar(session, member):
            if member is None:
                return None
            try:
                async with session.get(
                    str(member.display_avatar.replace(size=128).url), timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    return await resp.read()
            except Exception as e:
                logger.warning(f"[v0] Couldn't fetch leaderboard avatar for {member.id}: {e}")
                return None

        # Fetch all avatars concurrently — 10 members means 10 HTTP
        # fetches, and doing them sequentially would make /leaderboard
        # noticeably slow for a full top-10.
        async with aiohttp.ClientSession() as session:
            avatar_results = await asyncio.gather(*(_fetch_avatar(session, m) for m in members))

        entries = []
        view = discord.ui.View(timeout=None)
        view.add_item(BoostXPButton(interaction.guild_id, clone_id))

        for i, (row, member, avatar_bytes) in enumerate(zip(rows, members, avatar_results), start=1):
            name = member.display_name if member else f"User {row['user_id']}"
            role_name = role_color = None
            if member is not None and member.top_role.name != "@everyone":
                role_name = member.top_role.name
                # discord.Colour.default() (no role color set) is 0/black —
                # treat that the same as "no color", same fallback
                # render_leaderboard_card itself applies for a "#000000" hex.
                if member.top_role.color.value:
                    role_color = str(member.top_role.color)
            entries.append({
                "avatar_bytes": avatar_bytes, "display_name": name,
                "level": row["level"], "total_xp": row["total_xp"],
                "role_name": role_name, "role_color": role_color,
            })

            link = await db.get_leader_link(interaction.guild_id, row["user_id"], clone_id=clone_id)
            if link and link["status"] == "approved":
                view.add_item(discord.ui.Button(
                    label=f"#{i} · {name}'s server", style=discord.ButtonStyle.link, url=link["invite_url"]
                ))

        card_bytes = await asyncio.to_thread(render_leaderboard_card, entries, interaction.guild.name)
        file = discord.File(fp=io.BytesIO(card_bytes), filename="leaderboard.png")
        await interaction.followup.send(file=file, view=view)

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
