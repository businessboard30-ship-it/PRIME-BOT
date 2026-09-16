# path: discord_bot/cogs/lookup.py

"""/find — admin-only wizard: start typing a server name OR a person's
name, get live autocomplete suggestions searched across the main bot AND
every clone (discord_guilds is the shared cross-process registry all of
them write to — see clone_manager.py's docstring for why clones can't be
searched in-memory: each one is a separate OS process with its own
gateway connection, so Postgres is the only thing they all actually
share).

Person-name search is backed by discord_username_cache, a small table
that fills in opportunistically rather than from a one-off backfill:
every time this cog (or clone_admin.py's /allservers) resolves a user_id
to a name, it also writes it here, and on_member_join below caches
everyone as they join. That means there's no search history for anyone
who joined before this shipped and hasn't been resolved by anything
since — coverage grows over time, it doesn't start complete.

Picking a suggestion shows either: the full guild row (id, which bot
manages it, member count, join date, resolved owner name), or, for a
person, their resolved name plus every currently-joined guild they own
across the whole main-bot+clones roster.
"""

import logging

import discord
from discord import app_commands
from discord.ext import commands

from database import db
from config import DISCORD_CLONE_ADMIN_IDS

logger = logging.getLogger(__name__)


def _is_clone_admin(user_id: int) -> bool:
    return user_id in DISCORD_CLONE_ADMIN_IDS


class LookupCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ── opportunistic username-cache population ─────────────────────────
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        try:
            await db.cache_username(member.id, str(member))
        except Exception:
            logger.exception("Failed to cache username for member %s on join", member.id)

    async def _resolve_username(self, user_id: int) -> str:
        if not user_id:
            return "unknown"
        user = self.bot.get_user(user_id)
        if user is None:
            try:
                user = await self.bot.fetch_user(user_id)
            except discord.HTTPException:
                user = None
        if user is None:
            return f"unknown ({user_id})"
        name = f"{user.name} ({user_id})"
        try:
            await db.cache_username(user_id, str(user))
        except Exception:
            logger.exception("Failed to cache resolved username for %s", user_id)
        return name

    # ── autocomplete: merges guild-name and username-cache hits ─────────
    async def _find_autocomplete(self, interaction: discord.Interaction, current: str):
        if not _is_clone_admin(interaction.user.id):
            return []
        if not current:
            return []

        guild_rows = await db.search_discord_guilds(current, limit=15)
        user_rows = await db.search_cached_usernames(current, limit=10)

        choices = []
        for r in guild_rows:
            bot_label = "Main bot" if r["clone_id"] is None else f"Clone #{r['clone_id']} ({r['bot_username'] or 'unknown'})"
            label = f"🏠 {r['guild_name'] or 'Unknown'} — {bot_label} ({r['member_count'] or '?'} members)"
            choices.append(app_commands.Choice(name=label[:100], value=f"g:{r['guild_id']}"))
        for r in user_rows:
            label = f"🧑 {r['username']} ({r['user_id']})"
            choices.append(app_commands.Choice(name=label[:100], value=f"u:{r['user_id']}"))

        return choices[:25]

    # ── result views ──────────────────────────────────────────────────
    async def _show_guild(self, interaction: discord.Interaction, guild_id: int):
        all_rows = await db.get_all_guilds_with_managers(include_left=True)
        row = next((r for r in all_rows if r["guild_id"] == guild_id), None)
        if row is None:
            await interaction.followup.send("That server isn't on record.", ephemeral=True)
            return

        bot_label = "Main bot" if row["clone_id"] is None else f"Clone #{row['clone_id']} ({row['bot_username'] or 'unknown'})"
        owner_name = await self._resolve_username(row["server_owner_id"])
        manager_name = "—" if row["clone_id"] is None else await self._resolve_username(row["manager_id"])
        status = "left" if row["left_at"] else "active"

        embed = discord.Embed(title=row["guild_name"] or "Unknown server", color=discord.Color.blurple())
        embed.add_field(name="Server ID", value=str(row["guild_id"]), inline=False)
        embed.add_field(name="Bot", value=bot_label, inline=True)
        embed.add_field(name="Status", value=status, inline=True)
        embed.add_field(name="Members", value=str(row["member_count"] or "?"), inline=True)
        embed.add_field(name="Server owner", value=owner_name, inline=True)
        embed.add_field(name="Managed by", value=manager_name, inline=True)
        if row["joined_at"]:
            embed.add_field(name="Joined", value=str(row["joined_at"]), inline=True)

        await interaction.followup.send(embed=embed, ephemeral=True)

    async def _show_person(self, interaction: discord.Interaction, user_id: int):
        name = await self._resolve_username(user_id)
        owned = await db.get_guilds_owned_by(user_id)

        embed = discord.Embed(title=name, color=discord.Color.green())
        embed.add_field(name="User ID", value=str(user_id), inline=False)
        if owned:
            lines = []
            for g in owned:
                bot_label = "Main bot" if g["clone_id"] is None else f"Clone #{g['clone_id']} ({g['bot_username'] or 'unknown'})"
                lines.append(f"**{g['guild_name'] or 'Unknown'}** — {bot_label} ({g['member_count'] or '?'} members)")
            embed.add_field(name=f"Owns {len(owned)} server(s)", value="\n".join(lines)[:1024], inline=False)
        else:
            embed.add_field(name="Owns", value="No servers on record", inline=False)

        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── the command itself ────────────────────────────────────────────
    @app_commands.command(
        name="find",
        description="[Admin] Start typing a server name or a person's name — searches across the main bot and every clone",
    )
    @app_commands.describe(query="Start typing — suggestions search live across all bots")
    async def find(self, interaction: discord.Interaction, query: str):
        if not _is_clone_admin(interaction.user.id):
            await interaction.response.send_message("You're not authorized to use this.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)

        if query.startswith("g:"):
            await self._show_guild(interaction, int(query[2:]))
            return
        if query.startswith("u:"):
            await self._show_person(interaction, int(query[2:]))
            return

        # They typed free text instead of picking a suggestion — search
        # both one more time and take the top hit, preferring an exact
        # guild-name match over a username match if both exist.
        guild_rows = await db.search_discord_guilds(query, limit=1)
        if guild_rows:
            await self._show_guild(interaction, guild_rows[0]["guild_id"])
            return
        user_rows = await db.search_cached_usernames(query, limit=1)
        if user_rows:
            await self._show_person(interaction, user_rows[0]["user_id"])
            return
        await interaction.followup.send(f"Nothing matching **{query}** found.", ephemeral=True)

    @find.autocomplete("query")
    async def find_query_autocomplete(self, interaction: discord.Interaction, current: str):
        return await self._find_autocomplete(interaction, current)


async def setup(bot: commands.Bot):
    await bot.add_cog(LookupCog(bot))
