"""
Bot-owner admin tools — Discord equivalent of the portable pieces of
handlers/admin_panel.py + handlers/admin_tools.py + handlers/admin_config.py.

Gated by DISCORD_CLONE_ADMIN_IDS (config.py), same convention as
clone_admin.py and ads_marketplace.py — this is the bot OWNER's toolkit,
not a per-guild admin's (that's what Manage Server-gated commands like
/createpremium and /automod already cover).

THREE THINGS FROM THE OLD ADMIN SURFACE DELIBERATELY NOT PORTED HERE, with
reasons rather than just dropped silently:

1. handlers/admin_remote.py (pick-a-group-from-a-list, run-a-command-there
   remotely). This existed because Telegram admin commands only work inside
   the chat they're typed in, so reaching a group you're not currently in
   needed a remote-dispatch UI. Discord slash commands have the same
   "runs where you type it" constraint, so the same gap exists in
   principle — but the fix isn't a straight port: it'd mean building a
   picker over every guild the bot is in and executing arbitrary other
   slash commands against a synthesized Interaction, which is a much bigger
   and riskier piece of infra than anything else in this pass. If you
   actually hit this need often, it's worth its own design pass rather
   than bolting on here.

2. handlers/admin_tools.py's cmd_addsponsor / handlers/admin_tools.py's
   cmd_registerme. addsponsor queued into the Telegram-only autopost cron
   table — superseded by /ad submit + /ad approve in ads_marketplace.py,
   which already reaches api/cron_discord_announcements.py's pattern.
   registerme doesn't have a Discord equivalent to port: a Telegram bot
   needs each group's admin to opt in because it can't otherwise enumerate
   "chats it's in" the way this needs; a discord.py bot already has
   self.guilds live from the gateway connection, so there's nothing to
   register — /admin stats below reads guild count directly.

3. admin_config.py's botstore/superbot GHS-priced field editor. Editing
   Telegram-specific commerce config (subscription price in GHS, referral
   coin rewards) isn't a Discord concern — Discord's equivalent knobs
   already live in /createpremium (per-guild price) and DISCORD_CLONE_FEE_GHS
   (config.py, clone_admin.py). Nothing left here to port.

/admin submissions reuses the SAME submissions table Telegram's
handler/submit.py + admin_panel.py used (global, not per-platform) — so
approving one here approves it everywhere this data is read from. No
Discord-side /submit command exists yet to create new ones (only
handlers/submit.py does, on the Telegram side) — this cog only reviews
what's already pending. Say the word if you want a Discord /submit too.

Same collision caveat as discover.py's categories: `users`/`submissions`
are keyed on a bare user_id int with no platform column.
"""

import csv
import io
import logging
from datetime import date

import discord
from discord import app_commands
from discord.ext import commands

from database import db, get_pool
from modules.superbot_adapter import get_global_stats
from config import DISCORD_CLONE_ADMIN_IDS
from discord_clone_service import build_invite_url

logger = logging.getLogger(__name__)


def _is_bot_admin(user_id: int) -> bool:
    return user_id in DISCORD_CLONE_ADMIN_IDS


async def _deny(interaction: discord.Interaction):
    msg = "You're not authorized to use admin tools."
    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)


