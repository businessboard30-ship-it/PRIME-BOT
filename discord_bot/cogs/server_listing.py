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
from urllib.parse import quote

import discord
from discord.ext import commands, tasks

from config import DASHBOARD_BASE_URL
from database import db
from discord_bot.cogs._dm_support import GuildOnlyCog

logger = logging.getLogger(__name__)

VOTING_CHANNEL_NAME = "vote-for-us"


class ServerListingVotePanelView(discord.ui.View):
    """Persistent Vote/Boost buttons for the in-Discord panel (see
    _ensure_voting_panel below). timeout=None + fixed custom_ids +
    bot.add_view() in setup() below = survives restarts, same pattern
    setup_channels.py's docstring describes for its own persistent views.

    Deliberately NOT the web OAuth flow (api/server_listing_vote_oauth.py):
    a button click already carries a verified Discord identity via
    interaction.user, so there's no sign-in round trip needed at all here —
    this is a second, simpler path to the same cast_server_listing_vote,
    not a replacement for the web one (the public /servers site still needs
    its own vote entry point for people browsing outside Discord).
    """

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Vote", emoji="▲", style=discord.ButtonStyle.success, custom_id="sl_panel_vote")
    async def vote_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild
        if guild is None:
            return
        clone_id = _clone_id_of(interaction)
        newly_voted = await db.cast_server_listing_vote(
            guild.id, clone_id, interaction.user.id, str(interaction.user.display_name)
        )
        if newly_voted:
            msg = "✅ Thanks for voting — it counts instantly on the public directory."
        else:
            # Votes now run on a 12h cooldown rather than being permanent
            # (see cast_server_listing_vote's docstring) — tell the voter
            # when they're free to vote again instead of implying they can
            # never vote for this server again.
            remaining = await db.get_vote_cooldown_remaining(guild.id, interaction.user.id)
            if remaining is not None:
                hours = max(1, int(remaining.total_seconds() // 3600))
                msg = f"You've already voted for this server — you can vote again in about {hours}h."
            else:
                msg = "You've already voted for this server. Thanks for the support!"
        await interaction.response.send_message(msg, ephemeral=True)

        # Refresh the panel message itself so the vote count on the card
        # actually moves — previously this only ever sent the ephemeral
        # reply above and the embed never changed, so the card looked
        # permanently stuck even though the vote WAS recorded in the DB.
        if newly_voted and interaction.message is not None:
            try:
                count = await db.get_server_listing_vote_count(guild.id, clone_id)
                embed = _build_voting_embed(guild, count)
                await interaction.message.edit(embed=embed)
            except (discord.Forbidden, discord.HTTPException):
                pass  # best-effort — the vote itself already succeeded

    @discord.ui.button(label="Boost", emoji="🚀", style=discord.ButtonStyle.primary, custom_id="sl_panel_boost")
    async def boost_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild
        if guild is None:
            return
        clone_id = _clone_id_of(interaction)
        listing = await db.get_server_listing(guild.id, clone_id=clone_id)
        if not listing or not listing.get("ref_code"):
            await interaction.response.send_message(
                "This server hasn't been listed on the directory yet — an admin needs to run "
                "`/setup servers` and finish the listing form first, then a boost link will work.",
                ephemeral=True,
            )
            return
        boost_url = f"{DASHBOARD_BASE_URL}/servers?ref={listing['ref_code']}"
        await interaction.response.send_message(
            f"🚀 **Your boost link:**\n{boost_url}\n\n"
            "Share it anywhere — when someone joins this server through that link, "
            "it counts toward this server's ranking on the directory.",
            ephemeral=True,
        )


def _build_voting_embed(guild: discord.Guild, vote_count: int) -> discord.Embed:
    """Shared embed builder so the panel (on creation/repost) and the vote
    button's refresh (above) never drift out of sync on what the card
    looks like. `vote_count` is a live field now — this is the actual fix
    for the card never showing votes going up."""
    embed = discord.Embed(
        title=f"🗳️ Vote for {guild.name}",
        description=(
            "**▲ Vote** — supports this server on the public directory. One vote per person, "
            "counts instantly.\n\n"
            "**🚀 Boost** — get your own share link. Anyone who joins through it earns this "
            "server ranking credit.\n\n"
            "**🌐 Visit Site** — see this server's public listing page."
        ),
        color=discord.Color.blurple(),
    )
    embed.add_field(name="Votes", value=str(vote_count))
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    return embed


async def _ensure_voting_panel(guild: discord.Guild, clone_id) -> str:
    """Idempotently creates (or reuses) a #vote-for-us channel with the
    Vote/Boost/Visit-Site panel. Safe to call every time /setup servers
    runs: reuses the existing channel+message if both still resolve, only
    reposts if the channel or message got deleted. Returns a short status
    string ("created" | "existing" | "no_permission") for the caller's
    followup message.
    """
    existing = await db.get_server_listing(guild.id, clone_id=clone_id)
    channel = None
    if existing and existing.get("voting_channel_id"):
        channel = guild.get_channel(existing["voting_channel_id"])
        if channel is not None and existing.get("voting_message_id"):
            try:
                await channel.fetch_message(existing["voting_message_id"])
                return "existing"
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass  # message gone — fall through and repost in the same channel

    if channel is None:
        me = guild.me
        if me is None or not me.guild_permissions.manage_channels:
            logger.info(
                "[server_listing] can't create #%s in guild %s (%s): bot lacks Manage Channels",
                VOTING_CHANNEL_NAME, guild.id, guild.name,
            )
            return "no_permission"
        try:
            channel = await guild.create_text_channel(
                VOTING_CHANNEL_NAME, reason="Auto-created by /setup servers for the vote/boost panel",
            )
        except (discord.Forbidden, discord.HTTPException):
            logger.exception(
                "[server_listing] create_text_channel failed in guild %s (%s)", guild.id, guild.name,
            )
            return "no_permission"
        # Persist the channel id immediately — BEFORE attempting to send the
        # panel message. If send() below fails (e.g. the bot can't actually
        # post/embed in a channel it just created), this ensures the next
        # call reuses THIS channel instead of leaking a new one every retry.
        # message_id=0 is a sentinel meaning "channel exists, panel not
        # posted yet"; the fetch_message check above only trusts a nonzero
        # voting_message_id, so this row alone won't short-circuit as
        # "existing" — it'll fall through and retry the send.
        await db.set_listing_voting_panel(
            guild.id, clone_id, channel.id, 0,
            guild_name=guild.name, guild_icon_url=guild.icon.url if guild.icon else None,
            member_count=guild.member_count or 0,
        )

    current_votes = await db.get_server_listing_vote_count(guild.id, clone_id)
    embed = _build_voting_embed(guild, current_votes)

    view = ServerListingVotePanelView()
    view.add_item(discord.ui.Button(
        label="Visit Site", emoji="🌐", style=discord.ButtonStyle.link,
        url=f"{DASHBOARD_BASE_URL}/servers/{guild.id}",
    ))

    try:
        message = await channel.send(embed=embed, view=view)
    except (discord.Forbidden, discord.HTTPException):
        logger.exception(
            "[server_listing] channel.send failed for #%s in guild %s (%s) — channel exists but bot "
            "can't post/embed in it",
            channel.name, guild.id, guild.name,
        )
        return "no_permission"

    await db.set_listing_voting_panel(
        guild.id, clone_id, channel.id, message.id,
        guild_name=guild.name, guild_icon_url=guild.icon.url if guild.icon else None,
        member_count=guild.member_count or 0,
    )
    return "created"


async def _auto_generate_invite(guild: discord.Guild) -> str | None:
    """Best-effort permanent invite (max_age=0, max_uses=0) so admins don't
    have to go find/paste one themselves. Tries the guild's configured
    system/rules channel first (most likely to already be public-facing),
    then falls back to the first text channel the bot can actually create
    an invite in. Returns None on any permission/API failure — the submit
    page falls back to a manual paste in that case, same as before this
    feature existed."""
    candidates = [c for c in (guild.system_channel, guild.rules_channel) if c is not None]
    candidates += [c for c in guild.text_channels if c not in candidates]

    for channel in candidates:
        perms = channel.permissions_for(guild.me)
        if not perms.create_instant_invite:
            continue
        try:
            invite = await channel.create_invite(
                max_age=0, max_uses=0, unique=False,
                reason="Auto-generated for the public server directory listing",
            )
            return invite.url
        except (discord.Forbidden, discord.HTTPException):
            continue
    return None


def _clone_id_of(interaction: discord.Interaction):
    return getattr(interaction.client, "clone_id", None)


def _clone_id_of_bot(bot: commands.Bot):
    return getattr(bot, "clone_id", None)


class ServerListingCog(GuildOnlyCog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._voting_panel_poller.start()

    def cog_unload(self):
        self._voting_panel_poller.cancel()

    @tasks.loop(seconds=60)
    async def _voting_panel_poller(self):
        """Picks up listings the website marked voting_panel_pending (set by
        upsert_server_listing on submit) and does the actual channel/panel
        creation here, since api/server_listings.py — where the submit is
        handled — is a separate process with no live guild/Discord access.
        This is what makes the panel appear automatically on submit rather
        than needing an admin to rerun /setup servers.

        clear_voting_panel_pending is now only called after a resolved
        outcome — success ("created"/"existing") or a guild that no bot
        process could ever produce. It used to fire unconditionally right
        after every attempt, which caused two silent, permanent failures:
        1. "no_permission" (missing Manage Channels, or the send() itself
           failing) was swallowed with no log line and the flag cleared —
           so a fixable permission problem never got retried and never
           told anyone why.
        2. get_pending_voting_panels polls ALL pending rows on EVERY bot
           process (main + every clone), each checking only its own
           gateway cache. A guild that only belongs to a clone (not the
           main bot) would hit `guild is None` on the main bot's poll
           tick, which — since it cleared unconditionally — could
           permanently discard the row before the clone's own poll ever
           ran, racing the one process that could actually do the work.
           Leaving it pending on guild=None lets every process retry
           cheaply (a plain cache lookup) until whichever one actually has
           the guild claims it."""
        clone_id = _clone_id_of_bot(self.bot)
        try:
            pending = await db.get_pending_voting_panels(clone_id)
        except Exception:
            logger.exception("[server_listing] failed polling for pending voting panels")
            return
        for row in pending:
            guild = self.bot.get_guild(row["guild_id"])
            if guild is None:
                logger.info(
                    "[server_listing] guild %s not in this process's cache — leaving pending "
                    "for another clone/the main bot to pick up",
                    row["guild_id"],
                )
                continue
            try:
                status = await _ensure_voting_panel(guild, row["clone_id"])
            except Exception:
                logger.exception(
                    "[server_listing] failed auto-creating voting panel for guild %s (%s) — will retry next poll",
                    guild.id, guild.name,
                )
                continue
            if status == "no_permission":
                logger.warning(
                    "[server_listing] voting panel NOT created for guild %s (%s): bot lacks "
                    "Manage Channels (or can't post in the channel it made) — will keep retrying "
                    "every 60s until permissions are fixed",
                    guild.id, guild.name,
                )
                continue
            logger.info(
                "[server_listing] voting panel for guild %s (%s): %s", guild.id, guild.name, status,
            )
            await db.clear_voting_panel_pending(row["guild_id"], row["clone_id"])

    @_voting_panel_poller.before_loop
    async def _before_voting_panel_poller(self):
        await self.bot.wait_until_ready()

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
        # Points at the landing page (with these params attached) rather than
        # straight at /servers/submit, so first-time users land on "/" and see
        # what PRIME-BOT is before being dropped into the listing form — the
        # landing page reads these same params and shows a "continue to your
        # listing" banner with a link on to /servers/submit (see app/page.tsx).
        url = f"{DASHBOARD_BASE_URL}/?guild_id={guild.id}&token={token}"
        if clone_id is not None:
            url += f"&clone_id={clone_id}"

        existing = await db.get_server_listing(guild.id, clone_id=clone_id)

        # Only auto-fill invite/description for a brand-new listing — an
        # existing one already has its own saved invite_code (which
        # on_member_join above uses for conversion tracking) and whatever
        # description the admin already wrote, so re-running this command
        # must never silently clobber either with fresh Discord data.
        got_invite = False
        got_description = False
        if not existing:
            auto_invite = await _auto_generate_invite(guild)
            if auto_invite:
                url += f"&invite_url={quote(auto_invite, safe='')}"
                got_invite = True

            # guild.description is only ever set for Community-enabled
            # servers (Server Settings -> Community -> description) —
            # discord.py returns None for every other guild, so this is a
            # no-op prefill for the common case rather than a guaranteed one.
            if guild.description:
                url += f"&description={quote(guild.description, safe='')}"
                got_description = True

        if got_invite and got_description:
            auto_fill_note = " We've already filled in a permanent invite and your server's description — just add tags."
        elif got_invite:
            auto_fill_note = " We've already generated a permanent invite for you — just add a description and tags."
        elif got_description:
            auto_fill_note = " We've already pulled in your server's description — just add an invite link and tags."
        else:
            auto_fill_note = ""

        status_line = (
            "You're already listed — this link opens your listing so you can edit it."
            if existing else
            f"Fill in a short description and it goes live immediately — no approval wait.{auto_fill_note}"
        )
        await interaction.followup.send(
            f"📋 **Server directory link** (keep this private — it edits your listing, same as a password):\n"
            f"{url}\n\n{status_line}",
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    bot.add_view(ServerListingVotePanelView())
    await bot.add_cog(ServerListingCog(bot))
