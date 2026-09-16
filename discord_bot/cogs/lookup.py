# path: discord_bot/cogs/lookup.py

"""/find — admin-only wizard: start typing a server name, get live
autocomplete suggestions searched across the main bot AND every clone
(discord_guilds is the shared cross-process registry all of them write
to — see clone_manager.py's docstring for why clones can't be searched
in-memory: each one is a separate OS process with its own gateway
connection, so Postgres is the only thing they all actually share).

Picking a suggestion shows the full row: guild_id, which bot manages it,
member count, when it joined, and the server owner's resolved username.

Person-name search (buyer name / Discord username instead of server
name) is NOT included here — there's no persistent username cache table
anywhere in this codebase yet (clone_admin.py's _resolve_username does a
live bot.fetch_user() per command, fine for one-off lookups but far too
slow to run on every autocomplete keystroke across potentially hundreds
of guilds). Adding that would need a small cache table populated
incrementally (e.g. on_member_join, or whenever a name is resolved
elsewhere) rather than something that can search historical data from
day one.
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

    async def _guild_autocomplete(self, interaction: discord.Interaction, current: str):
        if not _is_clone_admin(interaction.user.id):
            return []
        if not current:
            return []
        rows = await db.search_discord_guilds(current, limit=25)
        choices = []
        for r in rows:
            bot_label = "Main bot" if r["clone_id"] is None else f"Clone #{r['clone_id']} ({r['bot_username'] or 'unknown'})"
            label = f"{r['guild_name'] or 'Unknown'} — {bot_label} ({r['member_count'] or '?'} members)"
            # Discord caps autocomplete choice labels at 100 chars.
            choices.append(app_commands.Choice(name=label[:100], value=str(r["guild_id"])))
        return choices

    async def _resolve_username(self, user_id: int) -> str:
        if not user_id:
            return "unknown"
        user = self.bot.get_user(user_id)
        if user is None:
            try:
                user = await self.bot.fetch_user(user_id)
            except discord.HTTPException:
                user = None
        return f"{user.name} ({user_id})" if user else f"unknown ({user_id})"

    @app_commands.command(
        name="find",
        description="[Admin] Start typing a server name to search across the main bot and every clone",
    )
    @app_commands.describe(server="Start typing — suggestions search live across all bots")
    async def find(self, interaction: discord.Interaction, server: str):
        if not _is_clone_admin(interaction.user.id):
            await interaction.response.send_message("You're not authorized to use this.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)

        try:
            guild_id = int(server)
        except ValueError:
            # They typed free text instead of picking a suggestion — do
            # the same search one more time and just take the top hit.
            rows = await db.search_discord_guilds(server, limit=1)
            if not rows:
                await interaction.followup.send(f"No server matching **{server}** found.", ephemeral=True)
                return
            guild_id = rows[0]["guild_id"]

        # Re-fetch the specific row by id rather than trusting the earlier
        # search result's staleness (guild could've been left in between).
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

    @find.autocomplete("server")
    async def find_server_autocomplete(self, interaction: discord.Interaction, current: str):
        return await self._guild_autocomplete(interaction, current)


async def setup(bot: commands.Bot):
    await bot.add_cog(LookupCog(bot))
