"""
Bots Archive — /archive command group. See modules/archive_adapter.py for
the schema/scoring/lookup logic this cog wires up to Discord.

Everything here is opt-in per guild via /archive enable — nothing posts,
reviews, or lists anything until an owner (DISCORD_CLONE_ADMIN_IDS) turns
it on for a specific guild with specific channels.
"""

import logging

import discord
from discord import app_commands
from discord.ext import commands, tasks

from config import DISCORD_CLONE_ADMIN_IDS
from modules import archive_adapter as arc
from payments import PaystackPayment
from discord_bot.cogs._views_archive import ArchiveReviewView, render_listing_embed

logger = logging.getLogger(__name__)

CATEGORIES = ["Moderation", "Economy", "Leveling", "AI", "Anime", "Utility", "Music", "Other"]

# Shown in the card title when a listing has no bot_icon_url yet (Discord's
# RPC lookup only returns an icon if the developer set one in the Portal).
# Picked deterministically per-listing so the same bot doesn't flicker
# between emoji across card refreshes.
_PLACEHOLDER_EMOJI_POOL = ["🤖", "🛰️", "🧩", "⚙️", "🗂️"]


def _placeholder_emoji_for(listing_id: int) -> str:
    return _PLACEHOLDER_EMOJI_POOL[listing_id % len(_PLACEHOLDER_EMOJI_POOL)]


_PLACEHOLDER_EMOJI = "🤖"  # fallback used where no listing_id is available yet


def _is_owner(user_id: int) -> bool:
    return user_id in DISCORD_CLONE_ADMIN_IDS


def _status_color(status: str) -> discord.Color:
    return {
        "approved": discord.Color.green(),
        "denied": discord.Color.red(),
        "pending_review": discord.Color.blurple(),
        "flagged_for_review": discord.Color.orange(),
    }.get(status, discord.Color.greyple())


class ArchiveCardView(discord.ui.LayoutView):
    """Components V2 version of the public listing card. Persistent
    (timeout=None, fixed custom_id prefix) for the same reason
    VoteInviteView was: buttons must survive bot restarts via
    bot.add_view(), and the actual vote handling lives in
    ArchiveCog.on_interaction, dispatched by custom_id prefix."""

    def __init__(self, listing: dict, votes: int, status: str = "approved", reason: str = None):
        super().__init__(timeout=None)
        listing_id = listing["id"]
        header = _STATUS_HEADERS.get(status, status)
        accent = _STATUS_ACCENTS.get(status, discord.Color.greyple())

        icon_url = listing.get("bot_icon_url")
        # No app icon set on Discord yet — lead the title with a placeholder
        # emoji instead of leaving the card bare. _IconRecheckTask swaps this
        # for the real Thumbnail automatically once the bot sets one.
        name_display = listing['bot_name'] if icon_url else f"{_placeholder_emoji_for(listing_id)} {listing['bot_name']}"

        body_lines = [f"**{header}**", f"### {name_display}"]
        body_lines.append(listing.get("description") or reason or "No description provided.")
        body_lines.append(
            f"-# Category: {listing.get('category') or 'Other'}  ·  "
            f"Votes: {votes}  ·  Developer: <@{listing['submitter_id']}>"
        )
        if listing.get("tags"):
            body_lines.append("-# Tags: " + ", ".join(f"`{t}`" for t in listing["tags"]))
        body_lines.append(f"-# Listing #{listing_id} · Bots Archive")
        text = discord.ui.TextDisplay("\n".join(body_lines))

        if icon_url:
            display = discord.ui.Section(text, accessory=discord.ui.Thumbnail(icon_url))
        else:
            display = text

        row = discord.ui.ActionRow()
        invite_link = listing.get("invite_link")
        support_server = listing.get("support_server")
        if invite_link:
            row.add_item(discord.ui.Button(label="Invite bot", style=discord.ButtonStyle.link, url=invite_link))
        if support_server:
            row.add_item(discord.ui.Button(label="Support server", style=discord.ButtonStyle.link, url=support_server))
        row.add_item(discord.ui.Button(
            label="Vote", style=discord.ButtonStyle.success, emoji="🗳️",
            custom_id=f"archive_vote:{listing_id}",
        ))

        container = discord.ui.Container(display, discord.ui.Separator(), row, accent_colour=accent)
        self.add_item(container)