class SubmissionReviewView(discord.ui.View):
    """One submission at a time, same flow as the old admin_panel.py
    (approve / reject-with-reason / skip), advancing through the pending
    queue rather than requiring the admin to re-run the command each time."""

    def __init__(self, cog: "AdminCog", submissions: list, index: int = 0):
        super().__init__(timeout=300)
        self.cog = cog
        self.submissions = submissions
        self.index = index

    @property
    def current(self) -> dict:
        return self.submissions[self.index]

    def embed(self) -> discord.Embed:
        s = self.current
        embed = discord.Embed(
            title=f"📤 Submission Review ({self.index + 1}/{len(self.submissions)})",
            description=s.get("synopsis") or "No description",
            color=discord.Color.orange(),
        )
        embed.add_field(name="Title", value=s.get("anime_name", "Unknown"))
        embed.add_field(name="Episodes", value=str(s.get("episodes", "?")))
        embed.add_field(name="Genres", value=s.get("genres") or "N/A", inline=False)
        embed.add_field(name="From user", value=str(s.get("user_id")))
        if s.get("image_url"):
            embed.set_thumbnail(url=s["image_url"])
        return embed

    async def _advance(self, interaction: discord.Interaction):
        self.index += 1
        if self.index >= len(self.submissions):
            await interaction.response.edit_message(
                content="✅ No more pending submissions.", embed=None, view=None
            )
            return
        await interaction.response.edit_message(embed=self.embed(), view=self)

    @discord.ui.button(label="✅ Approve", style=discord.ButtonStyle.success)
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        await db.approve_submission(self.current["submission_id"])
        await self._advance(interaction)

    @discord.ui.button(label="❌ Reject", style=discord.ButtonStyle.danger)
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(RejectReasonModal(self, interaction.message))

    @discord.ui.button(label="⏭️ Skip", style=discord.ButtonStyle.secondary)
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._advance(interaction)


class RejectReasonModal(discord.ui.Modal, title="Reject Submission"):
    reason = discord.ui.TextInput(label="Reason (shown in logs, not to the user)", required=False, max_length=200)

    def __init__(self, view: SubmissionReviewView, message: discord.Message):
        super().__init__()
        self.view = view
        self.message = message

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        await db.reject_submission(self.view.current["submission_id"], self.reason.value or "No reason given")
        self.view.index += 1
        if self.view.index >= len(self.view.submissions):
            await interaction.edit_original_response(content="✅ No more pending submissions.", embed=None, view=None)
            return
        await interaction.edit_original_response(embed=self.view.embed(), view=self.view)


class AdminCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    admin = app_commands.Group(name="admin", description="[Owner] Bot administration")

    @app_commands.command(name="invite", description="Get this bot's invite link, so you can add it to another server")
    async def invite(self, interaction: discord.Interaction):
        # self.bot.application_id is populated by discord.py from the
        # gateway IDENTIFY response — always the CURRENT running bot's own
        # application, so this is correct for the main bot AND for every
        # clone process (each clone runs bot.py with its own token, so its
        # application_id is naturally its own, never hardcoded anywhere).
        app_id = self.bot.application_id
        if app_id is None:
            await interaction.response.send_message(
                "Couldn't determine this bot's application ID yet — try again in a moment.", ephemeral=True
            )
            return
        url = build_invite_url(app_id)
        embed = discord.Embed(
            title="➕ Add me to your server",
            description=f"[Click here to invite]({url})",
            color=discord.Color.blurple(),
        )
        embed.set_footer(text="Requests moderation, roles, and messaging permissions this bot's features need.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @admin.command(name="stats", description="[Owner] Bot-wide analytics")
    async def stats(self, interaction: discord.Interaction):
        if not _is_bot_admin(interaction.user.id):
            await _deny(interaction)
            return
        await interaction.response.defer(ephemeral=True)

        pool = await get_pool()
        async with pool.acquire() as conn:
            total_ai_chat = await conn.fetchval("SELECT COUNT(*) FROM ai_chat_usage")
            total_ai_image = await conn.fetchval("SELECT COUNT(*) FROM ai_image_usage")
            pending_submissions = await conn.fetchval("SELECT COUNT(*) FROM submissions WHERE status = 'pending'")
            pending_ads = await conn.fetchval("SELECT COUNT(*) FROM ad_submissions WHERE status = 'pending'")

        global_stats = await get_global_stats()

        embed = discord.Embed(title="📈 Bot Analytics", color=discord.Color.blurple())
        embed.add_field(name="Servers", value=str(len(self.bot.guilds)), inline=True)
        embed.add_field(name="Tiered users", value=str(global_stats["total_users"]), inline=True)
        embed.add_field(
            name="Tier split",
            value=" / ".join(f"{k}: {v}" for k, v in global_stats["tier_distribution"].items()) or "n/a",
            inline=False,
        )
        embed.add_field(name="AI chat uses (all-time)", value=str(total_ai_chat), inline=True)
        embed.add_field(name="AI images (all-time)", value=str(total_ai_image), inline=True)
        embed.add_field(name="Pending anime submissions", value=str(pending_submissions), inline=True)
        embed.add_field(name="Pending ads", value=str(pending_ads), inline=True)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @admin.command(name="guilds", description="[Owner] List servers this bot is in, with join dates")
    async def guilds(self, interaction: discord.Interaction):
        if not _is_bot_admin(interaction.user.id):
            await _deny(interaction)
            return
        await interaction.response.defer(ephemeral=True)

        clone_id = getattr(self.bot, "clone_id", None)
        rows = await db.list_discord_guilds(clone_id=clone_id)
        if not rows:
            await interaction.followup.send("Not in any servers yet.", ephemeral=True)
            return

        lines = []
        for r in rows:
            line = (
                f"**{r['guild_name'] or 'Unknown'}** (`{r['guild_id']}`) — "
                f"{r['member_count'] or '?'} members — joined <t:{int(r['joined_at'].timestamp())}:R>"
            )
            if r['invite_url']:
                line += f" — [invite]({r['invite_url']})"
            else:
                line += " — no invite on file"
            lines.append(line)
        embed = discord.Embed(
            title=f"🏠 Servers ({len(rows)})",
            description="\n".join(lines)[:4000],
            color=discord.Color.blurple(),
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @admin.command(name="submissions", description="[Owner] Review pending anime submissions")
    async def submissions(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        if not _is_bot_admin(interaction.user.id):
            await _deny(interaction)
            return
        pending = await db.get_pending_submissions()
        if not pending:
            await interaction.followup.send("✅ No pending submissions to review.", ephemeral=True)
            return
        view = SubmissionReviewView(self, pending, 0)
        await interaction.followup.send(embed=view.embed(), view=view, ephemeral=True)

    @admin.command(name="exportusers", description="[Owner] Export all user records as CSV")
    async def exportusers(self, interaction: discord.Interaction):
        if not _is_bot_admin(interaction.user.id):
            await _deny(interaction)
            return
        await interaction.response.defer(ephemeral=True)

        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT user_id, username, first_name, tier, subscription_status, joined_date FROM users"
            )

        buf = io.StringIO()
        fieldnames = ["user_id", "username", "first_name", "tier", "subscription_status", "joined_date"]
        writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for r in rows:
            writer.writerow(dict(r))

        data = io.BytesIO(buf.getvalue().encode("utf-8"))
        filename = f"users_export_{date.today().isoformat()}.csv"
        await interaction.followup.send(
            content=f"📁 {len(rows)} user records exported.",
            file=discord.File(data, filename=filename),
            ephemeral=True,
        )

    @admin.command(name="envcheck", description="[Owner] Sanity-check required environment variables")
    async def envcheck(self, interaction: discord.Interaction):
        if not _is_bot_admin(interaction.user.id):
            await _deny(interaction)
            return
        import os
        checks = {
            "DISCORD_BOT_TOKEN": bool(os.getenv("DISCORD_BOT_TOKEN")),
            "DATABASE_URL": bool(os.getenv("DATABASE_URL")),
            "GROQ_API_KEY": bool(os.getenv("GROQ_API_KEY")),
            "FAL_API_KEY or OPENAI_API_KEY": bool(os.getenv("FAL_API_KEY") or os.getenv("OPENAI_API_KEY")),
            "ENCRYPTION_KEY": bool(os.getenv("ENCRYPTION_KEY")),
        }
        embed = discord.Embed(title="Environment check", color=discord.Color.blurple())
        for name, ok in checks.items():
            embed.add_field(name=name, value="✅ set" if ok else "❌ missing", inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── Revenue / subscriber / commission / clone tooling ────────────────
    # Discord equivalent of admin_panel.py's show_revenue_dashboard /
    # show_subscribers_list / show_commissions_tracking / show_bot_analytics
    # / show_clone_management. Built off REAL data (payment_logs via the
    # new db.get_revenue_by_type) rather than porting the Telegram
    # dashboard's hardcoded/placeholder numbers verbatim — see that file's
    # comments ("Commission data from Stripe payments", "Detailed analytics
    # coming soon!") for why those weren't worth porting as-is.

    # Every payment_type any Discord cog currently logs via db.log_payment —
    # add a new one here whenever a new paywall is built so it shows up in
    # /admin revenue automatically.
    _DISCORD_PAYMENT_TYPES = {
        "discord_clone": "Clone registrations",
        "discord_clone_monetization": "Clone monetization activations",
        "media_connect_subscription": "Media Connect subscriptions",
        "image_search_unlock": "Image search unlocks",
        "image_search_yandex": "Yandex direct-search subscriptions",
        "premium_group_join": "Premium group joins",
        "ai_store_topup": "AI Store credit top-ups",
        "ai_store_boost": "AI Store listing boosts",
    }

    @admin.command(name="revenue", description="[Owner] Revenue dashboard across all paid features")
    async def revenue(self, interaction: discord.Interaction):
        if not _is_bot_admin(interaction.user.id):
            await _deny(interaction)
            return
        await interaction.response.defer(ephemeral=True)

        rows = await db.get_revenue_by_type(list(self._DISCORD_PAYMENT_TYPES.keys()))
        by_type = {r["payment_type"]: r for r in rows}

        total = sum(r["completed_total"] for r in rows)
        total_count = sum(r["completed_count"] for r in rows)
        total_pending = sum(r["pending_count"] for r in rows)

        embed = discord.Embed(title="💰 Revenue Dashboard", color=discord.Color.green())
        embed.add_field(name="Total completed (GHS)", value=f"{total:g}", inline=True)
        embed.add_field(name="Completed payments", value=str(total_count), inline=True)
        embed.add_field(name="Pending checkouts", value=str(total_pending), inline=True)

        breakdown_lines = []
        for ptype, label in self._DISCORD_PAYMENT_TYPES.items():
            r = by_type.get(ptype)
            if not r or (r["completed_count"] == 0 and r["pending_count"] == 0):
                continue
            breakdown_lines.append(
                f"• **{label}** — GHS {r['completed_total']:g} ({r['completed_count']} paid"
                + (f", {r['pending_count']} pending" if r["pending_count"] else "") + ")"
            )
        embed.add_field(
            name="By feature",
            value="\n".join(breakdown_lines) if breakdown_lines else "No payments logged yet.",
            inline=False,
        )
        embed.set_footer(text="Excludes commission splits — see /admin commissions for that breakdown.")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @admin.command(name="subscribers", description="[Owner] List active Media Connect + premium-group subscribers")
    async def subscribers(self, interaction: discord.Interaction):
        if not _is_bot_admin(interaction.user.id):
            await _deny(interaction)
            return
        await interaction.response.defer(ephemeral=True)

        pool = await get_pool()
        async with pool.acquire() as conn:
            mc_rows = await conn.fetch(
                "SELECT user_id, expires_at FROM media_connect_subscriptions "
                "WHERE status = 'active' AND expires_at > NOW() ORDER BY expires_at ASC LIMIT 10"
            )
            mc_total = await conn.fetchval(
                "SELECT COUNT(*) FROM media_connect_subscriptions WHERE status = 'active' AND expires_at > NOW()"
            )
            pg_total = await conn.fetchval(
                "SELECT COUNT(*) FROM payment_logs WHERE payment_type = 'premium_group_join' AND status = 'completed'"
            )

        embed = discord.Embed(title="👥 Active Subscribers", color=discord.Color.blurple())
        embed.add_field(name="Media Connect (active)", value=str(mc_total or 0), inline=True)
        embed.add_field(name="Premium group joins (all-time)", value=str(pg_total or 0), inline=True)
        if mc_rows:
            lines = "\n".join(f"• <@{r['user_id']}> — expires {r['expires_at'].strftime('%Y-%m-%d')}" for r in mc_rows)
            embed.add_field(name="Next 10 Media Connect renewals", value=lines, inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @admin.command(name="commissions", description="[Owner] Commission split for monetized clone bots")
    async def commissions(self, interaction: discord.Interaction):
        if not _is_bot_admin(interaction.user.id):
            await _deny(interaction)
            return
        await interaction.response.defer(ephemeral=True)

        pool = await get_pool()
        async with pool.acquire() as conn:
            active_monetized = await conn.fetchval(
                "SELECT COUNT(*) FROM discord_clone_monetization_subscriptions "
                "WHERE status = 'active' AND expires_at > NOW()"
            )
            main_routed = await conn.fetchval(
                """
                SELECT COUNT(*) FROM discord_cloned_bots dc
                LEFT JOIN discord_clone_monetization_subscriptions ms ON ms.clone_id = dc.clone_id
                WHERE dc.status = 'active'
                  AND (ms.clone_id IS NULL OR ms.status != 'active' OR ms.expires_at <= NOW())
                """
            )
            own_key_routed = await conn.fetchval(
                "SELECT COUNT(*) FROM discord_cloned_bots WHERE status = 'active' "
                "AND custom_data->>'payment_provider' = 'paystack'"
            )

        embed = discord.Embed(title="💳 Commission Tracking", color=discord.Color.gold())
        embed.description = (
            "Clones **not** monetized (or that haven't connected their own key) route payments through "
            "the main bot's Paystack account — those are the ones the main bot actually collects a cut of. "
            "Clones with their own connected key keep 100% themselves."
        )
        embed.add_field(name="Active monetization subscriptions", value=str(active_monetized or 0), inline=True)
        embed.add_field(name="Clones routed through main account", value=str(main_routed or 0), inline=True)
        embed.add_field(name="Clones on their own Paystack key", value=str(own_key_routed or 0), inline=True)
        embed.set_footer(text="Use /admin revenue for the actual GHS totals moving through the main account.")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @admin.command(name="clones", description="[Owner] Manage active Discord bot clones")
    async def clones(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        if not _is_bot_admin(interaction.user.id):
            await _deny(interaction)
            return
        clones = await db.list_active_discord_clones()
        if not clones:
            await interaction.followup.send("No active Discord clones right now.", ephemeral=True)
            return
        view = CloneManagementView(clones)
        await interaction.followup.send(embed=view.embed(), view=view, ephemeral=True)


class CloneManagementView(discord.ui.View):
    """Discord equivalent of admin_panel.py's show_clone_management +
    handle_deactivate_clone — one Deactivate button per clone, refreshing
    in place after each action instead of requiring the admin to re-run
    /admin clones."""

    def __init__(self, clones: list):
        super().__init__(timeout=300)
        self.clones = clones
        for c in clones[:20]:  # Discord caps a view at 25 components; leave room for future rows
            self.add_item(DeactivateCloneButton(c["clone_id"], c.get("bot_username")))

    def embed(self) -> discord.Embed:
        embed = discord.Embed(title=f"🤖 Manage Clones ({len(self.clones)} active)", color=discord.Color.blurple())
        lines = []
        for c in self.clones:
            username = f"@{c['bot_username']}" if c.get("bot_username") else "(no username on file)"
            lines.append(f"`#{c['clone_id']}` **{username}** — owner <@{c['owner_id']}>")
        embed.description = "\n".join(lines)
        return embed


class DeactivateCloneButton(discord.ui.Button):
    def __init__(self, clone_id: int, bot_username: str):
        super().__init__(label=f"🛑 Deactivate #{clone_id}", style=discord.ButtonStyle.danger)
        self.clone_id = clone_id

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        if not _is_bot_admin(interaction.user.id):
            await _deny(interaction)
            return
        await db.set_discord_clone_status(self.clone_id, "inactive")
        view: CloneManagementView = self.view
        view.clones = [c for c in view.clones if c["clone_id"] != self.clone_id]
        view.clear_items()
        for c in view.clones[:20]:
            view.add_item(DeactivateCloneButton(c["clone_id"], c.get("bot_username")))
        if view.clones:
            await interaction.edit_original_response(embed=view.embed(), view=view)
        else:
            await interaction.edit_original_response(content="No active clones left.", embed=None, view=None)


async def setup(bot: commands.Bot):
    await bot.add_cog(AdminCog(bot))
