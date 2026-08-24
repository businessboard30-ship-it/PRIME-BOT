"""
Ads & services marketplace — Discord equivalent of
handlers/ads_marketplace_handler.py, using modules/ads_marketplace.py
as-is (plain Postgres CRUD, no Telegram dependency).

Two independent halves of the same module, both exposed here:
  - /ad submit|status — owner-approved sponsored ads. Approval uses
    DISCORD_CLONE_ADMIN_IDS (config.py) rather than a per-guild permission,
    same as clone_admin.py's approval gate — this is a bot-owner decision
    (who gets to advertise across the bot), not a server admin's call, and
    ad_submissions has no guild_id column to scope it per-server anyway.
  - /marketplace list|browse|mine — user-to-user services listings, no
    approval step (matches the original: list_service goes active
    immediately).

NOT PORTED: any payment collection for either. The old flow just recorded
a budget_usd / price_usd figure — no evidence Paystack was actually wired
to ad_submissions or services_listings on the Telegram side either (only
premium groups and the AI/download paywall touched payments.py). So there's
no monetization logic to lose here; adding real payment collection is a
new feature, not a port, and needs its own design (who gets paid, when,
how disputes/refunds work) before it's built.

/adboard (posting approved ads into a channel) is intentionally NOT a
live-gateway command — same reasoning as automation.py's /announce: this
bot process isn't guaranteed to be running when you'd want an ad shown.
For now /ad status lets a submitter check their own ad, and approved ads
are just queryable — wire actual channel posting into
api/cron_discord_announcements.py's pattern once you decide the cadence.
"""

import logging

import discord
from discord import app_commands
from discord.ext import commands

from config import DISCORD_CLONE_ADMIN_IDS
from modules.ads_marketplace import (
    submit_ad, get_pending_ads, get_ad, approve_ad, reject_ad, get_active_ads,
    list_service, get_marketplace_listings, get_my_listings,
)
from discord_bot.cogs._views_shared import ActionButton, NavCardView, refresh_button

logger = logging.getLogger(__name__)


def _is_ads_admin(user_id: int) -> bool:
    return user_id in DISCORD_CLONE_ADMIN_IDS


class AdsMarketplaceCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    ad = app_commands.Group(name="ad", description="Sponsored ads (owner-approved)")

    @ad.command(name="submit", description="Submit an ad for approval")
    @app_commands.describe(
        company_name="Your company/brand name", title="Ad headline", description="Ad body text",
        target_url="Where the ad should link", budget_usd="Proposed budget in USD",
    )
    async def ad_submit(
        self, interaction: discord.Interaction, company_name: str, title: str,
        description: str, target_url: str, budget_usd: float,
    ):
        if budget_usd < 0:
            await interaction.response.send_message("Budget can't be negative.", ephemeral=True)
            return
        ad_id = await submit_ad(interaction.user.id, company_name.strip(), title.strip(), description.strip(), target_url.strip(), budget_usd)
        if not ad_id:
            await interaction.response.send_message("❌ Couldn't submit that ad. Try again.", ephemeral=True)
            return
        await interaction.response.send_message(
            f"✅ Ad #{ad_id} submitted for review. Use `/ad status ad_id:{ad_id}` to check on it.", ephemeral=True
        )

    @ad.command(name="status", description="Check the status of an ad you submitted")
    @app_commands.describe(ad_id="The ad's id (given to you when you submitted it)")
    async def ad_status(self, interaction: discord.Interaction, ad_id: int):
        ad = await get_ad(ad_id)
        if not ad or ad["user_id"] != interaction.user.id:
            await interaction.response.send_message("No ad with that id belongs to you.", ephemeral=True)
            return
        lines = [
            f"**Status:** {ad['status'].title()}",
            f"**Company:** {ad['company_name']}",
            f"**Budget:** ${ad['budget_usd']}",
        ]
        if ad.get("rejection_reason"):
            lines.append(f"**Rejection reason:** {ad['rejection_reason']}")
        buttons = [refresh_button(self, "ad_status", args=(ad_id,))]
        card = NavCardView(f"Ad #{ad_id}: {ad['ad_title']}", lines, discord.Color.blurple(), buttons)
        await interaction.response.send_message(view=card, ephemeral=True)

    @ad.command(name="pending", description="[Owner] List ads awaiting approval")
    async def ad_pending(self, interaction: discord.Interaction):
        if not _is_ads_admin(interaction.user.id):
            await interaction.response.send_message("You're not authorized to review ads.", ephemeral=True)
            return
        pending = await get_pending_ads()
        if not pending:
            await interaction.response.send_message("No ads pending review.", ephemeral=True)
            return
        lines = [f"• **#{a['id']} — {a['company_name']}** — {a['ad_title']} · ${a['budget_usd']}" for a in pending]
        buttons = [refresh_button(self, "ad_pending")]
        card = NavCardView("📋 Ads pending review", lines, discord.Color.orange(), buttons)
        await interaction.response.send_message(view=card, ephemeral=True)

    @ad.command(name="approve", description="[Owner] Approve a pending ad")
    @app_commands.describe(ad_id="The ad's id")
    async def ad_approve(self, interaction: discord.Interaction, ad_id: int):
        if not _is_ads_admin(interaction.user.id):
            await interaction.response.send_message("You're not authorized to approve ads.", ephemeral=True)
            return
        ok = await approve_ad(ad_id)
        await interaction.response.send_message("✅ Approved." if ok else "❌ Not found or already reviewed.", ephemeral=True)

    @ad.command(name="reject", description="[Owner] Reject a pending ad")
    @app_commands.describe(ad_id="The ad's id", reason="Why it's being rejected")
    async def ad_reject(self, interaction: discord.Interaction, ad_id: int, reason: str):
        if not _is_ads_admin(interaction.user.id):
            await interaction.response.send_message("You're not authorized to reject ads.", ephemeral=True)
            return
        ok = await reject_ad(ad_id, reason.strip()[:200])
        await interaction.response.send_message("✅ Rejected." if ok else "❌ Not found or already reviewed.", ephemeral=True)

    @ad.command(name="active", description="Show currently approved ads")
    async def ad_active(self, interaction: discord.Interaction):
        ads = await get_active_ads()
        if not ads:
            await interaction.response.send_message("No active ads right now.", ephemeral=True)
            return
        lines = [f"**{a['company_name']} — {a['ad_title']}**\n{a['ad_description']}\n{a['target_url']}" for a in ads]
        buttons = [refresh_button(self, "ad_active")]
        card = NavCardView("📢 Sponsored", lines, discord.Color.orange(), buttons)
        await interaction.response.send_message(view=card)

    marketplace = app_commands.Group(name="marketplace", description="Buy/sell services with other members")

    @marketplace.command(name="list", description="List a service you're offering")
    @app_commands.describe(name="Short service name", title="Listing headline", description="Details", price_usd="Price in USD", category="e.g. design, coding, writing")
    async def marketplace_list(
        self, interaction: discord.Interaction, name: str, title: str,
        description: str, price_usd: float, category: str = "general",
    ):
        if price_usd < 0:
            await interaction.response.send_message("Price can't be negative.", ephemeral=True)
            return
        listing_id = await list_service(interaction.user.id, name.strip(), title.strip(), description.strip(), price_usd, category.strip() or "general")
        if not listing_id:
            await interaction.response.send_message("❌ Couldn't create that listing. Try again.", ephemeral=True)
            return
        await interaction.response.send_message(f"✅ Listed! Your listing id is `{listing_id}`.", ephemeral=True)

    @marketplace.command(name="browse", description="Browse active service listings")
    async def marketplace_browse(self, interaction: discord.Interaction):
        listings = await get_marketplace_listings()
        if not listings:
            await interaction.response.send_message("No active listings right now.", ephemeral=True)
            return
        lines = [f"**{l['service_title']} — ${l['price_usd']}**\n{l['description'][:150]}\n_{l['category']}_" for l in listings[:10]]
        buttons = [
            refresh_button(self, "marketplace_browse"),
            ActionButton("My Listings", discord.ButtonStyle.secondary, self, "marketplace_mine", emoji="📋"),
        ]
        card = NavCardView("🛒 Services Marketplace", lines, discord.Color.green(), buttons)
        await interaction.response.send_message(view=card, ephemeral=True)

    @marketplace.command(name="mine", description="Show your own listings")
    async def marketplace_mine(self, interaction: discord.Interaction):
        listings = await get_my_listings(interaction.user.id)
        if not listings:
            await interaction.response.send_message("You have no listings yet.", ephemeral=True)
            return
        lines = [f"• **{l['service_title']} — ${l['price_usd']}** — `{l['id']}` · {l['status']}" for l in listings]
        buttons = [
            refresh_button(self, "marketplace_mine"),
            ActionButton("Browse All", discord.ButtonStyle.secondary, self, "marketplace_browse", emoji="🛒"),
        ]
        card = NavCardView("Your listings", lines, discord.Color.green(), buttons)
        await interaction.response.send_message(view=card, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(AdsMarketplaceCog(bot))
