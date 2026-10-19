# path: discord_bot/cogs/_views_cat_market.py

"""
Cat market + leaderboard UI (play coins only, never real money; see modules/cat_market.py and modules/cat_board.py).

  * Sell: in "My cats" -> Trade / gift -> "Sell for coins" opens a small wizard (pick a price inside the range for the
    cat's rarity and rank, or type one, then confirm). The bot posts the cat in the channel like a spawn: picture,
    price tag, Buy button. The first person who can pay takes it; the seller gets the coins.
  * Market (button on /cat): every open listing from the main bot and all clones, cheapest first, paged. It is one long
    list: each cat is its own card in the same message, with the cat's picture and the seller's avatar on it.
  * Leaderboard (button on /cat): top collectors and richest, across the main bot and all clones.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Dict, List, Optional, Tuple

import discord
from discord_bot.cogs import _cat_visibility as vis

from modules import cat_adopt as ca
from modules import cat_announce
from modules import cat_board
from modules import cat_log
from modules import cat_market as cm
from modules import cat_pool
from modules import cat_public
from modules import cat_rank as cr
from modules import cat_wallet
from modules import cat_features

logger = logging.getLogger(__name__)

PINK = discord.Color.from_rgb(255, 182, 193)
GOLD = discord.Color.gold()
NOTE = "Play coins only, no real money"


def _clone_id(client):
    return getattr(client, "clone_id", None)


async def listing_embed(client, listing: cm.Listing, status_line: Optional[str] = None) -> discord.Embed:
    cat = listing.cat
    e = discord.Embed(title=f"\U0001F3F7\uFE0F {cat.name} is for sale" if cat else "\U0001F3F7\uFE0F A cat is for sale",
                      color=GOLD)
    lines = []
    if cat:
        lines += [f"**Rarity:** {ca.rarity_badge(cat.rarity)}", f"**Personality:** {cat.trait}",
                  f"**Rank:** {cr.badge(cat.cat_rank)}", f"**Times traded:** {cat.traded_count}",
                  f"**Cat ID:** #{cat.id}"]
    lines += [f"**Price:** {cat_wallet.COINS} **{listing.price:,} coins**", f"**Seller:** <@{listing.seller_id}>"]
    e.description = (status_line + "\n\n" if status_line else "") + "\n".join(lines)
    if cat:
        try:
            url = await cat_pool.url_for(client, cat.pool_id)
        except Exception:
            url = None
        if url:
            e.set_image(url=url)
    e.set_footer(text=NOTE)
    return e


def listing_view(listing_id: int, price: Optional[int] = None) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(BuyButton(listing_id, price))
    view.add_item(UnlistButton(listing_id))
    return view


_BUY_TEXT = {
    "gone": "This cat isn't for sale any more.",
    "own": "That's your own cat. Use **Take back** to remove the listing.",
    "limit": f"Your home is full ({ca.MAX_CATS} cats). Release or trade one first.",
}


async def _after_sale(client, listing: cm.Listing, buyer) -> None:
    """Tell the seller (best effort) that the cat was sold."""
    try:
        seller = await client.fetch_user(listing.seller_id)
        name = listing.cat.name if listing.cat else "your cat"
        await seller.send(f"\U0001F4B0 **{name}** sold to {buyer} for **{listing.price:,} coins**! They're in your purse now.")
    except Exception:
        logger.debug("[cat-market] couldn't DM the seller", exc_info=True)


class BuyButton(discord.ui.DynamicItem[discord.ui.Button], template=r"^cat_buy:(\d+)$"):
    def __init__(self, listing_id: int, price: Optional[int] = None):
        self.listing_id = listing_id
        super().__init__(discord.ui.Button(
            label=f"Adopt for {price:,} coins" if price else "Adopt this cat", emoji="\U0001F4B0",
            style=discord.ButtonStyle.success, custom_id=f"cat_buy:{listing_id}"))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        return cls(int(match.group(1)))

    async def callback(self, interaction: discord.Interaction):
        try:
            res = await cm.buy(self.listing_id, interaction.user.id, _clone_id(interaction.client))
        except Exception:
            logger.exception("[cat-market] buy failed for listing %s", self.listing_id)
            await interaction.response.send_message("Something went wrong, try again in a moment.", ephemeral=True)
            return
        if res.status == "poor":
            w = await cat_wallet.safe_wallet(interaction.user.id, _clone_id(interaction.client))
            await interaction.response.send_message(
                f"You need more coins for this one. You have {cat_wallet.wallet_text(w)}. "
                "Adopt cats to earn coins, or convert diamonds in **My cats**.", ephemeral=True)
            return
        if res.status != "ok":
            await interaction.response.send_message(_BUY_TEXT.get(res.status, "That didn't work."), ephemeral=True)
            return
        l = res.listing
        cat_log.record(interaction.client, "sale", price=l.price, guild_id=getattr(interaction.guild, "id", None),
                       guild_name=getattr(interaction.guild, "name", None), user_id=interaction.user.id,
                       user_name=str(interaction.user), pool_id=l.cat.pool_id if l.cat else 0,
                       cat_name=l.cat.name if l.cat else "", rarity=l.cat.rarity if l.cat else "",
                       trait=l.cat.trait if l.cat else "", other_id=l.seller_id, other_name=str(l.seller_id))
        await interaction.response.edit_message(
            embed=await listing_embed(interaction.client, l, f"\u2705 **Sold** to {interaction.user.mention} for {l.price:,} coins!"),
            view=None)
        await interaction.followup.send(f"\U0001F389 **{l.cat.name if l.cat else 'The cat'}** is yours! Open **My cats** to see it.",
                                        ephemeral=True)
        await _after_sale(interaction.client, l, interaction.user)


class UnlistButton(discord.ui.DynamicItem[discord.ui.Button], template=r"^cat_unlist:(\d+)$"):
    def __init__(self, listing_id: int):
        self.listing_id = listing_id
        super().__init__(discord.ui.Button(label="Take back", style=discord.ButtonStyle.secondary,
                                           custom_id=f"cat_unlist:{listing_id}"))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        return cls(int(match.group(1)))

    async def callback(self, interaction: discord.Interaction):
        if not await cm.cancel(self.listing_id, interaction.user.id):
            l = await cm.get(self.listing_id)
            text = ("Only the seller can take this listing down." if l and l.status == "open"
                    else "This cat isn't for sale any more.")
            await interaction.response.send_message(text, ephemeral=True)
            return
        l = await cm.get(self.listing_id)
        await interaction.response.edit_message(
            embed=await listing_embed(interaction.client, l, "\u21A9\uFE0F The seller took this cat off the market."), view=None)


# ?? sell ??????????????????????????????????????????????????????????????????

_SELL_TEXT = {"not_yours": "That cat isn't yours any more.", "listed": "That cat is already for sale.",
              "away": "That cat is out on a walk or a heist \U0001F338 Sell it after it is back.",
              "too_many": f"You can have {cm.MAX_OPEN_PER_SELLER} cats for sale at a time. Take one back first.",
              "bad_price": "That price won't work.",
              "out_of_range": "That price is outside the allowed range for this cat's rank. Pick another price."}


def range_line(cat: ca.Cat) -> str:
    low, high = cm.price_range(cat.rarity, cat.cat_rank)
    return f"{cat_wallet.COINS} **{low:,}** to **{high:,}** coins"


async def publish_listing(interaction: discord.Interaction, cat: ca.Cat, price: int) -> str:
    """Create the listing and post it in the channel. The interaction must already be deferred. Returns the text for the seller."""
    channel = interaction.channel
    if channel is None or interaction.guild is None:
        return "Sell cats in a server channel, not in DMs."
    client = interaction.client
    status, listing = await cm.create_listing(cat.id, interaction.user.id, _clone_id(client), price,
                                              interaction.guild.id, channel.id)
    if status != "ok":
        return _SELL_TEXT.get(status, "That didn't work.")
    try:
        msg = await channel.send(embed=await listing_embed(client, listing), view=listing_view(listing.id, price),
                                 allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException:
        await cm.cancel(listing.id, interaction.user.id)
        return "I couldn't post the listing here (I need Send Messages and Embed Links)."
    await cm.set_message(listing.id, channel.id, msg.id)
    return (f"\U0001F3F7\uFE0F **{cat.name}** is for sale for **{price:,} coins**! You'll get the coins the moment "
            "someone adopts it. It also shows in **Market** on `/cat`, for the main bot and every clone.")


class SellModal(discord.ui.Modal):
    """The title and the box label carry the cat's name, so there is no doubt which cat is being priced.
    `wizard`, when given, is the sell wizard that opened it: a good price moves it on to the confirm step."""

    def __init__(self, cat: ca.Cat, wizard: "Optional[SellWizard]" = None):
        super().__init__(title=f"Sell {cat.name} (cat #{cat.id})"[:45])
        self.cat = cat
        self.wizard = wizard
        low, high = cm.price_range(cat.rarity, cat.cat_rank)
        self.price = discord.ui.TextInput(label=f"Price for {cat.name}, in coins"[:45], min_length=2, max_length=12,
                                          placeholder=f"{low:,} to {high:,}  (1500, 1.5k ok)"[:100])
        self.add_item(self.price)

    async def on_submit(self, interaction: discord.Interaction):
        price = cm.parse_price(str(self.price))
        low, high = cm.price_range(self.cat.rarity, self.cat.cat_rank)
        if price is None or not low <= price <= high:
            await interaction.response.send_message(
                f"**{self.cat.name}** ({cr.badge(self.cat.cat_rank)}) can be sold for {low:,} to {high:,} coins. "
                "Type a number in that range.", ephemeral=True)
            return
        if self.wizard is not None:
            self.wizard.price = price
            await interaction.response.edit_message(embed=self.wizard.confirm_embed(), view=self.wizard.confirm_view())
            return
        if interaction.channel is None or interaction.guild is None:
            await interaction.response.send_message("Sell cats in a server channel, not in DMs.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        await interaction.followup.send(await publish_listing(interaction, self.cat, price), ephemeral=True)


class SellWizard:
    """Two steps on one private message: 1) pick a price (presets across the allowed range, or type one), 2) confirm."""

    def __init__(self, cat: ca.Cat, owner_id: int):
        self.cat, self.owner_id = cat, owner_id
        self.price: Optional[int] = None

    def price_embed(self) -> discord.Embed:
        c = self.cat
        e = discord.Embed(title=f"\U0001F3F7\uFE0F Sell {c.name}  \u00b7  step 1 of 2", color=GOLD, description=(
            f"{ca.rarity_badge(c.rarity)} \u00b7 {cr.badge(c.cat_rank)}\n"
            f"A **{c.rarity}** cat at this rank can be sold for {range_line(c)}.\n"
            "Rank up your cat and its allowed range goes up. Pick a price below, or type your own."))
        e.set_footer(text=NOTE)
        return e

    def price_view(self) -> discord.ui.View:
        wiz = self
        view = _OwnerView(self.owner_id)
        choices = cm.price_choices(self.cat.rarity, self.cat.cat_rank)
        sel = discord.ui.Select(placeholder="Pick a price\u2026", options=[
            discord.SelectOption(label=f"{p:,} coins", value=str(p), emoji="\U0001FA99") for p in choices])

        async def picked(interaction: discord.Interaction):
            wiz.price = int(sel.values[0])
            await interaction.response.edit_message(embed=wiz.confirm_embed(), view=wiz.confirm_view())

        sel.callback = picked
        view.add_item(sel)
        custom = discord.ui.Button(label="Type a price", emoji="\u270F\uFE0F", style=discord.ButtonStyle.primary)

        async def typed(interaction: discord.Interaction):
            await interaction.response.send_modal(SellModal(wiz.cat, wiz))

        custom.callback = typed
        view.add_item(custom)
        cancel = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary)

        async def cancelled(interaction: discord.Interaction):
            await interaction.response.edit_message(content="Okay, nothing was listed.", embed=None, view=None)

        cancel.callback = cancelled
        view.add_item(cancel)
        return view

    def confirm_embed(self) -> discord.Embed:
        c = self.cat
        e = discord.Embed(title=f"\U0001F3F7\uFE0F Sell {c.name}  \u00b7  step 2 of 2", color=GOLD, description=(
            f"{ca.rarity_badge(c.rarity)} \u00b7 {cr.badge(c.cat_rank)}\n"
            f"**Price:** {cat_wallet.COINS} **{self.price:,} coins**  (allowed: {range_line(c)})\n\n"
            "I'll post it in this channel with a Buy button. You can take it back any time from the post. Ready?"))
        e.set_footer(text=NOTE)
        return e

    def confirm_view(self) -> discord.ui.View:
        wiz = self
        view = _OwnerView(self.owner_id)
        ok = discord.ui.Button(label="List it", emoji="\u2705", style=discord.ButtonStyle.success)

        async def confirmed(interaction: discord.Interaction):
            if interaction.channel is None or interaction.guild is None:
                await interaction.response.edit_message(content="Sell cats in a server channel, not in DMs.",
                                                        embed=None, view=None)
                return
            await interaction.response.defer()
            text = await publish_listing(interaction, wiz.cat, wiz.price)
            await interaction.edit_original_response(content=text, embed=None, view=None)

        ok.callback = confirmed
        view.add_item(ok)
        back = discord.ui.Button(label="Change price", emoji="\u2B05\uFE0F", style=discord.ButtonStyle.secondary)

        async def went_back(interaction: discord.Interaction):
            await interaction.response.edit_message(embed=wiz.price_embed(), view=wiz.price_view())

        back.callback = went_back
        view.add_item(back)
        return view


class _OwnerView(discord.ui.View):
    def __init__(self, owner_id: int):
        super().__init__(timeout=300)
        self.owner_id = owner_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("This is someone else's sale.", ephemeral=True)
            return False
        return True


async def open_sell_wizard(interaction: discord.Interaction, cat: ca.Cat) -> None:
    wiz = SellWizard(cat, interaction.user.id)
    await interaction.response.send_message(embed=wiz.price_embed(), view=wiz.price_view(), ephemeral=True)


# ?? market browser ????????????????????????????????????????????????????????

_SELLER_CACHE: Dict[int, Tuple[float, str, Optional[str]]] = {}      # seller id -> (expires, name, avatar url)
_SELLER_TTL = 600


async def _seller_info(client, seller_id: int) -> Tuple[str, Optional[str]]:
    """(display name, avatar url) for a seller, cached for a few minutes. Never raises: a stranger falls back to a plain name."""
    hit = _SELLER_CACHE.get(seller_id)
    if hit and hit[0] > time.monotonic():
        return hit[1], hit[2]
    user = client.get_user(seller_id)
    if user is None:
        try:
            user = await asyncio.wait_for(client.fetch_user(seller_id), timeout=3)
        except Exception:
            user = None
    name = (getattr(user, "display_name", None) or getattr(user, "name", None)) if user else None
    avatar = user.display_avatar.url if user is not None and getattr(user, "display_avatar", None) else None
    if user is not None:                                   # don't cache a failed lookup
        _SELLER_CACHE[seller_id] = (time.monotonic() + _SELLER_TTL, name or f"Seller {seller_id}", avatar)
    return name or f"Seller {seller_id}", avatar


async def market_cards(client, listings: List[cm.Listing]) -> List[discord.Embed]:
    """One card per listing, all in the same message: the seller's avatar and name on top, the cat's picture beside it."""
    shown = [l for l in listings if l.cat]
    sellers, urls = await asyncio.gather(
        asyncio.gather(*[_seller_info(client, l.seller_id) for l in shown]),
        asyncio.gather(*[cat_pool.url_for(client, l.cat.pool_id) for l in shown], return_exceptions=True))
    cards = []
    for l, (name, avatar), url in zip(shown, sellers, urls):
        c = l.cat
        e = discord.Embed(color=GOLD, title=f"{ca.RARITY_EMOJI.get(c.rarity, '')} {c.name}"[:256], description=(
            f"{cr.badge(c.cat_rank)} \u00b7 {c.trait}\n"
            f"{cat_wallet.COINS} **{l.price:,} coins** \u00b7 cat #{c.id}"))
        e.set_author(name=f"Seller: {name}"[:256], icon_url=avatar or None)
        if isinstance(url, str) and url:
            e.set_thumbnail(url=url)
        cards.append(e)
    return cards


class MarketButton(discord.ui.DynamicItem[discord.ui.Button], template=r"^cat_market$"):
    def __init__(self):
        super().__init__(discord.ui.Button(label="Market", emoji="\U0001F3F7\uFE0F", style=discord.ButtonStyle.secondary,
                                           custom_id="cat_market"))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        return cls()

    async def callback(self, interaction: discord.Interaction):
        view = MarketView(interaction.client, interaction.user)
        await view.load()
        await vis.send(interaction, view=view, **await view.render())


class MarketView(discord.ui.View):
    def __init__(self, client, user, page: int = 0):
        super().__init__(timeout=300)
        self.client, self.user, self.page = client, user, page
        self.listings: List[cm.Listing] = []
        self.total = 0

    async def load(self) -> None:
        self.listings, self.total = await cm.list_open(self.page)
        pages = max(1, -(-self.total // cm.PAGE_SIZE))
        if self.page >= pages:
            self.page = pages - 1
            self.listings, self.total = await cm.list_open(self.page)
        self._build()

    def header(self) -> str:
        pages = max(1, -(-self.total // cm.PAGE_SIZE))
        if not self.listings:
            return ("\U0001F3F7\uFE0F **Cat market**\nNo cats are for sale right now. "
                    "Put one up from **My cats** > Trade / gift > Sell for coins.")
        return (f"\U0001F3F7\uFE0F **Cat market** \u00b7 page {self.page + 1}/{pages} \u00b7 {self.total} for sale \u00b7 "
                f"cheapest first \u00b7 main bot and clones together \u00b7 {NOTE}")

    def embed(self) -> discord.Embed:
        """The plain one-card version (kept for callers that want a single embed)."""
        e = discord.Embed(title="\U0001F3F7\uFE0F Cat market", color=GOLD, description=self.header())
        return e

    async def render(self, note: Optional[str] = None) -> dict:
        """message content + one card per cat (cat picture + seller avatar), as one long list."""
        head = self.header() if not note else f"{note}\n\n{self.header()}"
        return {"content": head, "embeds": await market_cards(self.client, self.listings)}

    def _build(self) -> None:
        self.clear_items()
        if self.listings:
            sel = discord.ui.Select(placeholder="Look at a cat\u2026", options=[
                discord.SelectOption(label=f"{l.cat.name} \u00b7 {l.cat.rarity.title()} \u00b7 {l.price:,} coins"[:100],
                                     description=l.cat.trait, value=str(l.id), emoji=ca.RARITY_EMOJI.get(l.cat.rarity))
                for l in self.listings if l.cat])
            sel.callback = self._picked
            self.add_item(sel)
        pages = max(1, -(-self.total // cm.PAGE_SIZE))
        for label, delta, off in (("\u25C0", -1, self.page <= 0), ("\u25B6", 1, self.page >= pages - 1)):
            b = discord.ui.Button(label=label, style=discord.ButtonStyle.secondary, disabled=off)
            b.callback = (lambda d: (lambda i: self._turn(i, d)))(delta)
            self.add_item(b)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("Tap **Market** on `/cat` to open your own.", ephemeral=True)
            return False
        return True

    async def _turn(self, interaction, delta: int):
        self.page += delta
        await self.load()
        await interaction.response.defer()                      # building the cards can take a moment
        await interaction.edit_original_response(view=self, **await self.render())

    async def _picked(self, interaction: discord.Interaction):
        l = await cm.get(int(interaction.data["values"][0]))
        if l is None or l.status != "open":
            await self.load()
            await interaction.response.defer()
            await interaction.edit_original_response(view=self, **await self.render("That cat was just taken."))
            return
        await interaction.response.edit_message(content=None, embeds=[await listing_embed(self.client, l)],
                                                view=MarketDetailView(self, l))


class MarketDetailView(discord.ui.View):
    def __init__(self, parent: MarketView, listing: cm.Listing):
        super().__init__(timeout=300)
        self.parent, self.listing = parent, listing
        buy = discord.ui.Button(label=f"Adopt for {listing.price:,} coins", emoji="\U0001F4B0",
                                style=discord.ButtonStyle.success)
        buy.callback = self._buy
        back = discord.ui.Button(label="Back", style=discord.ButtonStyle.secondary)
        back.callback = self._back
        self.add_item(buy)
        self.add_item(back)

    async def interaction_check(self, interaction):
        return await self.parent.interaction_check(interaction)

    async def _back(self, interaction):
        await self.parent.load()
        await interaction.response.defer()
        await interaction.edit_original_response(view=self.parent, **await self.parent.render())

    async def _buy(self, interaction: discord.Interaction):
        res = await cm.buy(self.listing.id, interaction.user.id, _clone_id(interaction.client))
        if res.status == "poor":
            w = await cat_wallet.safe_wallet(interaction.user.id, _clone_id(interaction.client))
            await interaction.response.send_message(f"You need more coins. You have {cat_wallet.wallet_text(w)}.",
                                                    ephemeral=True)
            return
        if res.status != "ok":
            await interaction.response.send_message(_BUY_TEXT.get(res.status, "That didn't work."), ephemeral=True)
            return
        l = res.listing
        await interaction.response.edit_message(
            content=f"\U0001F389 **{l.cat.name}** is yours! Open **My cats** to see it.", embed=None, view=None)
        await _after_sale(interaction.client, l, interaction.user)
        if l.cat:
            await cat_announce.announce(interaction, cat_announce.sale_text(
                interaction.user.mention, f"<@{l.seller_id}>", l.cat.name, l.cat.id, l.cat.cat_rank, l.price))
        # Best effort: close the original listing post, if this bot can reach it.
        try:
            if l.channel_id and l.message_id:
                part = interaction.client.get_partial_messageable(l.channel_id).get_partial_message(l.message_id)
                await part.edit(embed=await listing_embed(
                    interaction.client, l, f"\u2705 **Sold** to {interaction.user.mention} for {l.price:,} coins!"), view=None)
        except Exception:
            logger.debug("[cat-market] couldn't update the listing post", exc_info=True)


# ?? leaderboard ???????????????????????????????????????????????????????????

class LeaderboardButton(discord.ui.DynamicItem[discord.ui.Button], template=r"^cat_board$"):
    def __init__(self):
        super().__init__(discord.ui.Button(label="Leaderboard", emoji="\U0001F3C6", style=discord.ButtonStyle.secondary,
                                           custom_id="cat_board"))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        return cls()

    async def callback(self, interaction: discord.Interaction):
        view = BoardView(interaction.user)
        await vis.send(interaction, embed=await view.embed(), view=view)


class BoardView(discord.ui.View):
    def __init__(self, user, mode: str = "collectors"):
        super().__init__(timeout=300)
        self.user, self.mode = user, mode
        self._build()

    def _build(self) -> None:
        self.clear_items()
        for key, label in (("collectors", "Top collectors"), ("richest", "Richest")):
            b = discord.ui.Button(label=label, style=discord.ButtonStyle.primary if self.mode == key
                                  else discord.ButtonStyle.secondary, disabled=self.mode == key)
            b.callback = (lambda k: (lambda i: self._switch(i, k)))(key)
            self.add_item(b)
        if self.mode == "collectors":              # the richest board shows wallets, so it is never posted publicly
            post = discord.ui.Button(label="Post to channel", emoji="\U0001F4E3", style=discord.ButtonStyle.success)
            post.callback = self._post
            self.add_item(post)

    async def _post(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message("Posting only works in a server channel.", ephemeral=True)
            return
        wait = cat_public.board_wait(interaction.channel_id, interaction.user.id)
        if wait > 0:
            await interaction.response.send_message(
                f"The leaderboard was just posted here. Try again in {max(1, round(wait / 60))} min.", ephemeral=True)
            return
        await interaction.response.defer()
        try:
            rows, _ = await cat_board.top_collectors(10, None)
        except Exception:
            logger.exception("[cat-board] couldn't load the leaderboard to post")
            await interaction.followup.send("The leaderboard isn't available right now.", ephemeral=True)
            return
        if not rows:
            await interaction.followup.send("Nobody is on the board yet.", ephemeral=True)
            return
        if await cat_announce.announce(interaction, cat_public.board_text(rows), gold=True):
            cat_public.mark_board(interaction.channel_id, interaction.user.id)
        else:
            await interaction.followup.send("I couldn't post here (the server may have public posts turned off).", ephemeral=True)

    async def embed(self) -> discord.Embed:
        try:
            rows, rank = await (cat_board.top_richest if self.mode == "richest" else cat_board.top_collectors)(10, self.user.id)
            text = cat_board.format_board(rows, self.mode, rank)
        except Exception:
            logger.exception("[cat-board] couldn't load the leaderboard")
            text = "The leaderboard isn't available right now. Try again in a moment."
        title = "\U0001F3C6 Richest cat owners" if self.mode == "richest" else "\U0001F3C6 Top cat collectors"
        e = discord.Embed(title=title, description=text, color=GOLD)
        e.set_footer(text="Main bot and clones together \u00b7 "
                          + ("points: common 1, uncommon 3, rare 8, legendary 25" if self.mode != "richest" else "coins + diamonds"))
        return e

    async def _switch(self, interaction: discord.Interaction, mode: str):
        self.mode = mode
        self._build()
        await interaction.response.edit_message(embed=await self.embed(), view=self)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("Tap **Leaderboard** on `/cat` to open your own.", ephemeral=True)
            return False
        return True


DYNAMIC_ITEMS = (BuyButton, UnlistButton, MarketButton, LeaderboardButton)
