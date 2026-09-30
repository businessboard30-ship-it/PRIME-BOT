# path: discord_bot/cogs/lookup.py

"""/admin find — admin-only wizard: start typing a server name, a person's
name, OR paste a raw server/user ID directly, and get live autocomplete
suggestions searched across the main bot AND every clone (discord_guilds
is the shared cross-process registry all of
them write to — see clone_manager.py's docstring for why clones can't be
searched in-memory: each one is a separate OS process with its own
gateway connection, so Postgres is the only thing they all actually
share).

Person-name search is backed by discord_username_cache, a small table
that fills in opportunistically rather than from a one-off backfill:
every time this cog (or clone_admin.py's /admin servers) resolves a user_id
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
from discord_bot.cogs._admin_mount import mount_admin_command
from config import DISCORD_CLONE_ADMIN_IDS

logger = logging.getLogger(__name__)


def _is_clone_admin(user_id: int) -> bool:
    return user_id in DISCORD_CLONE_ADMIN_IDS


def _format_payments(rows, show_user: bool = False) -> str:
    """One compact line per payment_logs row for /admin find embeds."""
    lines = []
    for r in rows or []:
        when = r["created_date"].strftime("%Y-%m-%d") if r.get("created_date") else "?"
        who = f" by <@{r['user_id']}>" if show_user and r.get("user_id") else ""
        where = f" in {r['chat_id']}" if not show_user and r.get("chat_id") else ""
        lines.append(f"`{r['status']}` {r.get('payment_type') or '?'} — {r['amount']:g} ({r.get('provider') or 'paystack'}){who}{where} — {when}")
    return "\n".join(lines)[:1024]


class LookupCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        mount_admin_command(
            self.bot, self.find, name="find",
            description="[Admin] Start typing a server name, person's name, or paste a server/user ID",
            autocompletes={"query": self.find_query_autocomplete},
        )

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
        current = (current or "").strip()
        if not current:
            return []

        choices = []
        # Numeric input = the admin is pasting an ID. Names never match digits,
        # so search the ID columns (prefix match) instead of just names.
        if current.isdigit():
            all_rows = await db.get_all_guilds_with_managers(include_left=True)
            guild_rows = [r for r in all_rows if str(r["guild_id"]).startswith(current)][:10]
            user_rows = await db.search_cached_users_by_id_prefix(current, limit=10)
            for r in guild_rows:
                bot_label = "Main bot" if r["clone_id"] is None else f"Clone #{r['clone_id']} ({r['bot_username'] or 'unknown'})"
                left = " [left]" if r["left_at"] else ""
                label = f"🏠 {r['guild_name'] or 'Unknown'}{left} — {bot_label} ({r['guild_id']})"
                choices.append(app_commands.Choice(name=label[:100], value=f"g:{r['guild_id']}"))
            for r in user_rows:
                choices.append(app_commands.Choice(name=f"🧑 {r['username']} ({r['user_id']})"[:100], value=f"u:{r['user_id']}"))
            # A full-length ID we have no record of can still be a real Discord
            # user (resolved via the API), so always offer to look it up.
            if len(current) >= 17 and not any(c.value.endswith(f":{current}") for c in choices):
                choices.append(app_commands.Choice(name=f"🔎 Look up ID {current}"[:100], value=f"i:{current}"))
            return choices[:25]

        guild_rows = await db.search_discord_guilds(current, limit=15)
        user_rows = await db.search_cached_usernames(current, limit=10)
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

        # ── stored invite link(s): the directory/bump listing link first, then
        # any cached invite codes. Works even after the bot has left, since
        # these rows live in Postgres. Cached codes may have expired. ──
        try:
            links = []
            listing = await db.get_server_listing(guild_id)
            if listing and listing.get("invite_url"):
                links.append(f"{listing['invite_url']} (listing)")
            cache = await db.get_invite_cache(guild_id, row["clone_id"])
            for code in list(cache or {})[:5]:
                links.append(f"https://discord.gg/{code} (cached)")
            embed.add_field(name="Invite link", value="\n".join(links)[:1024] if links else "None stored", inline=False)
        except Exception:
            logger.exception("/admin find: couldn't read invite link for guild %s", guild_id)

        # ── deeper: what this server owns + what's been paid for it ──
        try:
            cfg = await db._get_welcome_config_raw(guild_id, row["clone_id"])
            premium = await db.is_guild_premium_active(guild_id, row["clone_id"])
            owns = [
                f"Card Pack: {'✅' if cfg.get('card_pack_unlocked') else '❌'}",
                f"Customize Card: {'✅' if cfg.get('ultra_pack_unlocked') else '❌'}",
                f"Premium: {'✅ active' if premium else '❌'}",
            ]
            embed.add_field(name="Unlocks", value=" • ".join(owns), inline=False)
        except Exception:
            logger.exception("/admin find: couldn't read unlock state for guild %s", guild_id)
        try:
            pays = await db.get_payments_for_guild(guild_id, limit=6)
            embed.add_field(name="Payments for this server",
                            value=_format_payments(pays, show_user=True) or "None on record", inline=False)
        except Exception:
            logger.exception("/admin find: couldn't read payments for guild %s", guild_id)

        await interaction.followup.send(embed=embed, ephemeral=True)

    async def _show_person(self, interaction: discord.Interaction, user_id: int):
        name = await self._resolve_username(user_id)
        owned = await db.get_guilds_owned_by(user_id)
        try:
            clones = await db.get_clones_managed_by(user_id)
        except Exception:
            logger.exception("/admin find: couldn't read clones for %s", user_id)
            clones = []
        try:
            pays = await db.get_payments_for_user(user_id, limit=8)
        except Exception:
            logger.exception("/admin find: couldn't read payments for %s", user_id)
            pays = []

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
        if clones:
            embed.add_field(
                name=f"Manages {len(clones)} clone bot(s)",
                value="\n".join(f"#{c['clone_id']} {c['bot_username'] or 'unknown'} ({c['status']})" for c in clones)[:1024],
                inline=False)
        embed.add_field(name="Recent payments", value=_format_payments(pays) or "None on record", inline=False)
        embed.set_footer(text="Shows servers they OWN and payments/clones on record — not every server they're merely a member of.")
        await interaction.followup.send(embed=embed, ephemeral=True)

    async def _show_id(self, interaction: discord.Interaction, raw_id: int):
        """A pasted numeric ID: known server (incl. ones the bot left) first,
        otherwise treat it as a user and say so plainly if nothing is known."""
        all_rows = await db.get_all_guilds_with_managers(include_left=True)
        if any(r["guild_id"] == raw_id for r in all_rows):
            await self._show_guild(interaction, raw_id)
            return
        name = await self._resolve_username(raw_id)
        owned = await db.get_guilds_owned_by(raw_id)
        clones = await db.get_clones_managed_by(raw_id)
        pays = await db.get_payments_for_user(raw_id, limit=1)
        if name.startswith("unknown") and not (owned or clones or pays):
            await interaction.followup.send(
                f"`{raw_id}` isn't a server any of the bots have been in, and Discord doesn't resolve it as a user. "
                f"It may be a server the bots were never added to, a channel/role/message ID, or a typo.",
                ephemeral=True)
            return
        await self._show_person(interaction, raw_id)

    # ── the command itself ────────────────────────────────────────────
    # Mounted as /admin find (see cog_load).
    @app_commands.describe(query="Server/user ID, or start typing a name — suggestions search live across all bots")
    async def find(self, interaction: discord.Interaction, query: str):
        if not _is_clone_admin(interaction.user.id):
            await interaction.response.send_message("You're not authorized to use this.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)

        query = query.strip()
        prefix, _, rest = query.partition(":")
        if prefix in ("g", "u", "i") and rest.isdigit():
            raw_id = int(rest)
            if prefix == "g":
                await self._show_guild(interaction, raw_id)
            elif prefix == "u":
                await self._show_person(interaction, raw_id)
            else:
                await self._show_id(interaction, raw_id)
            return

        # A raw numeric ID (guild ID or user ID) typed/pasted directly.
        if query.isdigit():
            await self._show_id(interaction, int(query))
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

    async def find_query_autocomplete(self, interaction: discord.Interaction, current: str):
        return await self._find_autocomplete(interaction, current)


async def setup(bot: commands.Bot):
    await bot.add_cog(LookupCog(bot))
