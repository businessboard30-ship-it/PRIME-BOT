# path: discord_bot/cogs/_views_cat_market.py

"""
Cat market + leaderboard UI (play coins only, never real money; see modules/cat_market.py and modules/cat_board.py).

  * Sell: in "My cats" -> Trade / gift -> "Sell for coins" opens a small wizard (pick a price inside the range for the
    cat's rarity and rank, or type one, then confirm). The bot posts the cat in the channel like a spawn: picture,
    price tag, Buy button. The first person who can pay takes it; the seller gets the coins.
  * Market (button on /cat): every open listing from the main bot and all clones, cheapest first, paged. A Components V2
    message: one long list where every cat shows its picture and its seller's avatar in the same message. New public
    "for sale" posts are V2 cards too. Older ones are classic embed posts that stay in channels (a message can't be
    switched to V2 after it is posted), so Buy / Take back and the market's "close the post" step edit either kind.
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
    if cm.FEE_PERCENT:
        lines.append(f"-# A {cm.FEE_PERCENT}% market fee is taken from the price when it sells. Listings last {cm.EXPIRY_DAYS} days.")
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


async def listing_layout(client, listing: cm.Listing, status_line: Optional[str] = None,
                         open_: bool = False) -> discord.ui.LayoutView:
    """A for-sale post as one V2 card: the cat's facts, its picture and, while the listing is open, the Adopt and
    Take back buttons (the same persistent buttons as before). A sold or taken-back post has no buttons."""
    cat = listing.cat
    lines = [f"## \U0001F3F7\uFE0F {cat.name} is for sale" if cat else "## \U0001F3F7\uFE0F A cat is for sale"]
    if status_line:
        lines += [status_line, ""]
    if cat:
        lines += [f"**Rarity:** {ca.rarity_badge(cat.rarity)}", f"**Personality:** {cat.trait}",
                  f"**Rank:** {cr.badge(cat.cat_rank)}", f"**Times traded:** {cat.traded_count}",
                  f"**Cat ID:** #{cat.id}"]
    lines += [f"**Price:** {cat_wallet.COINS} **{listing.price:,} coins**", f"**Seller:** <@{listing.seller_id}>"]
    box = discord.ui.Container(accent_colour=GOLD)
    box.add_item(discord.ui.TextDisplay("\n".join(lines)[:3900]))
    if cat:
        try:
            url = await cat_pool.url_for(client, cat.pool_id)
        except Exception:
            url = None
        if url:
            box.add_item(discord.ui.MediaGallery(discord.MediaGalleryItem(str(url), description=cat.name[:256])))
    box.add_item(discord.ui.TextDisplay(f"-# {NOTE}"))
    if open_:
        box.add_item(discord.ui.Separator())
        box.add_item(discord.ui.ActionRow(BuyButton(listing.id, listing.price), UnlistButton(listing.id)))
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(box)
    return view


async def listing_edit_kwargs(client, message, listing: cm.Listing, status_line: str) -> dict:
    """What to pass to an edit that closes a for-sale post, for either kind of post: an older classic embed post gets
    a new embed and loses its buttons; a V2 post gets a V2 card without buttons."""
    if vis.message_is_v2(message):
        return {"view": await listing_layout(client, listing, status_line),
                "allowed_mentions": discord.AllowedMentions.none()}
    return {"embed": await listing_embed(client, listing, status_line), "view": None}


async def close_listing_post(client, listing: cm.Listing, status_line: str) -> None:
    """Best effort: close the original for-sale post from somewhere that only knows its ids (a PartialMessage can't
    tell which kind it is). Tries the classic edit first, which is what every older post needs, then the V2 edit.
    Never raises."""
    if not (listing.channel_id and listing.message_id):
        return
    try:
        part = client.get_partial_messageable(listing.channel_id).get_partial_message(listing.message_id)
        try:
            await part.edit(embed=await listing_embed(client, listing, status_line), view=None)
        except discord.HTTPException:
            await part.edit(view=await listing_layout(client, listing, status_line),
                            allowed_mentions=discord.AllowedMentions.none())
    except Exception:
        logger.debug("[cat-market] couldn't update the listing post", exc_info=True)


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
        got = listing.price - listing.fee
        extra = f" (market fee {listing.fee:,}, you got **{got:,}**)" if listing.fee else ""
        await seller.send(f"\U0001F4B0 **{name}** sold to {buyer} for **{listing.price:,} coins**{extra}! They're in your purse now.")
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
        await interaction.response.edit_message(**await listing_edit_kwargs(
            interaction.client, interaction.message, l, f"\u2705 **Sold** to {interaction.user.mention} for {l.price:,} coins!"))
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
        await interaction.response.edit_message(**await listing_edit_kwargs(
            interaction.client, interaction.message, l, "\u21A9\uFE0F The seller took this cat off the market."))


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
        msg = await channel.send(view=await listing_layout(client, listing, open_=True),
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
            await vis.edit(interaction, view=self.wizard.confirm_view())
            return
        if interaction.channel is None or interaction.guild is None:
            await interaction.response.send_message("Sell cats in a server channel, not in DMs.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        await interaction.followup.send(await publish_listing(interaction, self.cat, price), ephemeral=True)


class SellWizard:
    """Two steps on one private message: 1) pick a price (presets across the allowed range, or type one), 2) confirm.
    Both steps are Components V2 cards (one Container: text, a Separator, the controls)."""

    def __init__(self, cat: ca.Cat, owner_id: int):
        self.cat, self.owner_id = cat, owner_id
        self.price: Optional[int] = None

    def _card(self, text: str, *rows) -> discord.ui.LayoutView:
        view = _OwnerView(self.owner_id)
        box = discord.ui.Container(accent_colour=GOLD)
        box.add_item(discord.ui.TextDisplay(text))
        box.add_item(discord.ui.Separator())
        for row in rows:
            box.add_item(row)
        view.add_item(box)
        return view

    def price_text(self) -> str:
        c = self.cat
        return (f"## \U0001F3F7\uFE0F Sell {c.name}  \u00b7  step 1 of 2\n"
                f"{ca.rarity_badge(c.rarity)} \u00b7 {cr.badge(c.cat_rank)}\n"
                f"A **{c.rarity}** cat at this rank can be sold for {range_line(c)}.\n"
                "Rank up your cat and its allowed range goes up. Pick a price below, or type your own.\n"
                f"-# {NOTE}")

    def price_view(self) -> discord.ui.LayoutView:
        wiz = self
        choices = cm.price_choices(self.cat.rarity, self.cat.cat_rank)
        sel = discord.ui.Select(placeholder="Pick a price\u2026", options=[
            discord.SelectOption(label=f"{p:,} coins", value=str(p), emoji="\U0001FA99") for p in choices])

        async def picked(interaction: discord.Interaction):
            wiz.price = int(sel.values[0])
            await vis.edit(interaction, view=wiz.confirm_view())

        sel.callback = picked
        custom = discord.ui.Button(label="Type a price", emoji="\u270F\uFE0F", style=discord.ButtonStyle.primary)

        async def typed(interaction: discord.Interaction):
            await interaction.response.send_modal(SellModal(wiz.cat, wiz))

        custom.callback = typed
        cancel = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary)

        async def cancelled(interaction: discord.Interaction):
            await vis.edit(interaction, view=_notice_layout("Okay, nothing was listed."))

        cancel.callback = cancelled
        return self._card(self.price_text(), discord.ui.ActionRow(sel), discord.ui.ActionRow(custom, cancel))

    def confirm_text(self) -> str:
        c = self.cat
        return (f"## \U0001F3F7\uFE0F Sell {c.name}  \u00b7  step 2 of 2\n"
                f"{ca.rarity_badge(c.rarity)} \u00b7 {cr.badge(c.cat_rank)}\n"
                f"**Price:** {cat_wallet.COINS} **{self.price:,} coins**  (allowed: {range_line(c)})\n\n"
                "I'll post it in this channel with a Buy button. You can take it back any time from the post. Ready?\n"
                f"-# {NOTE}")

    def confirm_view(self) -> discord.ui.LayoutView:
        wiz = self
        ok = discord.ui.Button(label="List it", emoji="\u2705", style=discord.ButtonStyle.success)

        async def confirmed(interaction: discord.Interaction):
            if interaction.channel is None or interaction.guild is None:
                await vis.edit(interaction, view=_notice_layout("Sell cats in a server channel, not in DMs."))
                return
            await interaction.response.defer()
            text = await publish_listing(interaction, wiz.cat, wiz.price)
            await vis.edit(interaction, original=True, view=_notice_layout(text))

        ok.callback = confirmed
        back = discord.ui.Button(label="Change price", emoji="\u2B05\uFE0F", style=discord.ButtonStyle.secondary)

        async def went_back(interaction: discord.Interaction):
            await vis.edit(interaction, view=wiz.price_view())

        back.callback = went_back
        return self._card(self.confirm_text(), discord.ui.ActionRow(ok, back))


class _OwnerView(discord.ui.LayoutView):
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
    await interaction.response.send_message(view=wiz.price_view(), ephemeral=True)


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


MARKET_PAGE = 4          # cats per page: each one uses 7 of the 40 components Discord allows in one V2 message


def _notice_layout(text: str) -> discord.ui.LayoutView:
    """A one-line V2 message (V2 messages can't carry plain content, so notes are cards too)."""
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(discord.ui.Container(discord.ui.TextDisplay(text), accent_colour=GOLD))
    return view


class _Seen:
    """What one listing shows besides its own data: the seller's name + avatar and the cat's picture."""
    __slots__ = ("seller", "avatar", "pic")

    def __init__(self, seller: str, avatar: Optional[str], pic: Optional[str]):
        self.seller, self.avatar, self.pic = seller, avatar, pic


async def _look_up(client, listings: List[cm.Listing]) -> Dict[int, "_Seen"]:
    shown = [l for l in listings if l.cat]
    sellers, pics = await asyncio.gather(
        asyncio.gather(*[_seller_info(client, l.seller_id) for l in shown]),
        asyncio.gather(*[cat_pool.url_for(client, l.cat.pool_id) for l in shown], return_exceptions=True))
    return {l.id: _Seen(name, avatar, pic if isinstance(pic, str) and pic else None)
            for l, (name, avatar), pic in zip(shown, sellers, pics)}


def _person(seen: "_Seen", prefix: str = "Seller"):
    """The seller line: their avatar as the small picture beside it when there is one."""
    text = f"-# {prefix}\n**{seen.seller}**"
    if seen.avatar:
        return discord.ui.Section(text, accessory=discord.ui.Thumbnail(seen.avatar, description=seen.seller[:256]))
    return discord.ui.TextDisplay(text)


def market_container(listings: List[cm.Listing], seen: Dict[int, "_Seen"], header: str, select=None, nav=None):
    """The whole market as one long Container: a header, then for every cat its card (cat picture) and its
    seller (avatar), then the picker and the page buttons. One message, no separate screens."""
    box = discord.ui.Container(accent_colour=GOLD)
    box.add_item(discord.ui.TextDisplay(header))
    for l in listings:
        if not l.cat or l.id not in seen:
            continue
        c, info = l.cat, seen[l.id]
        body = (f"### {ca.RARITY_EMOJI.get(c.rarity, '')} {c.name}\n{cr.badge(c.cat_rank)} \u00b7 {c.trait}\n"
                f"{cat_wallet.COINS} **{l.price:,} coins** \u00b7 cat #{c.id}")
        box.add_item(discord.ui.Separator())
        if info.pic:
            box.add_item(discord.ui.Section(body, accessory=discord.ui.Thumbnail(info.pic, description=c.name[:256])))
        else:
            box.add_item(discord.ui.TextDisplay(body))
        box.add_item(_person(info))
    if select is not None or nav:
        box.add_item(discord.ui.Separator())
    if select is not None:
        box.add_item(discord.ui.ActionRow(select))
    if nav:
        box.add_item(discord.ui.ActionRow(*nav))
    return box


class MarketButton(discord.ui.DynamicItem[discord.ui.Button], template=r"^cat_market$"):
    def __init__(self):
        super().__init__(discord.ui.Button(label="Market", emoji="\U0001F3F7\uFE0F", style=discord.ButtonStyle.secondary,
                                           custom_id="cat_market"))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        return cls()

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=not vis.is_public(interaction))      # avatars + pictures take a moment
        view = MarketView(interaction.client, interaction.user)
        await view.load()
        await vis.send(interaction, view=view, deferred=True)