class DisputeModal(discord.ui.Modal, title="Dispute this decision"):
    reason = discord.ui.TextInput(label="Why should this be reconsidered?", style=discord.TextStyle.paragraph, max_length=1000)

    def __init__(self, listing_id: int):
        super().__init__()
        self.listing_id = listing_id

    async def on_submit(self, interaction: discord.Interaction):
        await arc.create_dispute(self.listing_id, interaction.user.id, str(self.reason))
        await interaction.response.send_message("Your dispute was sent to the archive owner. You'll hear back soon.", ephemeral=True)
        listing = await arc.get_listing(self.listing_id)
        for admin_id in DISCORD_CLONE_ADMIN_IDS:
            try:
                user = await interaction.client.fetch_user(admin_id)
                await user.send(
                    f"⚖️ Dispute on listing #{self.listing_id} (**{listing['bot_name']}**) "
                    f"from <@{interaction.user.id}>:\n> {self.reason}\n\n"
                    f"Use `/archive resolve listing_id:{self.listing_id} approve:True/False` to resolve it."
                )
            except discord.HTTPException:
                pass


class DisputeButtonView(discord.ui.View):
    """No callback here either — see VoteInviteView's docstring. Handled by
    ArchiveCog.on_interaction via the archive_dispute:<id> custom_id prefix."""

    def __init__(self, listing_id: int):
        super().__init__(timeout=None)
        self.add_item(discord.ui.Button(
            label="Dispute this decision", style=discord.ButtonStyle.secondary,
            custom_id=f"archive_dispute:{listing_id}",
        ))


_STATUS_ACCENTS = {
    "approved": discord.Color.green(),
    "flagged_for_review": discord.Color.blurple(),
    "denied": discord.Color.red(),
}
_STATUS_HEADERS = {
    "approved": "✅ Approved and live",
    "flagged_for_review": "📋 In review",
    "denied": "❌ Submission denied",
}


def _build_card_view(listing: dict, votes: int, status: str, reason: str = None) -> discord.ui.LayoutView:
    """Components V2 layout for a listing card — replaces the plain-text /
    bare-embed outcome message with the same styled card used publicly."""
    view = discord.ui.LayoutView()
    header = _STATUS_HEADERS.get(status, status)
    accent = _STATUS_ACCENTS.get(status, discord.Color.greyple())

    icon_url = listing.get("bot_icon_url")
    name_display = listing['bot_name'] if icon_url else f"{_placeholder_emoji_for(listing['id'])} {listing['bot_name']}"

    body_lines = [f"**{header}**", f"### {name_display}"]
    body_lines.append(listing.get("description") or reason or "No description provided.")
    body_lines.append(
        f"-# Category: {listing.get('category') or 'Other'}  ·  "
        f"Votes: {votes}  ·  Developer: <@{listing['submitter_id']}>"
    )
    if listing.get("tags"):
        body_lines.append("-# Tags: " + ", ".join(f"`{t}`" for t in listing["tags"]))
    body_lines.append(f"-# Listing #{listing['id']} · Bots Archive")

    text = discord.ui.TextDisplay("\n".join(body_lines))

    if icon_url:
        section = discord.ui.Section(
            text, accessory=discord.ui.Thumbnail(icon_url)
        )
        container = discord.ui.Container(section, accent_colour=accent)
    else:
        container = discord.ui.Container(text, accent_colour=accent)

    view.add_item(container)
    return view


class ArchiveCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        await arc.ensure_ready()
        self._details_recheck.start()

    async def cog_unload(self):
        self._details_recheck.cancel()

    @tasks.loop(hours=6)
    async def _details_recheck(self):
        """Periodically re-looks-up approved listings against Discord so
        the posted card stays in sync with the bot's current name and
        icon — not just filling in an icon that was missing at submit
        time, but catching renames too. Updates the DB row and edits the
        already-posted card in place; no new card, no resubmission."""
        try:
            candidates = await arc.approved_listings_for_rescan(stale_hours=6, limit=25)
        except Exception as e:
            logger.error(f"[v0] archive icon recheck: couldn't fetch candidates: {e}")
            return
        for listing in candidates:
            app_data = await arc.fetch_application(str(listing["application_id"]))
            await arc.mark_rescanned(listing["id"])
            if not app_data:
                continue
            changed = await arc.sync_bot_details(
                listing["id"], name=app_data.get("name"), icon_url=app_data.get("icon_url")
            )
            if not (changed["name"] or changed["icon"]):
                continue
            logger.info(
                f"[v0] archive icon recheck: listing {listing['id']} updated "
                f"(name={changed['name']}, icon={changed['icon']})"
            )
            if listing.get("channel_id") and listing.get("message_id"):
                votes = await arc.vote_count(listing["id"])
                await self._refresh_card(listing["id"], votes)

    @_details_recheck.before_loop
    async def _before_details_recheck(self):
        await self.bot.wait_until_ready()

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        # Vote/dispute buttons use a per-listing custom_id (archive_vote:<id>,
        # archive_dispute:<id>), so they can't be reattached via bot.add_view()
        # on restart — add_view only matches views with fixed custom_ids.
        # Handling them here means the button keeps working on old messages
        # even after the process restarts and the original View instance
        # that posted the card is long gone.
        if interaction.type != discord.InteractionType.component:
            return
        custom_id = interaction.data.get("custom_id", "")
        if custom_id.startswith("archive_vote:"):
            listing_id = int(custom_id.split(":", 1)[1])
            await interaction.response.defer(ephemeral=True)
            ok = await arc.try_vote(listing_id, interaction.user.id)
            if not ok:
                await interaction.followup.send(
                    "You already voted for this bot recently — try again later.", ephemeral=True
                )
                return
            count = await arc.vote_count(listing_id)
            logger.info(f"[v0] archive vote recorded: listing={listing_id} voter={interaction.user.id} new_count={count}")
            result = await self._refresh_card(listing_id, count)
            logger.info(f"[v0] archive vote refresh result: {result}")
            await interaction.followup.send(f"✅ Vote counted! This bot now has **{count}** votes.", ephemeral=True)
        elif custom_id.startswith("archive_dispute:"):
            listing_id = int(custom_id.split(":", 1)[1])
            await interaction.response.send_modal(DisputeModal(listing_id))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # guild_only() blocks true DMs, but it does NOT catch a user-installed
        # app invoked inside a guild channel — Discord still reports that as
        # a "private channel" context there, so interaction.guild_id comes
        # back None even though the channel visibly belongs to a server. Every
        # command in this group writes rows keyed on guild_id, so we hard-stop
        # here instead of hitting a NotNullViolationError deep in the DB layer.
        if interaction.guild_id is None:
            await interaction.response.send_message(
                "This only works when the bot is added to the server (not as your personal app). "
                "Ask a server admin to add PrimeBot with the 'Add to Server' option, then try again here.",
                ephemeral=True,
            )
            return False
        return True

    group = app_commands.guild_only()(app_commands.Group(name="archive", description="Bots Archive — submit, review, and browse"))

    # ── Owner config ─────────────────────────────────────────────────────

    @group.command(name="enable", description="[Owner] Enable the archive system for this server")
    @app_commands.describe(
        listing_channel="Where approved bot cards get posted",
        second_channel="Optional second listing channel",
        featured_channel="Optional channel for trending/featured posts",
    )
    async def enable(self, interaction: discord.Interaction, listing_channel: discord.TextChannel,
                      second_channel: discord.TextChannel = None, featured_channel: discord.TextChannel = None):
        if not _is_owner(interaction.user.id):
            await interaction.response.send_message(
                "Only the archive owner can do that.", ephemeral=True,
            )
            return
        channel_ids = [listing_channel.id] + ([second_channel.id] if second_channel else [])
        await arc.enable_guild(interaction.guild_id, channel_ids, featured_channel.id if featured_channel else None, interaction.user.id)
        await interaction.response.send_message(
            f"✅ Archive enabled. Listings post to {listing_channel.mention}"
            + (f" and {second_channel.mention}" if second_channel else "") + ".",
            ephemeral=True,
        )

    # ── Submission ───────────────────────────────────────────────────────

    @group.command(name="submit", description="Submit your bot to the archive (by Application ID)")
    @app_commands.describe(
        application_id="Your bot's Application/Client ID (Developer Portal → General Information)",
        description="What does your bot do? Leave blank to use the bot's own Discord description",
        category="Pick the closest fit",
        invite_link="Full https:// invite link (optional — auto-generated from the bot's own install settings if skipped)",
        support_server="Your bot's support server invite (optional)",
        webhook_url="Optional — get an HTTP POST when your listing's status changes",
        encryption_key="Optional — signing key for the webhook. Auto-generated if left blank",
    )
    @app_commands.choices(category=[app_commands.Choice(name=c, value=c) for c in CATEGORIES])
    async def submit(self, interaction: discord.Interaction, application_id: str, category: app_commands.Choice[str],
                      description: str = None, invite_link: str = None, support_server: str = None,
                      webhook_url: str = None, encryption_key: str = None):
        config = await arc.is_enabled(interaction.guild_id)
        if not config:
            await interaction.response.send_message("The archive isn't enabled in this server yet.", ephemeral=True)
            return

        if webhook_url and not webhook_url.startswith("https://"):
            await interaction.response.send_message("webhook_url must be a valid https:// URL.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        app_data = await arc.fetch_application(application_id)
        if not app_data:
            await interaction.followup.send(
                "That Application ID doesn't match a real Discord bot. Double check it in the "
                "Developer Portal under General Information → Application ID.", ephemeral=True
            )
            return

        description = description or app_data.get("description") or "No description provided."
        invite_link = invite_link or app_data.get("invite_link")

        prior_attempts = await arc.get_resubmit_count(interaction.guild_id, app_data["id"])
        if prior_attempts >= arc.RESUBMIT_LIMIT:
            await interaction.followup.send(
                f"This bot has been resubmitted {prior_attempts} times and hit the limit. "
                f"Contact the archive owner directly for a manual review.", ephemeral=True
            )
            return

        risk = await arc.score_submission(app_data, description, interaction.user.id)

        if arc.contains_nsfw(app_data, description):
            # Sexual content always goes to a human — never auto-approved,
            # never auto-denied outright, regardless of the risk score.
            status = "flagged_for_review"
            reason = None
        elif risk >= arc.RISK_DENY_ABOVE:
            status = "denied"
            reason = "Flagged as high-risk (suspicious pattern detected). Contact the archive owner if this is a mistake."
        else:
            # Everything else auto-approves — no more mid-risk holding queue.
            status = "approved"
            reason = None

        listing_id = await arc.create_listing(
            interaction.guild_id, interaction.user.id, app_data, description, category.value,
            invite_link or "", support_server or "", risk, status,
            webhook_url=webhook_url, webhook_secret=encryption_key,
        )
        await arc.send_webhook(listing_id, "status_update")

        if status == "approved":
            await self._post_card(interaction.guild, listing_id)
        listing = await arc.get_listing(listing_id)

        outcome_view = _build_card_view(listing, votes=0, status=status, reason=reason)
        await interaction.followup.send(view=outcome_view, ephemeral=True)

        if webhook_url:
            listing = await arc.get_listing(listing_id)
            try:
                await interaction.user.send(
                    f"🔑 Your webhook signing key for listing #{listing_id} (**{app_data['name']}**):\n"
                    f"||`{listing['webhook_secret']}`||\n"
                    f"Keep this secret — verify incoming POSTs with an HMAC-SHA256 of the raw body "
                    f"using this key, compared against the `X-Archive-Signature` header. "
                    f"Edit it anytime with `/archive webhook listing_id:{listing_id}`."
                )
            except discord.Forbidden:
                pass

        try:
            dm = await interaction.user.create_dm()
            if status == "approved":
                dm_embed = discord.Embed(
                    description=f"✅ Your bot **{app_data['name']}** was approved and is now live in the archive!",
                    color=discord.Color.green(),
                )
                await dm.send(embed=dm_embed)
            elif status == "flagged_for_review":
                dm_embed = discord.Embed(
                    description=f"📋 Your bot **{app_data['name']}** is in review. You'll be notified once it's decided.",
                    color=discord.Color.blurple(),
                )
                await dm.send(embed=dm_embed)
            else:
                dm_embed = discord.Embed(
                    title=f"❌ {app_data['name']} was denied",
                    description=f"Reason: {reason}",
                    color=discord.Color.red(),
                )
                await dm.send(embed=dm_embed, view=DisputeButtonView(listing_id))
        except discord.Forbidden:
            pass

    async def _post_card(self, guild: discord.Guild, listing_id: int):
        config = await arc.is_enabled(guild.id)
        listing = await arc.get_listing(listing_id)
        if not config or not listing:
            return
        votes = await arc.vote_count(listing_id)
        card_view = ArchiveCardView(listing, votes, status="approved")
        for channel_id in config["listing_channel_ids"]:
            channel = guild.get_channel(channel_id)
            if channel:
                msg = await channel.send(view=card_view)
                await arc.set_message_ref(listing_id, channel.id, msg.id)

    async def _refresh_card(self, listing_id: int, votes: int) -> str:
        """Edits the already-posted card in place so the Votes field on the
        embed reflects the real count instead of staying frozen at whatever
        it was when the card was first posted. Returns a short status string
        for diagnostics instead of swallowing the outcome silently."""
        listing = await arc.get_listing(listing_id)
        if not listing:
            msg = f"no listing {listing_id}"
            logger.warning(f"[v0] archive _refresh_card: {msg}")
            return msg
        if not listing.get("channel_id") or not listing.get("message_id"):
            msg = (f"listing {listing_id} has no channel_id/message_id on record "
                   f"(channel_id={listing.get('channel_id')}, message_id={listing.get('message_id')})")
            logger.warning(f"[v0] archive _refresh_card: {msg}")
            return msg
        channel = self.bot.get_channel(listing["channel_id"])
        if not channel:
            try:
                channel = await self.bot.fetch_channel(listing["channel_id"])
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
                msg = f"couldn't fetch channel {listing['channel_id']}: {type(e).__name__}: {e}"
                logger.warning(f"[v0] archive _refresh_card: {msg}")
                return msg
        try:
            message = await channel.fetch_message(listing["message_id"])
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
            msg = f"couldn't fetch message {listing['message_id']}: {type(e).__name__}: {e}"
            logger.warning(f"[v0] archive _refresh_card: {msg}")
            return msg
        card_view = ArchiveCardView(listing, votes, status="approved")
        try:
            # Some older listings were originally posted with a plain embed
            # (before this cog switched to Components V2 cards) and still
            # carry that embeds field on the message. Discord rejects any
            # edit that attaches a Components V2 view while embeds is still
            # set, so clear it explicitly rather than relying on the
            # previous embed state being empty.
            await message.edit(view=card_view, embeds=[])
        except discord.HTTPException as e:
            msg = f"edit failed for message {listing['message_id']}: {type(e).__name__}: {e}"
            logger.warning(f"[v0] archive _refresh_card: {msg}")
            return msg
        return "edit succeeded"

    # ── Owner review queue ───────────────────────────────────────────────

    @group.command(name="pending", description="[Owner] Show submissions flagged for manual review")
    async def pending(self, interaction: discord.Interaction):
        if not _is_owner(interaction.user.id):
            await interaction.response.send_message("Only the archive owner can do that.", ephemeral=True)
            return
        rows = await arc.pending_review_queue(interaction.guild_id)
        if not rows:
            await interaction.response.send_message("Nothing pending review.", ephemeral=True)
            return
        embed = discord.Embed(title="Flagged for review", color=discord.Color.orange())
        for r in rows:
            embed.add_field(name=f"#{r['id']} — {r['bot_name']}", value=f"Risk {r['risk_score']} · <@{r['submitter_id']}>", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @group.command(name="review", description="[Owner] Resolve flagged listings from a dropdown")
    async def review(self, interaction: discord.Interaction):
        if not _is_owner(interaction.user.id):
            await interaction.response.send_message("Only the archive owner can do that.", ephemeral=True)
            return
        rows = await arc.pending_review_queue(interaction.guild_id)
        if not rows:
            await interaction.response.send_message("Nothing pending review.", ephemeral=True)
            return
        view = ArchiveReviewView(rows, interaction.user.id, self._do_resolve)
        embed = render_listing_embed(rows[0], 1, len(rows))
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    async def _do_resolve(self, interaction: discord.Interaction, listing_id: int, approve: bool, reason: str = None):
        """Shared by /archive resolve and the /archive review panel. Pure
        side-effecting work only — no interaction.response/.followup calls
        here, since the two callers are at different points in their own
        response lifecycle (one already deferred, the other about to
        edit_message) and need to send the final confirmation themselves."""
        listing = await arc.get_listing(listing_id)
        if not listing:
            return False
        status = "approved" if approve else "denied"
        await arc.set_status(listing_id, status, reason if not approve else None)
        await arc.send_webhook(listing_id, "status_update")
        if approve:
            await self._post_card(interaction.guild, listing_id)
        try:
            user = await interaction.client.fetch_user(listing["submitter_id"])
            if approve:
                embed = discord.Embed(
                    description=f"✅ Your bot **{listing['bot_name']}** was approved and is now live!",
                    color=discord.Color.green(),
                )
            else:
                embed = discord.Embed(
                    title=f"❌ {listing['bot_name']} was denied",
                    description=f"Reason: {reason or 'Not specified.'}",
                    color=discord.Color.red(),
                )
            await user.send(embed=embed)
        except discord.HTTPException:
            pass
        return True

    @group.command(name="resolve", description="[Owner] Approve or deny a flagged/disputed listing")
    @app_commands.describe(listing_id="Listing ID from /archive pending", approve="True to approve, False to deny", reason="Reason if denying")
    async def resolve(self, interaction: discord.Interaction, listing_id: int, approve: bool, reason: str = None):
        if not _is_owner(interaction.user.id):
            await interaction.response.send_message("Only the archive owner can do that.", ephemeral=True)
            return
        listing = await arc.get_listing(listing_id)
        if not listing:
            await interaction.response.send_message("No such listing.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        ok = await self._do_resolve(interaction, listing_id, approve, reason)
        if not ok:
            await interaction.followup.send("No such listing.", ephemeral=True)
            return
        status = "approved" if approve else "denied"
        await interaction.followup.send(f"✅ Listing #{listing_id} marked **{status}**.", ephemeral=True)

    @group.command(name="bypass", description="[Owner] Force-approve a bot regardless of risk score")
    @app_commands.describe(
        application_id="Application ID to force-approve",
        category="Pick the closest fit (defaults to Other if skipped)",
        description="Optional — leave blank to use the bot's own Discord description",
    )
    @app_commands.choices(category=[app_commands.Choice(name=c, value=c) for c in CATEGORIES])
    async def bypass(self, interaction: discord.Interaction, application_id: str,
                      category: app_commands.Choice[str] = None, description: str = None):
        if not _is_owner(interaction.user.id):
            await interaction.response.send_message("Only the archive owner can do that.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        app_data = await arc.fetch_application(application_id)
        if not app_data:
            await interaction.followup.send("That Application ID doesn't resolve.", ephemeral=True)
            return
        final_description = description or app_data.get("description") or "No description provided."
        final_category = category.value if category else "Other"
        listing_id = await arc.create_listing(
            interaction.guild_id, interaction.user.id, app_data, final_description, final_category,
            app_data.get("invite_link", ""), "", 0, "approved"
        )
        await self._post_card(interaction.guild, listing_id)
        await arc.send_webhook(listing_id, "status_update")
        await interaction.followup.send(f"✅ Force-approved **{app_data['name']}**.", ephemeral=True)

    @group.command(name="webhook", description="Add, edit, or remove the webhook on your own listing")
    @app_commands.describe(
        listing_id="Your listing's ID",
        webhook_url="New https:// URL, or leave blank to clear it",
        encryption_key="Optional — set a specific signing key instead of keeping/generating one",
    )
    async def webhook_cmd(self, interaction: discord.Interaction, listing_id: int,
                           webhook_url: str = None, encryption_key: str = None):
        if webhook_url and not webhook_url.startswith("https://"):
            await interaction.response.send_message("webhook_url must be a valid https:// URL.", ephemeral=True)
            return
        ok = await arc.set_webhook(listing_id, interaction.user.id, webhook_url, encryption_key)
        if not ok:
            await interaction.response.send_message("You don't own a listing with that id.", ephemeral=True)
            return
        if not webhook_url:
            await interaction.response.send_message(f"Webhook cleared for listing #{listing_id}.", ephemeral=True)
            return
        listing = await arc.get_listing(listing_id)
        await interaction.response.send_message(
            f"✅ Webhook set for listing #{listing_id}: {webhook_url}\n"
            f"Signing key: ||`{listing['webhook_secret']}`||",
            ephemeral=True,
        )

    @group.command(name="refresh", description="[Owner] Force the posted card to re-sync with the current vote count")
    @app_commands.describe(listing_id="Listing ID to refresh")
    async def refresh_cmd(self, interaction: discord.Interaction, listing_id: int):
        if not _is_owner(interaction.user.id):
            await interaction.response.send_message("Only the archive owner can do that.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        listing = await arc.get_listing(listing_id)
        if not listing:
            await interaction.followup.send("No such listing.", ephemeral=True)
            return
        count = await arc.vote_count(listing_id)
        before = (listing.get("channel_id"), listing.get("message_id"))
        raw_votes = await arc.debug_votes_for_listing(listing_id)
        refresh_result = await self._refresh_card(listing_id, count)
        vote_dump = "\n".join(f"<@{v['voter_id']}> at {v['voted_at']}" for v in raw_votes) or "(no rows in archive_votes for this listing_id)"
        jump_link = (
            f"https://discord.com/channels/{interaction.guild_id}/{before[0]}/{before[1]}"
            if before[0] and before[1] else "n/a"
        )
        await interaction.followup.send(
            f"Attempted refresh for #{listing_id} — votes={count}, "
            f"channel_id={before[0]}, message_id={before[1]}.\n"
            f"Refresh result: **{refresh_result}**\n"
            f"This is the ONLY message the bot considers the card for listing #{listing_id}: {jump_link}\n"
            f"(If you have other 'Listing #1' cards posted elsewhere/earlier, those are stale duplicates — "
            f"delete them.)\n"
            f"Raw archive_votes rows for listing_id={listing_id}:\n{vote_dump}",
            ephemeral=True,
        )

    # ── Trending ─────────────────────────────────────────────────────────

    @group.command(name="trending", description="See the current top bots in the archive")
    async def trending_cmd(self, interaction: discord.Interaction):
        rows = await arc.trending(interaction.guild_id)
        if not rows:
            await interaction.response.send_message("No approved listings yet.", ephemeral=True)
            return
        lines = [f"**{i+1}.** {r['bot_name']} — {r['score']:.1f} pts" for i, r in enumerate(rows)]
        embed = discord.Embed(title="🌟 Trending Bots", description="\n".join(lines), color=discord.Color.gold())
        await interaction.response.send_message(embed=embed)

    # ── Boost ────────────────────────────────────────────────────────────

    @group.command(name="boost", description="Feature your approved listing at the top of the archive")
    @app_commands.describe(listing_id="Your listing's ID", tier="Boost tier")
    @app_commands.choices(tier=[app_commands.Choice(name=f"{k} (${v['usd']} / {v['hours']}h)", value=k) for k, v in arc.BOOST_TIERS.items()])
    async def boost(self, interaction: discord.Interaction, listing_id: int, tier: app_commands.Choice[str]):
        listing = await arc.get_listing(listing_id)
        if not listing or listing["submitter_id"] != interaction.user.id:
            await interaction.response.send_message("You don't own a listing with that id.", ephemeral=True)
            return
        if listing["status"] != "approved":
            await interaction.response.send_message("Only approved listings can be boosted.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        tier_info = arc.BOOST_TIERS[tier.value]
        paystack = PaystackPayment()
        result = paystack.initialize_payment(
            email=f"{interaction.user.id}@discord.archive",
            amount_minor_units=tier_info["usd"] * 100,  # USD cents
            user_id=interaction.user.id,
            bot_name=listing["bot_name"],
            payment_type="archive_boost",
            extra_metadata={"listing_id": listing_id, "tier": tier.value},
            currency="USD",
        )
        link = result.get("authorization_url") if result and result.get("status") == "success" else None
        if not link:
            await interaction.followup.send("Couldn't start checkout right now — try again shortly.", ephemeral=True)
            return
        await interaction.followup.send(
            f"Boost **{listing['bot_name']}** ({tier.value}, ${tier_info['usd']}) here: {link}\n"
            f"It activates automatically once payment is confirmed.",
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(ArchiveCog(bot))
