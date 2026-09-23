"""
Ads & services marketplace — Discord equivalent of
handlers/ads_marketplace_handler.py, using modules/ads_marketplace.py
as-is (plain Postgres CRUD, no Telegram dependency).

Two independent halves of the same module, both exposed here:
  - /ad submit|status|toggle — owner-approved sponsored ads. Approval uses
    DISCORD_CLONE_ADMIN_IDS (config.py) rather than a per-guild permission,
    same as clone_admin.py's approval gate — this is a bot-owner decision
    (who gets to advertise across the bot), not a server admin's call, and
    ad_submissions has no guild_id column to scope it per-server anyway.
  - /marketplace list|browse|mine — user-to-user services listings, no
    approval step (matches the original: list_service goes active
    immediately).

/ad submit now collects payment: after the ad is recorded 'pending', the
submitter gets a Gumroad "ad_placement" checkout button (see
gumroad_payments.start_gumroad_payment, payment_type "ad_placement" in
config.GUMROAD_PRODUCT_LINKS/AD_PLACEMENT_FEE_USD). The ad's id is encoded
as a "_ad<id>" suffix on the payment reference so payments_manual.py's
_unlock_ad_placement can auto-approve() it once the webhook confirms —
no manual /ad approve needed unless payment fails or you're comping one.
services_listings (/marketplace) still has no payment collection — those
are user-to-user deals, buyer and seller settle directly.

/adboard (posting approved ads into a channel) is intentionally NOT a
live-gateway command — same reasoning as automation.py's /announce: this
bot process isn't guaranteed to be running when you'd want an ad shown.
For now /ad status lets a submitter check their own ad, and approved ads
are just queryable — wire actual channel posting into
api/cron_discord_announcements.py's pattern once you decide the cadence.
"""

import logging
import secrets

import discord
from discord import app_commands
from discord.ext import commands

from config import DISCORD_CLONE_ADMIN_IDS, AD_PLACEMENT_FEE_USD
from modules.ads_marketplace import (
    submit_ad, set_ad_image, update_ad_fields, get_pending_ads, get_ad, approve_ad, reject_ad, get_active_ads,
    deactivate_ad, reactivate_ad, search_ads,
    list_service, get_marketplace_listings, get_my_listings,
)
from gumroad_payments import start_gumroad_payment
from discord_bot.ad_images import upload_ad_image
from discord_bot.cogs._views_shared import ActionButton, NavCardView, refresh_button
from discord_bot.cogs._views_ads_wizard import build_manager_view, _load as _load_manager

logger = logging.getLogger(__name__)


def _is_ads_admin(user_id: int) -> bool:
    return user_id in DISCORD_CLONE_ADMIN_IDS


class AdsMarketplaceCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    ad = app_commands.Group(name="ad", description="Sponsored ads (owner-approved)")

    # ── ad picker (no more typing ids) ──────────────────────────────────
    # Every ad_id option below is an autocomplete: the user sees
    # "#12 · Acme — Big Sale (approved)" and Discord submits the int id.
    # The bot owner sees every ad; anyone else only their own.
    async def _ad_choices(self, interaction: discord.Interaction, current: str, statuses: tuple = None):
        owner = _is_ads_admin(interaction.user.id)
        ads = await search_ads(None if owner else interaction.user.id, current, statuses)
        return [
            app_commands.Choice(
                name=f"#{a['id']} · {a['company_name']} — {a['ad_title']} ({a['status']})"[:100],
                value=a["id"],
            )
            for a in ads
        ]

    async def _any_ad_autocomplete(self, interaction: discord.Interaction, current: str):
        return await self._ad_choices(interaction, current)

    async def _pending_ad_autocomplete(self, interaction: discord.Interaction, current: str):
        return await self._ad_choices(interaction, current, ("pending",))

    async def _toggle_ad_autocomplete(self, interaction: discord.Interaction, current: str):
        return await self._ad_choices(interaction, current, ("approved", "deactivated"))

    @ad.command(name="submit", description="Submit an ad for approval")
    @app_commands.describe(
        company_name="Your company/brand name", title="Ad headline", description="Ad body text",
        target_url="Where the ad should link", budget_usd="Proposed budget in USD",
        image="Optional image for the ad (png/jpeg/gif/webp, max 8MB)",
    )
    async def ad_submit(
        self, interaction: discord.Interaction, company_name: str, title: str,
        description: str, target_url: str, budget_usd: float,
        image: discord.Attachment = None,
    ):
        if budget_usd < AD_PLACEMENT_FEE_USD:
            await interaction.response.send_message(
                f"Minimum ad budget is ${AD_PLACEMENT_FEE_USD:.2f}.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        # Validate/upload the image first so a bad file fails BEFORE the ad is
        # recorded and a payment link is issued.
        img_channel_id = img_message_id = None
        if image is not None:
            img_channel_id, img_message_id, reason = await upload_ad_image(
                interaction.client, image, interaction.user
            )
            if reason:
                await interaction.followup.send(
                    f"❌ Ad not submitted — {reason}. Fix the image (or leave it out) and run the command again.",
                    ephemeral=True,
                )
                return
        ad_id = await submit_ad(
            interaction.user.id, company_name.strip(), title.strip(), description.strip(),
            target_url.strip(), budget_usd,
            image_channel_id=img_channel_id, image_message_id=img_message_id,
        )
        if not ad_id:
            await interaction.followup.send("❌ Couldn't submit that ad. Try again.", ephemeral=True)
            return
        # Ad id is embedded in the reference (not a payment_logs column) so
        # payments_manual._unlock_ad_placement can recover it from the
        # webhook ping and auto-approve this exact ad — see that file.
        reference = f"gum_ad_placement_{interaction.user.id}_{secrets.token_hex(4)}_ad{ad_id}"
        await start_gumroad_payment(
            interaction, "ad_placement", f"${budget_usd:.2f}",
            guild_id=interaction.guild_id, reference=reference, amount_usd=budget_usd,
        )
        await interaction.followup.send(
            "Once approved, this ad is placed in the combined join DM and in every clone's "
            "bump channel."
            + (" 🖼️ Your image is attached." if img_message_id
               else f" Want an image on it? Run `/ad image ad_id:{ad_id}` and attach one."),
            ephemeral=True,
        )

    @ad.command(name="image", description="Attach or replace the image on one of your ads")
    @app_commands.describe(ad_id="The ad's id", image="png/jpeg/gif/webp, max 8MB")
    @app_commands.autocomplete(ad_id=_any_ad_autocomplete)
    async def ad_image(self, interaction: discord.Interaction, ad_id: int, image: discord.Attachment):
        await interaction.response.defer(ephemeral=True, thinking=True)
        is_admin = _is_ads_admin(interaction.user.id)
        ad = await get_ad(ad_id)
        if not ad or (ad["user_id"] != interaction.user.id and not is_admin):
            await interaction.followup.send("No ad with that id belongs to you.", ephemeral=True)
            return
        if ad["status"] == "rejected" and not is_admin:
            await interaction.followup.send("That ad was rejected, so its image can't be changed.", ephemeral=True)
            return
        channel_id, message_id, reason = await upload_ad_image(interaction.client, image, interaction.user, ad_id)
        if reason:
            await interaction.followup.send(f"❌ {reason.capitalize()}.", ephemeral=True)
            return
        ok = await set_ad_image(ad_id, None if is_admin else interaction.user.id, channel_id, message_id)
        await interaction.followup.send(
            f"🖼️ Image saved on ad #{ad_id}." if ok else "❌ Couldn't save the image on that ad. Try again.",
            ephemeral=True,
        )

    @ad.command(name="edit", description="[Owner] Edit any ad's text and/or image")
    @app_commands.describe(
        ad_id="The ad's id", company_name="New company/brand name", title="New headline",
        description="New body text", target_url="New link", image="New image (png/jpeg/gif/webp, max 8MB)",
    )
    @app_commands.autocomplete(ad_id=_any_ad_autocomplete)
    async def ad_edit(
        self, interaction: discord.Interaction, ad_id: int, company_name: str = None, title: str = None,
        description: str = None, target_url: str = None, image: discord.Attachment = None,
    ):
        if not _is_ads_admin(interaction.user.id):
            await interaction.response.send_message("You're not authorized to edit ads.", ephemeral=True)
            return
        if not any((company_name, title, description, target_url, image)):
            await interaction.response.send_message("Give me at least one thing to change.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        ad = await get_ad(ad_id)
        if not ad:
            await interaction.followup.send("No ad with that id.", ephemeral=True)
            return
        changed = []
        if any((company_name, title, description, target_url)):
            ok = await update_ad_fields(
                ad_id,
                company_name=company_name.strip() if company_name else None,
                ad_title=title.strip() if title else None,
                ad_description=description.strip() if description else None,
                target_url=target_url.strip() if target_url else None,
            )
            if not ok:
                await interaction.followup.send("❌ Couldn't update that ad's text.", ephemeral=True)
                return
            changed.append("text")
        if image is not None:
            channel_id, message_id, reason = await upload_ad_image(interaction.client, image, interaction.user, ad_id)
            if reason:
                await interaction.followup.send(
                    f"❌ Image not changed — {reason}." + (" (Text was updated.)" if changed else ""), ephemeral=True
                )
                return
            if not await set_ad_image(ad_id, None, channel_id, message_id):
                await interaction.followup.send("❌ Couldn't save the image on that ad.", ephemeral=True)
                return
            changed.append("image")
        await interaction.followup.send(f"✅ Ad #{ad_id} updated ({' + '.join(changed)}).", ephemeral=True)

    @ad.command(name="status", description="Check the status of an ad you submitted")
    @app_commands.describe(ad_id="The ad's id (given to you when you submitted it)")
    @app_commands.autocomplete(ad_id=_any_ad_autocomplete)
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

    @ad.command(name="manage", description="[Owner] Manage ads — pick one, then approve, reject, deactivate or edit")
    async def ad_manage(self, interaction: discord.Interaction):
        if not _is_ads_admin(interaction.user.id):
            await interaction.response.send_message("You're not authorized to manage ads.", ephemeral=True)
            return
        ads, counts, _ = await _load_manager()
        await interaction.response.send_message(view=build_manager_view(ads, counts), ephemeral=True)

    @ad.command(name="pending", description="[Owner] List ads awaiting approval")
    async def ad_pending(self, interaction: discord.Interaction):
        if not _is_ads_admin(interaction.user.id):
            await interaction.response.send_message("You're not authorized to review ads.", ephemeral=True)
            return
        pending = await get_pending_ads()
        if not pending:
            await interaction.response.send_message("No ads pending review.", ephemeral=True)
            return
        lines = [
            f"• **#{a['id']} — {a['company_name']}** — {a['ad_title']} · ${a['budget_usd']}"
            + (" · 🖼️" if a.get("image_message_id") else "")
            for a in pending
        ]
        buttons = [refresh_button(self, "ad_pending")]
        card = NavCardView("📋 Ads pending review", lines, discord.Color.orange(), buttons)
        await interaction.response.send_message(view=card, ephemeral=True)

    @ad.command(name="approve", description="[Owner] Approve a pending ad")
    @app_commands.describe(ad_id="The ad's id")
    @app_commands.autocomplete(ad_id=_pending_ad_autocomplete)
    async def ad_approve(self, interaction: discord.Interaction, ad_id: int):
        if not _is_ads_admin(interaction.user.id):
            await interaction.response.send_message("You're not authorized to approve ads.", ephemeral=True)
            return
        ok = await approve_ad(ad_id)
        await interaction.response.send_message("✅ Approved." if ok else "❌ Not found or already reviewed.", ephemeral=True)

    @ad.command(name="reject", description="[Owner] Reject a pending ad")
    @app_commands.describe(ad_id="The ad's id", reason="Why it's being rejected")
    @app_commands.autocomplete(ad_id=_pending_ad_autocomplete)
    async def ad_reject(self, interaction: discord.Interaction, ad_id: int, reason: str):
        if not _is_ads_admin(interaction.user.id):
            await interaction.response.send_message("You're not authorized to reject ads.", ephemeral=True)
            return
        ok = await reject_ad(ad_id, reason.strip()[:200])
        await interaction.response.send_message("✅ Rejected." if ok else "❌ Not found or already reviewed.", ephemeral=True)

    @ad.command(name="toggle", description="[Owner] Deactivate a live ad, or switch a deactivated one back on")
    @app_commands.describe(ad_id="Pick the ad")
    @app_commands.autocomplete(ad_id=_toggle_ad_autocomplete)
    async def ad_toggle(self, interaction: discord.Interaction, ad_id: int):
        if not _is_ads_admin(interaction.user.id):
            await interaction.response.send_message("You're not authorized to manage ads.", ephemeral=True)
            return
        ad = await get_ad(ad_id)
        if not ad:
            await interaction.response.send_message("No ad with that id.", ephemeral=True)
            return
        if ad["status"] == "approved":
            ok = await deactivate_ad(ad_id)
            msg = f"⏸️ Ad #{ad_id} deactivated — it's off every surface. Run this again to switch it back on."
        elif ad["status"] == "deactivated":
            ok = await reactivate_ad(ad_id)
            msg = f"▶️ Ad #{ad_id} is live again."
        else:
            await interaction.response.send_message(
                f"Ad #{ad_id} is **{ad['status']}**, so there's nothing to toggle "
                "(only approved/deactivated ads can be).", ephemeral=True,
            )
            return
        await interaction.response.send_message(msg if ok else "❌ That ad changed while you were looking at it. Try again.", ephemeral=True)

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
