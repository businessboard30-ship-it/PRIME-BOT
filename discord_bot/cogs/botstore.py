"""
Bot Submission → Shared Directory (Phase 5) — Discord commands for
modules/botstore_adapter.py's existing "bot" listing type.

This reuses the same botstore_listings/botstore_ratings tables and adapter
functions the Telegram side already built (handlers/botstore_handler.py) —
same pattern as ads_marketplace.py: the adapter is plain Postgres CRUD with
no Telegram dependency, so nothing there needed to change.

Scoped to type="bot" only here (not "group"/"channel" — those are Telegram
concepts: a t.me group/channel link doesn't have a Discord equivalent worth
forcing into this command set). A user "submits" one of their own bots
(any Discord bot they run/manage — not necessarily one cloned through this
project) so other members can discover it.

NOT PORTED: featured-listing payment (Paystack, GHS pricing) and the
admin moderation queue (report/approve) that the Telegram BotStore has —
paged for later the same way ad_marketplace's payment collection was:
this just lists things live immediately, gated only by FREE_BOT_LIMIT per
user (checked via modules.superbot_adapter.get_user_tier so paid tiers get
unlimited submissions like they do everywhere else on the Discord side).

Telegram's `to_url()` helper (in botstore_adapter.py) rewrites @handles and
t.me/... into full Telegram URLs — not meaningful for a Discord bot invite
link, so submissions here go straight to save_listing() with the link used
as-is (must be a full https:// URL, e.g. a Discord OAuth invite link).
"""

import discord
from discord import app_commands
from discord.ext import commands

from modules.botstore_adapter import (
    new_listing_id, save_listing, get_listing, search_listings, list_by_type,
    owner_listings, bot_limit_reached, add_rating, get_avg_rating,
    trending, CATEGORIES, ConfigCache,
)
from modules.superbot_adapter import get_user_tier
from discord_bot.cogs._views_botstore import RateFlowView, BrowseNavView

LISTING_TYPE = "bot"


class BotDirectoryCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    botstore = app_commands.Group(name="botstore", description="Directory of member-submitted bots")

    @botstore.command(name="submit", description="Submit your own bot to the directory")
    @app_commands.describe(
        name="Bot's display name", invite_link="Full https:// invite/info link for the bot",
        description="What it does", category="Pick the closest fit",
    )
    @app_commands.choices(category=[app_commands.Choice(name=c, value=c) for c in CATEGORIES])
    async def botstore_submit(
        self, interaction: discord.Interaction, name: str, invite_link: str,
        description: str, category: app_commands.Choice[str],
    ):
        if not invite_link.strip().lower().startswith("https://"):
            await interaction.response.send_message("The link needs to be a full `https://` URL.", ephemeral=True)
            return

        tier = await get_user_tier(interaction.user.id)
        is_premium = tier not in ("basic", "free")
        if await bot_limit_reached(interaction.user.id, is_premium):
            await interaction.response.send_message(
                f"You've hit the free limit of {ConfigCache.FREE_BOT_LIMIT} bot listings. "
                f"Remove an old one with `/botstore mine`, or upgrade your tier for unlimited listings.",
                ephemeral=True,
            )
            return

        listing_id = new_listing_id()
        ok = await save_listing({
            "id": listing_id, "owner_id": interaction.user.id, "type": LISTING_TYPE,
            "category": category.value, "title": name.strip()[:255],
            "description": description.strip(), "link": invite_link.strip(), "status": "live",
        })
        if not ok:
            await interaction.response.send_message("❌ Couldn't submit that. Try again.", ephemeral=True)
            return
        await interaction.response.send_message(
            embed=discord.Embed(
                description=f"✅ **{name.strip()}** is live in the directory. Listing id: `{listing_id}`.",
                color=discord.Color.green(),
            ),
            ephemeral=True,
        )

    @botstore.command(name="browse", description="Browse submitted bots")
    @app_commands.describe(category="Optional: filter by category")
    @app_commands.choices(category=[app_commands.Choice(name=c, value=c) for c in CATEGORIES])
    async def botstore_browse(self, interaction: discord.Interaction, category: app_commands.Choice[str] = None):
        listings = await list_by_type(LISTING_TYPE, category.value if category else None)
        if not listings:
            await interaction.response.send_message("No bots listed yet.", ephemeral=True)
            return
        view = BrowseNavView(listings, interaction.user.id)
        embed = await view.current_embed()
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    async def botstore_rate_picker(self, interaction: discord.Interaction):
        listings = await list_by_type(LISTING_TYPE, None)
        if not listings:
            await interaction.response.send_message("No bots to rate yet.", ephemeral=True)
            return
        view = RateFlowView(listings, interaction.user.id)
        await interaction.response.send_message("Pick a bot, then a star rating:", view=view, ephemeral=True)

    @botstore.command(name="trending", description="Most-clicked bots in the directory")
    async def botstore_trending(self, interaction: discord.Interaction):
        listings = await trending(LISTING_TYPE)
        if not listings:
            await interaction.response.send_message("Nothing trending yet.", ephemeral=True)
            return
        view = BrowseNavView(listings, interaction.user.id)
        embed = await view.current_embed()
        embed.title = f"🔥 {embed.title}"
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @botstore.command(name="mine", description="Show bots you've submitted")
    async def botstore_mine(self, interaction: discord.Interaction):
        listings = [l for l in await owner_listings(interaction.user.id) if l["type"] == LISTING_TYPE]
        if not listings:
            await interaction.response.send_message("You haven't submitted any bots yet.", ephemeral=True)
            return
        embed = discord.Embed(title="📋 Your bot listings", color=discord.Color.blurple())
        for l in listings[:10]:
            status_emoji = "🟢" if l.get("status") == "live" else "⚪"
            embed.add_field(
                name=f"{status_emoji} {l['title']}",
                value=f"`{l['id']}` · {l.get('status', 'unknown')} · {l.get('clicks', 0)} clicks",
                inline=False,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @botstore.command(name="search", description="Search the bot directory")
    @app_commands.describe(query="Keyword to search titles/descriptions")
    async def botstore_search(self, interaction: discord.Interaction, query: str):
        results = await search_listings(query.strip(), LISTING_TYPE)
        if not results:
            await interaction.response.send_message("No matches.", ephemeral=True)
            return
        view = BrowseNavView(results, interaction.user.id)
        embed = await view.current_embed()
        embed.title = f"🔎 {embed.title}"
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @botstore.command(name="rate", description="Rate a bot in the directory")
    @app_commands.describe(listing_id="The listing's id (shown in browse/search)", stars="1-5")
    async def botstore_rate(self, interaction: discord.Interaction, listing_id: str, stars: app_commands.Range[int, 1, 5]):
        listing = await get_listing(listing_id)
        if not listing or listing["type"] != LISTING_TYPE:
            await interaction.response.send_message("No bot listing with that id.", ephemeral=True)
            return
        await add_rating(listing_id, interaction.user.id, stars)
        avg = await get_avg_rating(listing_id)
        await interaction.response.send_message(
            embed=discord.Embed(description=f"✅ Rated. Average is now {avg}★.", color=discord.Color.green()),
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(BotDirectoryCog(bot))