class MarketView(discord.ui.LayoutView):
    def __init__(self, client, user, page: int = 0):
        super().__init__(timeout=300)
        self.client, self.user, self.page = client, user, page
        self.listings: List[cm.Listing] = []
        self.seen: Dict[int, _Seen] = {}
        self.total = 0
        self.note: Optional[str] = None

    @property
    def pages(self) -> int:
        return max(1, -(-self.total // MARKET_PAGE))

    async def load(self) -> None:
        self.listings, self.total = await cm.list_open(self.page, MARKET_PAGE)
        if self.page >= self.pages:
            self.page = self.pages - 1
            self.listings, self.total = await cm.list_open(self.page, MARKET_PAGE)
        self.seen = await _look_up(self.client, self.listings)
        self._build()

    def header(self) -> str:
        if not self.listings:
            text = ("## \U0001F3F7\uFE0F Cat market\nNo cats are for sale right now. "
                    "Put one up from **My cats** > Trade / gift > Sell for coins.")
        else:
            text = (f"## \U0001F3F7\uFE0F Cat market\n-# page {self.page + 1}/{self.pages} \u00b7 {self.total} for sale \u00b7 "
                    f"cheapest first \u00b7 main bot and clones together \u00b7 {NOTE}")
        return f"{self.note}\n\n{text}" if self.note else text

    def _build(self) -> None:
        self.clear_items()
        shown = [l for l in self.listings if l.cat]
        sel = None
        if shown:
            sel = discord.ui.Select(placeholder="Look closer at a cat\u2026", options=[
                discord.SelectOption(label=f"{l.cat.name} \u00b7 {l.cat.rarity.title()} \u00b7 {l.price:,} coins"[:100],
                                     description=l.cat.trait[:100], value=str(l.id), emoji=ca.RARITY_EMOJI.get(l.cat.rarity))
                for l in shown])
            sel.callback = self._picked
        nav = []
        for label, delta, off in (("\u25C0", -1, self.page <= 0), ("\u25B6", 1, self.page >= self.pages - 1)):
            b = discord.ui.Button(label=label, style=discord.ButtonStyle.secondary, disabled=off)
            b.callback = (lambda d: (lambda i: self._turn(i, d)))(delta)
            nav.append(b)
        self.add_item(market_container(self.listings, self.seen, self.header(), sel, nav if shown or self.page else None))
        vb = discord.ui.Button(label="Voice", emoji="\U0001F399\uFE0F", style=discord.ButtonStyle.secondary)
        vb.callback = self._voice_screen
        self.add_item(discord.ui.ActionRow(vb))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("Tap **Market** on `/cat` to open your own.", ephemeral=True)
            return False
        return True

    async def _voice_screen(self, interaction: discord.Interaction):
        from discord_bot.cogs import _views_market_voice as mvv
        await mvv.open_screen(interaction)

    async def _turn(self, interaction, delta: int):
        self.page += delta
        self.note = None
        await interaction.response.defer()                       # looking up the cards can take a moment
        await self.load()
        await vis.edit(interaction, original=True, view=self)

    async def _picked(self, interaction: discord.Interaction):
        l = await cm.get(int(interaction.data["values"][0]))
        if l is None or l.status != "open":
            self.note = "That cat was just taken."
            await interaction.response.defer()
            await self.load()
            await vis.edit(interaction, original=True, view=self)
            return
        self.note = None
        await interaction.response.defer()
        seen = self.seen.get(l.id) or (await _look_up(self.client, [l])).get(l.id) or _Seen(f"Seller {l.seller_id}", None, None)
        await vis.edit(interaction, original=True, view=MarketDetailView(self, l, seen))


class MarketDetailView(discord.ui.LayoutView):
    """One cat up close: a big picture, its details, the seller with their avatar, and the Adopt button."""

    def __init__(self, parent: MarketView, listing: cm.Listing, seen: _Seen):
        super().__init__(timeout=300)
        self.parent, self.listing = parent, listing
        cat = listing.cat
        box = discord.ui.Container(accent_colour=GOLD)
        box.add_item(discord.ui.TextDisplay(f"## \U0001F3F7\uFE0F {cat.name} is for sale" if cat else "## \U0001F3F7\uFE0F A cat is for sale"))
        if seen.pic:
            box.add_item(discord.ui.MediaGallery(discord.MediaGalleryItem(seen.pic, description=(cat.name if cat else "cat")[:256])))
        if cat:
            box.add_item(discord.ui.TextDisplay(
                f"**Rarity:** {ca.rarity_badge(cat.rarity)}\n**Personality:** {cat.trait}\n**Rank:** {cr.badge(cat.cat_rank)}\n"
                f"**Times traded:** {cat.traded_count}\n**Cat ID:** #{cat.id}"))
        box.add_item(discord.ui.TextDisplay(f"{cat_wallet.COINS} **{listing.price:,} coins**"))
        box.add_item(discord.ui.Separator())
        box.add_item(_person(seen))
        buy = discord.ui.Button(label=f"Adopt for {listing.price:,} coins", emoji="\U0001F4B0", style=discord.ButtonStyle.success)
        buy.callback = self._buy
        back = discord.ui.Button(label="Back", style=discord.ButtonStyle.secondary)
        back.callback = self._back
        voice = discord.ui.Button(label="Voice message", emoji="\U0001F399\uFE0F", style=discord.ButtonStyle.secondary)
        voice.callback = self._voice
        duel = discord.ui.Button(label="Duel the seller", emoji="\u2694\uFE0F", style=discord.ButtonStyle.secondary)
        duel.callback = self._duel
        box.add_item(discord.ui.ActionRow(buy, voice, duel, back))
        self.add_item(box)

    async def interaction_check(self, interaction):
        return await self.parent.interaction_check(interaction)

    async def _duel(self, interaction: discord.Interaction):
        from discord_bot.cogs import _views_cat_arena as arena_views         # lazy: it imports the market indirectly
        if interaction.user.id == self.listing.seller_id:
            await interaction.response.send_message("That is your own listing.", ephemeral=True)
            return
        await interaction.response.defer()
        await arena_views.open_duel(interaction, self.listing.seller_id)

    async def _voice(self, interaction: discord.Interaction):
        from discord_bot.cogs import _views_market_voice as mvv
        from modules import market_voice as mv
        if interaction.user.id == self.listing.seller_id:
            await interaction.response.send_message("That is your own listing.", ephemeral=True)
            return
        if not await mv.voice_on(self.listing.seller_id):
            await interaction.response.send_message("This seller is not taking voice messages.", ephemeral=True)
            return
        mvv.start_wait(interaction.user.id, self.listing.id)
        await interaction.response.send_message(
            f"\U0001F399\uFE0F Send me a Discord voice message (up to {mv.MAX_SECONDS}s) in a **DM with this bot** within "
            f"{mvv.WAIT_SECONDS // 60} minutes. {mv.PRIVACY}", ephemeral=True)

    async def _back(self, interaction):
        await interaction.response.defer()
        await self.parent.load()
        await vis.edit(interaction, original=True, view=self.parent)

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
        await vis.edit(interaction,
            view=_notice_layout(f"\U0001F389 **{l.cat.name}** is yours! Open **My cats** to see it."))
        await _after_sale(interaction.client, l, interaction.user)
        if l.cat:
            await cat_announce.announce(interaction, cat_announce.sale_text(
                interaction.user.mention, f"<@{l.seller_id}>", l.cat.name, l.cat.id, l.cat.cat_rank, l.price))
        # Best effort: close the original listing post, if this bot can reach it.
        await close_listing_post(interaction.client, l, f"\u2705 **Sold** to {interaction.user.mention} for {l.price:,} coins!")


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
        await view.draw()
        await vis.send(interaction, view=view)


class BoardView(discord.ui.LayoutView):
    """The leaderboard as one V2 card: title, the ranking, a footer, and the Top collectors / Richest / Post buttons."""

    def __init__(self, user, mode: str = "collectors"):
        super().__init__(timeout=300)
        self.user, self.mode = user, mode
        self._text = ""                            # the ranking; loaded by draw() because it needs the database
        self._build()

    def _build(self) -> None:
        self.clear_items()
        buttons = []
        for key, label in (("collectors", "Top collectors"), ("richest", "Richest")):
            b = discord.ui.Button(label=label, style=discord.ButtonStyle.primary if self.mode == key
                                  else discord.ButtonStyle.secondary, disabled=self.mode == key)
            b.callback = (lambda k: (lambda i: self._switch(i, k)))(key)
            buttons.append(b)
        if self.mode == "collectors":              # the richest board shows wallets, so it is never posted publicly
            post = discord.ui.Button(label="Post to channel", emoji="\U0001F4E3", style=discord.ButtonStyle.success)
            post.callback = self._post
            buttons.append(post)
        box = discord.ui.Container(accent_colour=GOLD)
        box.add_item(discord.ui.TextDisplay(self._body()))
        box.add_item(discord.ui.Separator())
        box.add_item(discord.ui.ActionRow(*buttons))
        self.add_item(box)

    def _body(self) -> str:
        title = "\U0001F3C6 Richest cat owners" if self.mode == "richest" else "\U0001F3C6 Top cat collectors"
        footer = ("Main bot and clones together \u00b7 "
                  + ("points: common 1, uncommon 3, rare 8, legendary 25" if self.mode != "richest" else "coins + diamonds"))
        ranking = self._text or "\u2026"
        return f"## {title}\n{ranking}\n-# {footer}"

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

    async def text(self) -> str:
        """The ranking for the current tab (what used to be the embed description)."""
        try:
            rows, rank = await (cat_board.top_richest if self.mode == "richest" else cat_board.top_collectors)(10, self.user.id)
            return cat_board.format_board(rows, self.mode, rank)
        except Exception:
            logger.exception("[cat-board] couldn't load the leaderboard")
            return "The leaderboard isn't available right now. Try again in a moment."

    async def draw(self) -> None:
        self._text = await self.text()
        self._build()

    async def _switch(self, interaction: discord.Interaction, mode: str):
        self.mode = mode
        await self.draw()
        await vis.edit(interaction, view=self)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("Tap **Leaderboard** on `/cat` to open your own.", ephemeral=True)
            return False
        return True


DYNAMIC_ITEMS = (BuyButton, UnlistButton, MarketButton, LeaderboardButton)
