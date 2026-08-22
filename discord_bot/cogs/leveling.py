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
import time

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
from discord_bot.cogs._dm_support import GuildOnlyCog

from database import db
from modules import leveling
from modules.level_card import render_level_card
from discord_bot.cogs._views_shared import ActionButton, NavCardView
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
        self._last_award: dict[tuple[int, int], float] = {}

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
        key = (message.guild.id, message.author.id)
        now = time.monotonic()
        last = self._last_award.get(key, 0)
        if now - last < XP_COOLDOWN_SECONDS:
            return
        self._last_award[key] = now

        clone_id = getattr(self.bot, "clone_id", None)
        current = await db.get_xp(message.guild.id, message.author.id, clone_id=clone_id)
        old_level = leveling.compute_level(current["total_xp"])
        config = await db.get_leveling_config(message.guild.id, clone_id=clone_id)
        multiplier = XP_RATE_MULTIPLIERS.get(config.get("xp_rate", "default"), 1.0)
        gained = max(1, round(random.randint(XP_MIN, XP_MAX) * multiplier))
        new_total = current["total_xp"] + gained
        new_level = leveling.compute_level(new_total)
        await db.add_xp(message.guild.id, message.author.id, gained, new_level, clone_id=clone_id)

        if new_level > old_level and isinstance(message.author, discord.Member):
            announce_channel = message.channel
            announce_channel_id = config.get("announce_channel_id")
            if announce_channel_id:
                found = message.guild.get_channel(int(announce_channel_id))
                if found is not None:
                    announce_channel = found
            await self._send_level_up_card(announce_channel, message.author, new_level, new_total)
            await self._grant_level_roles(message.author, new_level, clone_id=clone_id)

    async def _send_level_up_card(self, channel, member: discord.Member, new_level: int, new_total_xp: int):
        """Renders and sends the level-up image card, pulling the avatar from
        the member's live Discord profile. Falls back to a plain text message
        if the avatar fetch or image render fails for any reason."""
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

    @app_commands.command(name="leaderboard", description="Show this server's top XP earners")
    @app_commands.guild_only()
    async def leaderboard(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        rows = await db.get_xp_leaderboard(interaction.guild_id, limit=10, clone_id=_clone_id_of(interaction))
        if not rows:
            await interaction.followup.send("No XP earned yet.", ephemeral=True)
            return
        lines = []
        for i, row in enumerate(rows, start=1):
            member = interaction.guild.get_member(row["user_id"])
            name = member.display_name if member else f"User {row['user_id']}"
            lines.append(f"**{i}.** {name} — Level {row['level']} ({row['total_xp']} XP)")
        buttons = [ActionButton("My rank", discord.ButtonStyle.secondary, self, "rank", emoji="📊")]
        card = NavCardView("🏆 XP leaderboard", lines, discord.Color.gold(), buttons)
        await interaction.followup.send(view=card)

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
