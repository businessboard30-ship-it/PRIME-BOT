"""
Trading cards — cross-server marketplace built on database.py's card_*
tables. Deliberately GLOBAL (no guild_id): a card earned in one server can
be listed and bought by someone in a completely different server, scoped
only by clone_id so a clone's card economy stays independent of the main
bot's and other clones'.

Getting cards: a one-time starter pack on first use of any /card command,
a free /card daily pull (24h cooldown, rarity-weighted toward Common), and
paid packs bought with Card Coins (also rarity-weighted).

Card Coins are a brand new global currency, separate from each guild's
existing discord_economy_balances — that per-guild economy can't sensibly
back a cross-server marketplace, so this is its own pool, earned by
selling cards and spent buying packs/cards.

The marketplace itself (/card market) is a swipeable one-listing-per-page
embed with Prev/Next/Buy buttons, matching the "big table with cards to
swipe" ask — built as its own View rather than reusing ActionButton/NavView
since it needs live pagination state (current index) per message, not just
static routed buttons.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from database import db

logger = logging.getLogger(__name__)

DAILY_COOLDOWN = timedelta(hours=24)
STARTER_CARD_NAMES = ["Ember Sprite", "Tide Pup", "Leaf Whelp", "Spark Mouse"]
PACK_COST = 50  # Card Coins per card in a pack

RARITY_COLOR = {
    "common": discord.Color.light_grey(),
    "rare": discord.Color.blue(),
    "epic": discord.Color.purple(),
    "legendary": discord.Color.gold(),
}


def _rarity_label(rarity: str) -> str:
    return rarity.capitalize()


class CardsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @property
    def clone_id(self) -> Optional[int]:
        return getattr(self.bot, "clone_id", None)

    async def _ensure_starter_pack(self, user_id: int) -> bool:
        """Grants the fixed starter set on first-ever /card use. Returns
        True if a starter pack was just granted (so callers can mention it),
        False if the user already has cards."""
        if await db.has_starter_pack(user_id, self.clone_id):
            return False
        for name in STARTER_CARD_NAMES:
            card = await db.get_card_by_name(name, self.clone_id)
            if card:
                await db.grant_card(user_id, card["card_id"], self.clone_id, quantity=1)
        return True

    card = app_commands.Group(name="card", description="Trading cards — pull, trade, and browse the marketplace")

    # ── Daily / packs / inventory ──────────────────────────────────────

    @card.command(name="daily", description="Claim your free daily card pull")
    async def daily(self, interaction: discord.Interaction):
        await interaction.response.defer()
        got_starter = await self._ensure_starter_pack(interaction.user.id)

        last = await db.get_card_daily_cooldown(interaction.user.id, self.clone_id)
        if last:
            last = last.replace(tzinfo=timezone.utc) if last.tzinfo is None else last
            elapsed = datetime.now(timezone.utc) - last
            if elapsed < DAILY_COOLDOWN:
                remaining = DAILY_COOLDOWN - elapsed
                hours, mins = remaining.seconds // 3600, (remaining.seconds % 3600) // 60
                await interaction.followup.send(
                    f"⏳ Already claimed — try again in {hours}h {mins}m."
                    + ("\n🎁 (Starter pack granted — check `/card inventory`!)" if got_starter else "")
                )
                return

        card_row = await db.roll_random_card(self.clone_id)
        if not card_row:
            await interaction.followup.send("No cards in the catalog yet — ask the bot owner to add some.")
            return

        await db.grant_card(interaction.user.id, card_row["card_id"], self.clone_id, quantity=1)
        await db.set_card_daily_claimed(interaction.user.id, self.clone_id)
        await self._notify_watchers_if_relevant(card_row)

        embed = discord.Embed(
            title=f"{card_row['emoji'] or '🎴'} {card_row['name']}",
            description=f"Rarity: **{_rarity_label(card_row['rarity'])}**\nDaily pull claimed!",
            color=RARITY_COLOR.get(card_row["rarity"], discord.Color.default()),
        )
        if got_starter:
            embed.set_footer(text="Starter pack granted — check /card inventory!")
        await interaction.followup.send(embed=embed)

    @card.command(name="pack", description=f"Buy a card pack with Card Coins ({PACK_COST} each)")
    @app_commands.describe(amount="How many cards to pull (1-5)")
    async def pack(self, interaction: discord.Interaction, amount: app_commands.Range[int, 1, 5] = 1):
        await interaction.response.defer()
        await self._ensure_starter_pack(interaction.user.id)

        cost = PACK_COST * amount
        spent = await db.try_spend_card_coins(interaction.user.id, cost, self.clone_id)
        if not spent:
            balance = await db.get_card_coins(interaction.user.id, self.clone_id)
            await interaction.followup.send(
                f"❌ Not enough Card Coins — need {cost}, you have {balance}. "
                f"Sell cards on `/card market` to earn more."
            )
            return

        pulled = []
        for _ in range(amount):
            card_row = await db.roll_random_card(self.clone_id)
            if not card_row:
                continue
            await db.grant_card(interaction.user.id, card_row["card_id"], self.clone_id, quantity=1)
            await self._notify_watchers_if_relevant(card_row)
            pulled.append(card_row)

        if not pulled:
            await db.add_card_coins(interaction.user.id, cost, self.clone_id)  # refund
            await interaction.followup.send("No cards in the catalog yet — refunded your coins.")
            return

        lines = [f"{c['emoji'] or '🎴'} **{c['name']}** — {_rarity_label(c['rarity'])}" for c in pulled]
        embed = discord.Embed(
            title=f"📦 Opened {amount} pack{'s' if amount > 1 else ''}",
            description="\n".join(lines),
            color=discord.Color.blurple(),
        )
        embed.set_footer(text=f"Spent {cost} Card Coins")
        await interaction.followup.send(embed=embed)

    @card.command(name="inventory", description="View your card collection")
    async def inventory(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        got_starter = await self._ensure_starter_pack(interaction.user.id)
        cards = await db.get_user_cards(interaction.user.id, self.clone_id)
        if not cards:
            await interaction.followup.send("You don't have any cards yet — try `/card daily`.", ephemeral=True)
            return
        lines = [f"{c['emoji'] or '🎴'} **{c['name']}** ({_rarity_label(c['rarity'])}) ×{c['quantity']}" for c in cards]
        embed = discord.Embed(title=f"🎴 {interaction.user.display_name}'s Cards", description="\n".join(lines)[:4000],
                               color=discord.Color.blurple())
        if got_starter:
            embed.set_footer(text="Starter pack granted!")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @card.command(name="coins", description="Check your Card Coins balance")
    async def coins(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        balance = await db.get_card_coins(interaction.user.id, self.clone_id)
        await interaction.followup.send(f"🪙 You have **{balance}** Card Coins.", ephemeral=True)

    # ── Marketplace ─────────────────────────────────────────────────────

    @card.command(name="sell", description="List one of your cards for sale on the marketplace")
    @app_commands.describe(name="Exact card name", price="Asking price in Card Coins")
    async def sell(self, interaction: discord.Interaction, name: str, price: app_commands.Range[int, 1, 1_000_000]):
        await interaction.response.defer(ephemeral=True)
        card_row = await db.get_card_by_name(name, self.clone_id)
        if not card_row:
            await interaction.followup.send(f"❌ No card named \"{name}\" exists.", ephemeral=True)
            return
        taken = await db.take_card(interaction.user.id, card_row["card_id"], self.clone_id, quantity=1)
        if not taken:
            await interaction.followup.send(f"❌ You don't own a **{card_row['name']}** to sell.", ephemeral=True)
            return
        listing_id = await db.create_card_listing(interaction.user.id, card_row["card_id"], price, self.clone_id)
        await self.notify_watchers_on_listing(card_row["card_id"], card_row["name"], interaction.user.display_name, price)
        await interaction.followup.send(
            f"✅ Listed **{card_row['name']}** for **{price}** Card Coins (listing #{listing_id}). "
            f"Anyone on any server can now buy it via `/card market`.",
            ephemeral=True,
        )

    @card.command(name="listings", description="View and cancel your active marketplace listings")
    async def listings(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        rows = await db.get_user_listings(interaction.user.id, self.clone_id)
        if not rows:
            await interaction.followup.send("You have no active listings.", ephemeral=True)
            return
        lines = [f"#{r['listing_id']} — {r['emoji'] or '🎴'} **{r['name']}** — {r['price']} coins" for r in rows]
        embed = discord.Embed(title="📋 Your Listings", description="\n".join(lines)[:4000],
                               color=discord.Color.blurple())
        embed.set_footer(text="Use /card cancel listing_id:<#> to pull one down")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @card.command(name="cancel", description="Cancel one of your marketplace listings")
    @app_commands.describe(listing_id="The listing number from /card listings")
    async def cancel(self, interaction: discord.Interaction, listing_id: int):
        await interaction.response.defer(ephemeral=True)
        ok = await db.cancel_card_listing(listing_id, interaction.user.id)
        if ok:
            await interaction.followup.send(f"✅ Listing #{listing_id} cancelled — card returned to your inventory.", ephemeral=True)
        else:
            await interaction.followup.send("❌ That listing isn't active, or isn't yours.", ephemeral=True)

    @card.command(name="watch", description="Get notified next time a specific card is listed")
    @app_commands.describe(name="Exact card name")
    async def watch(self, interaction: discord.Interaction, name: str):
        await interaction.response.defer(ephemeral=True)
        card_row = await db.get_card_by_name(name, self.clone_id)
        if not card_row:
            await interaction.followup.send(f"❌ No card named \"{name}\" exists.", ephemeral=True)
            return
        await db.add_card_watch(interaction.user.id, card_row["card_id"], self.clone_id)
        await interaction.followup.send(
            f"🔔 You'll get a DM the next time a **{card_row['name']}** is listed.", ephemeral=True
        )

    @card.command(name="market", description="Browse the cross-server card marketplace")
    @app_commands.describe(rarity="Filter by rarity", search="Filter by name")
    @app_commands.choices(rarity=[
        app_commands.Choice(name="Common", value="common"),
        app_commands.Choice(name="Rare", value="rare"),
        app_commands.Choice(name="Epic", value="epic"),
        app_commands.Choice(name="Legendary", value="legendary"),
    ])
    async def market(self, interaction: discord.Interaction, rarity: Optional[app_commands.Choice[str]] = None,
                      search: Optional[str] = None):
        await interaction.response.defer()
        listings = await db.get_active_listings(
            self.clone_id, rarity=rarity.value if rarity else None, search=search
        )
        if not listings:
            await interaction.followup.send("📭 No active listings match that filter right now.")
            return
        view = MarketBrowserView(self, listings, interaction.user.id)
        await interaction.followup.send(embed=view.embed(), view=view)

    async def _notify_watchers_if_relevant(self, card_row: dict) -> None:
        """No-op here — daily/pack pulls don't create listings, so nothing
        to notify. Kept as a hook so future drop sources can call it
        without needing to know about the listings table directly."""
        return

    async def notify_watchers_on_listing(self, card_id: int, card_name: str, seller_name: str, price: int) -> None:
        watcher_ids = await db.get_watchers_for_card(card_id, self.clone_id)
        for uid in watcher_ids:
            try:
                user = await self.bot.fetch_user(uid)
                await user.send(
                    f"🔔 **{card_name}** you were watching for just got listed by {seller_name} for {price} Card Coins! "
                    f"Use `/card market search:{card_name}` to grab it."
                )
            except (discord.Forbidden, discord.HTTPException):
                pass


class MarketBrowserView(discord.ui.View):
    """One-listing-per-page swipeable browser. Prev/Next page through the
    listing set fetched at command time (a snapshot — doesn't live-refresh
    if new listings appear while someone's browsing, matching how /shop
    and similar paginators elsewhere in this bot already behave). Buy
    re-validates against the DB at click time regardless of what the
    snapshot shows, so a stale page can't double-sell a listing someone
    else already bought."""

    def __init__(self, cog: CardsCog, listings: list, viewer_id: int):
        super().__init__(timeout=180)
        self.cog = cog
        self.listings = listings
        self.viewer_id = viewer_id
        self.index = 0
        self._sync_buy_button_state()

    def _sync_buy_button_state(self):
        current = self.listings[self.index]
        self.buy_button.disabled = current["seller_id"] == self.viewer_id

    def embed(self) -> discord.Embed:
        item = self.listings[self.index]
        embed = discord.Embed(
            title=f"{item['emoji'] or '🎴'} {item['name']}",
            description=f"Rarity: **{_rarity_label(item['rarity'])}**\nPrice: **{item['price']}** Card Coins",
            color=RARITY_COLOR.get(item["rarity"], discord.Color.default()),
        )
        if item.get("image_url"):
            embed.set_image(url=item["image_url"])
        embed.set_footer(text=f"Listing {self.index + 1}/{len(self.listings)} — #{item['listing_id']}")
        return embed

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary)
    async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.index = (self.index - 1) % len(self.listings)
        self._sync_buy_button_state()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    @discord.ui.button(label="Buy", style=discord.ButtonStyle.success, emoji="🛒")
    async def buy_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        item = self.listings[self.index]
        result = await db.buy_card_listing(item["listing_id"], interaction.user.id)
        if not result:
            await interaction.followup.send(
                "❌ Couldn't complete that purchase — it may already be sold, cancelled, "
                "be your own listing, or you may not have enough Card Coins.",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            f"✅ Bought **{result['card_name']}** for {result['price']} Card Coins!", ephemeral=True
        )
        # Alert the seller
        try:
            seller = await interaction.client.fetch_user(result["seller_id"])
            await seller.send(
                f"💰 Your **{result['card_name']}** sold for **{result['price']}** Card Coins "
                f"to {interaction.user.display_name}!"
            )
        except (discord.Forbidden, discord.HTTPException):
            pass

        # Remove the now-sold listing from this browsing session and refresh
        self.listings.pop(self.index)
        if not self.listings:
            for child in self.children:
                child.disabled = True
            await interaction.edit_original_response(
                content="📭 No more listings match this filter.", embed=None, view=self
            )
            return
        self.index = self.index % len(self.listings)
        self._sync_buy_button_state()
        await interaction.edit_original_response(embed=self.embed(), view=self)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.index = (self.index + 1) % len(self.listings)
        self._sync_buy_button_state()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True


async def setup(bot: commands.Bot):
    await bot.add_cog(CardsCog(bot))
