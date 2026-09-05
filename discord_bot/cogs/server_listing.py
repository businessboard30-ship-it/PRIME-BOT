# path: discord_bot/cogs/server_listing.py

"""
"List your server" — hands an admin a personal link into the public
directory on the website (app/servers).

NOT a standalone slash command — the bot was already at/over Discord's
100 top-level-command cap, so this is exposed as a subcommand of the
existing /setup group instead (see request_listing_link below, called
from discord_bot/cogs/setup_channels.py's `/setup servers`), the same
cross-cog delegation pattern that group already uses for roast/ship
(get_cog("ServerListingCog") -> call a plain method). Adding a subcommand
to an EXISTING top-level group costs nothing against the cap — only
standalone commands and top-level Groups themselves count.

Deliberately NOT an open web form: the only way to get a listing link is to
run /setup servers inside a guild the bot is already in, as someone with
Manage Server. That's the whole "auto-approve only if bot confirmed in
server" requirement — there's no separate verification step to build because
the token literally cannot exist otherwise. See server_listing_tokens' and
server_listings' comments in database.py's _create_tables for the full trust
model (same shape as discord_dashboard_tokens / /automod dashboard).

The vote/boost/join additions on top of this deliberately add ZERO further
commands either: voting is a web sign-in link
(api/server_listing_vote_oauth.py), joining just opens invite_url directly,
and referral-boost conversion is picked up for free below in
on_member_join, an event listener rather than a command.
"""

import logging

import discord
from discord.ext import commands

from config import DASHBOARD_BASE_URL
from database import db
from discord_bot.cogs._dm_support import GuildOnlyCog

logger = logging.getLogger(__name__)


def _clone_id_of(interaction: discord.Interaction):
    return getattr(interaction.client, "clone_id", None)


def _clone_id_of_bot(bot: commands.Bot):
    return getattr(bot, "clone_id", None)


class ServerListingCog(GuildOnlyCog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ── referral-boost conversion (event listener, not a command) ─────────

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        """Piggybacks on the same join event discord_bot/cogs/invites.py
        already listens on, but does its own narrow invites() fetch rather
        than sharing that cog's cache — this only cares about ONE specific
        invite (the listing's own, if this guild has a listing at all), so
        there's no reason to couple to invites.py's general-purpose
        multi-invite diffing or its Manage Server config gate. Silently
        no-ops for guilds with no listing, or if the bot lacks Manage
        Server here (same "no permission, no guess" stance as invites.py)."""
        if member.bot:
            return
        guild = member.guild
        clone_id = _clone_id_of_bot(self.bot)
        listing = await db.get_server_listing(guild.id, clone_id=clone_id)
        if not listing or not listing.get("invite_code"):
            return
        try:
            invites = await guild.invites()
        except (discord.Forbidden, discord.HTTPException):
            return
        match = next((inv for inv in invites if inv.code == listing["invite_code"]), None)
        if match is None:
            return
        try:
            await db.check_ref_conversion(guild.id, clone_id, listing["invite_code"], match.uses or 0)
        except Exception:
            logger.exception(f"[server_listing] ref-conversion check failed for guild {guild.id}")

    # ── /setup servers delegates here (see setup_channels.py) ─────────────

    async def request_listing_link(self, interaction: discord.Interaction):
        """Same body /servers used to have as its own top-level command —
        moved verbatim, just no longer @app_commands.command-decorated."""
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        if guild is None:
            await interaction.followup.send("This only works in a server.", ephemeral=True)
            return
        if not interaction.user.guild_permissions.manage_guild:
            await interaction.followup.send(
                "You need the **Manage Server** permission to list this server.", ephemeral=True
            )
            return

        clone_id = _clone_id_of(interaction)
        icon_url = guild.icon.url if guild.icon else None
        token = await db.get_or_create_listing_token(
            guild.id, guild.name, icon_url, guild.member_count or 0, clone_id=clone_id,
        )
        url = f"{DASHBOARD_BASE_URL}/servers/submit?guild_id={guild.id}&token={token}"
        if clone_id is not None:
            url += f"&clone_id={clone_id}"

        existing = await db.get_server_listing(guild.id, clone_id=clone_id)
        status_line = (
            "You're already listed — this link opens your listing so you can edit it."
            if existing else
            "Fill in your invite link and a short description, and it goes live immediately — no approval wait."
        )
        await interaction.followup.send(
            f"📋 **Server directory link** (keep this private — it edits your listing, same as a password):\n"
            f"{url}\n\n{status_line}",
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(ServerListingCog(bot))
