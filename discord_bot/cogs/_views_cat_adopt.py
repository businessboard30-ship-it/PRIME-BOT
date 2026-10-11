# path: discord_bot/cogs/_views_cat_adopt.py

"""
Cat adoption UI (virtual only: play coins and diamonds earned by adopting, never real money).

  * Adopt button on every automatic cat spawn (a persistent DynamicItem, so it still works after a restart).
  * "My cats" button on spawns and on /cat: an ephemeral collection browser with Rename, Release and Trade.
  * Trade: pick a member, pick one of THEIR cats to swap for (or just gift yours), and they get an offer with
    Accept / Decline buttons. Nothing moves until they accept, and the swap is one atomic database step.
"""

from __future__ import annotations

import logging
import re
from typing import List, Optional

import discord

from modules import cat_adopt as ca
from modules import cat_pool
from modules import cat_wallet

logger = logging.getLogger(__name__)

PINK = discord.Color.from_rgb(255, 182, 193)
OFFER_TIMEOUT = 600
GIFT_VALUE = "gift"


def _clone_id(client):
    return getattr(client, "clone_id", None)


def cat_line(cat: ca.Cat) -> str:
    return f"**{cat.name}** · {ca.rarity_badge(cat.rarity)} · {cat.trait}"


async def cat_embed_full(client, cat: ca.Cat, position: str = "") -> discord.Embed:
    e = discord.Embed(title=f"{ca.RARITY_EMOJI.get(cat.rarity, '🐱')} {cat.name}", color=PINK)
    e.description = (f"**Rarity:** {ca.rarity_badge(cat.rarity)}\n**Personality:** {cat.trait}\n"
                     f"**Times traded:** {cat.traded_count}")
    try:
        url = await cat_pool.url_for(client, cat.pool_id)
    except Exception:
        url = None
    if url:
        e.set_image(url=url)
    if position:
        e.set_footer(text=position)
    return e


# ── Adopt button on a spawn ───────────────────────────────────────────────

def spawn_view(spawn_id: int, adopted: bool = False) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(AdoptButton(spawn_id, adopted=adopted))
    view.add_item(MyCatsButton())
    return view


class AdoptButton(discord.ui.DynamicItem[discord.ui.Button], template=r"^cat_adopt:(\d+)$"):
    def __init__(self, spawn_id: int, adopted: bool = False):
        self.spawn_id = spawn_id
        super().__init__(discord.ui.Button(
            label="Adopted 💖" if adopted else "Adopt me!", emoji=None if adopted else "🐾",
            style=discord.ButtonStyle.secondary if adopted else discord.ButtonStyle.success,
            custom_id=f"cat_adopt:{spawn_id}", disabled=adopted))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        return cls(int(match.group(1)))

    async def callback(self, interaction: discord.Interaction):
        try:
            res = await ca.adopt(self.spawn_id, interaction.user.id, _clone_id(interaction.client),
                                 interaction.guild_id)
        except Exception:
            logger.exception("[cat-adopt] adopt failed for spawn %s", self.spawn_id)
            await interaction.response.send_message("😿 Something went wrong, try again in a moment.", ephemeral=True)
            return
        if res.status == "limit":
            await interaction.response.send_message(
                f"🏠 Your home is full! You can keep up to {ca.MAX_CATS} cats. Release or trade one in **My cats**.",
                ephemeral=True)
            return
        if res.status in ("taken", "gone"):
            await interaction.response.send_message("😿 Someone else already adopted this cat." if res.status == "taken"
                                                    else "😿 This cat is no longer available.", ephemeral=True)
            return
        cat = res.cat
        embed = (interaction.message.embeds[0] if interaction.message and interaction.message.embeds
                 else discord.Embed(color=PINK))
        embed.description = (f"💖 **{cat.name}** was adopted by {interaction.user.mention}!\n"
                             f"{ca.rarity_badge(cat.rarity)} · {cat.trait}")
        await interaction.response.edit_message(embed=embed, view=spawn_view(self.spawn_id, adopted=True))
        await interaction.followup.send(
            f"🎉 You adopted **{cat.name}**, a {ca.rarity_badge(cat.rarity)} {cat.trait.lower()} cat! "
            + (f"\n{cat_wallet.reward_text(res.reward)}" if res.reward else "")
            + "\nOpen **My cats** to rename, trade or gift.", ephemeral=True)


class MyCatsButton(discord.ui.DynamicItem[discord.ui.Button], template=r"^cat_mine$"):
    def __init__(self):
        super().__init__(discord.ui.Button(label="My cats", emoji="🏠", style=discord.ButtonStyle.primary,
                                           custom_id="cat_mine"))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        return cls()

    async def callback(self, interaction: discord.Interaction):
        await open_collection(interaction)


# ── collection browser ────────────────────────────────────────────────────

async def open_collection(interaction: discord.Interaction) -> None:
    cats = await ca.list_cats(interaction.user.id, _clone_id(interaction.client))
    if not cats:
        await interaction.response.send_message(
            "🏠 You haven't adopted any cats yet. Tap **Adopt me!** on the next cat that appears. "
            "It's all just for fun, no money involved. 🐾", ephemeral=True)
        return
    view = CollectionView(interaction.client, interaction.user, cats)
    await view.refresh_wallet()
    view._build()
    await interaction.response.send_message(embed=await view.embed(), view=view, ephemeral=True)


class RenameModal(discord.ui.Modal, title="Rename your cat"):
    new_name = discord.ui.TextInput(label="New name", min_length=2, max_length=ca.NAME_MAX, placeholder="Mochi")

    def __init__(self, parent: "CollectionView"):
        super().__init__()
        self.parent = parent

    async def on_submit(self, interaction: discord.Interaction):
        cat = self.parent.current
        if not await ca.rename(cat.id, interaction.user.id, str(self.new_name)):
            await interaction.response.send_message("That name won't work. Use 2-20 letters, numbers or emoji.",
                                                    ephemeral=True)
            return
        await self.parent.reload(interaction.user.id)
        await interaction.response.edit_message(embed=await self.parent.embed(), view=self.parent)


class CollectionView(discord.ui.View):
    def __init__(self, client, user, cats: List[ca.Cat], index: int = 0):
        super().__init__(timeout=600)
        self.client, self.user, self.cats, self.index = client, user, cats, index
        self.confirm_release = False
        self.wallet = cat_wallet.Wallet()
        self._build()

    async def refresh_wallet(self) -> None:
        self.wallet = await cat_wallet.safe_wallet(self.user.id, _clone_id(self.client))

    @property
    def current(self) -> ca.Cat:
        return self.cats[self.index]

    async def embed(self) -> discord.Embed:
        e = await cat_embed_full(self.client, self.current,
                                 f"Cat {self.index + 1} of {len(self.cats)} · just for fun, no real money")
        e.add_field(name="Your purse", value=cat_wallet.wallet_text(self.wallet), inline=False)
        return e

    def _build(self) -> None:
        self.clear_items()
        many = len(self.cats) > 1
        for label, fn, style, disabled in (
            ("◀", self._prev, discord.ButtonStyle.secondary, not many),
            ("▶", self._next, discord.ButtonStyle.secondary, not many),
            ("Rename", self._rename, discord.ButtonStyle.primary, False),
            ("Trade / gift", self._trade, discord.ButtonStyle.success, False),
            ("Tap again to release" if self.confirm_release else "Release", self._release,
             discord.ButtonStyle.danger, False),
        ):
            b = discord.ui.Button(label=label, style=style, disabled=disabled)
            b.callback = fn
            self.add_item(b)
        have = self.wallet.diamonds
        for label, fn in ((f"{cat_wallet.DIAMOND} Convert 1", self._convert_one),
                          (f"{cat_wallet.DIAMOND} Convert all ({have})", self._convert_all)):
            b = discord.ui.Button(label=label, style=discord.ButtonStyle.secondary, disabled=have < 1, row=1)
            b.callback = fn
            self.add_item(b)

    async def _convert(self, interaction, count) -> None:
        try:
            result = await cat_wallet.convert(interaction.user.id, _clone_id(self.client), count)
        except Exception:
            logger.exception("[cat-wallet] convert failed")
            await interaction.response.send_message("😿 Something went wrong, try again in a moment.", ephemeral=True)
            return
        await self.refresh_wallet()
        self.confirm_release = False
        self._build()
        note = ("You don't have a diamond to convert." if result is None else
                f"{cat_wallet.DIAMOND} Converted! You now have {result.coins:,} coins.")
        await interaction.response.edit_message(content=note, embed=await self.embed(), view=self)

    async def _convert_one(self, interaction):
        await self._convert(interaction, 1)

    async def _convert_all(self, interaction):
        await self._convert(interaction, None)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("These are someone else's cats. Tap **My cats** for yours.",
                                                    ephemeral=True)
            return False
        return True

    async def reload(self, user_id: int) -> None:
        keep = self.current.id if self.cats else None
        self.cats = await ca.list_cats(user_id, _clone_id(self.client))
        self.index = next((i for i, c in enumerate(self.cats) if c.id == keep), min(self.index, max(len(self.cats) - 1, 0)))
        self.confirm_release = False
        self._build()

    async def _show(self, interaction):
        self._build()
        await interaction.response.edit_message(embed=await self.embed(), view=self)

    async def _prev(self, interaction):
        self.index, self.confirm_release = (self.index - 1) % len(self.cats), False
        await self._show(interaction)

    async def _next(self, interaction):
        self.index, self.confirm_release = (self.index + 1) % len(self.cats), False
        await self._show(interaction)

    async def _rename(self, interaction):
        await interaction.response.send_modal(RenameModal(self))

    async def _release(self, interaction):
        if not self.confirm_release:
            self.confirm_release = True
            await self._show(interaction)
            return
        cat = self.current
        await ca.release(cat.id, interaction.user.id)
        await self.reload(interaction.user.id)
        if not self.cats:
            await interaction.response.edit_message(
                content=f"👋 **{cat.name}** went off to find a new home. You have no cats left.", embed=None, view=None)
            return
        await interaction.response.edit_message(content=f"👋 **{cat.name}** went off to find a new home.",
                                                embed=await self.embed(), view=self)

    async def _trade(self, interaction):
        if interaction.guild is None or interaction.channel is None:
            await interaction.response.send_message("Trades happen in a server channel, not in DMs.", ephemeral=True)
            return
        view = TradeTargetView(self)
        await interaction.response.edit_message(
            content=f"🤝 Who do you want to trade **{self.current.name}** with? (Just for fun, no money.)",
            embed=None, view=view)


# ── trade flow ────────────────────────────────────────────────────────────

class TradeTargetView(discord.ui.View):
    def __init__(self, parent: CollectionView):
        super().__init__(timeout=300)
        self.parent = parent
        self.picker = discord.ui.UserSelect(placeholder="Pick a member", min_values=1, max_values=1)
        self.picker.callback = self._picked
        self.add_item(self.picker)
        back = discord.ui.Button(label="Back", style=discord.ButtonStyle.secondary)
        back.callback = self._back
        self.add_item(back)

    async def interaction_check(self, interaction):
        return await self.parent.interaction_check(interaction)

    async def _back(self, interaction):
        await interaction.response.edit_message(content=None, embed=await self.parent.embed(), view=self.parent)

    async def _picked(self, interaction: discord.Interaction):
        target = self.picker.values[0]
        if target.bot or target.id == interaction.user.id:
            await interaction.response.send_message("Pick another member (not yourself, not a bot).", ephemeral=True)
            return
        theirs = await ca.list_cats(target.id, _clone_id(interaction.client))
        view = TradeAskView(self.parent, target, theirs)
        await interaction.response.edit_message(
            content=(f"🤝 What should **{self.parent.current.name}** be swapped for? Pick one of {target.display_name}'s "
                     "cats, or just gift yours."), view=view)


class TradeAskView(discord.ui.View):
    def __init__(self, parent: CollectionView, target, their_cats: List[ca.Cat]):
        super().__init__(timeout=300)
        self.parent, self.target, self.their = parent, target, {c.id: c for c in their_cats[:24]}
        options = [discord.SelectOption(label="🎁 Just gift mine, ask for nothing", value=GIFT_VALUE)]
        options += [discord.SelectOption(label=f"{c.name} · {c.rarity.title()}", description=c.trait, value=str(c.id),
                                         emoji=ca.RARITY_EMOJI.get(c.rarity)) for c in self.their.values()]
        self.pick = discord.ui.Select(placeholder="Swap for…", options=options)
        self.pick.callback = self._chosen
        self.add_item(self.pick)
        back = discord.ui.Button(label="Back", style=discord.ButtonStyle.secondary)
        back.callback = self._back
        self.add_item(back)

    async def interaction_check(self, interaction):
        return await self.parent.interaction_check(interaction)

    async def _back(self, interaction):
        await interaction.response.edit_message(content=None, embed=await self.parent.embed(), view=self.parent)

    async def _chosen(self, interaction: discord.Interaction):
        mine = self.parent.current
        value = self.pick.values[0]
        want = None if value == GIFT_VALUE else self.their.get(int(value))
        if value != GIFT_VALUE and want is None:
            await interaction.response.send_message("That cat isn't available any more.", ephemeral=True)
            return
        offer = TradeOfferView(interaction.client, interaction.user, self.target, mine, want)
        embed = discord.Embed(title="🤝 Cat trade offer", color=PINK)
        embed.description = (
            f"{interaction.user.mention} offers {cat_line(mine)}\n"
            + (f"in exchange for {self.target.mention}'s {cat_line(want)}" if want else
               f"as a **gift** to {self.target.mention} 🎁")
            + "\n\nJust for fun, no real money. Nothing moves until it's accepted.")
        try:
            msg = await interaction.channel.send(
                content=self.target.mention, embed=embed, view=offer,
                allowed_mentions=discord.AllowedMentions(users=[self.target], everyone=False, roles=False))
        except discord.HTTPException:
            await interaction.response.edit_message(
                content="😿 I couldn't post the offer in this channel (I need Send Messages).", view=None)
            return
        offer.message = msg
        await interaction.response.edit_message(content=f"✅ Offer sent to {self.target.mention}. They have "
                                                f"{OFFER_TIMEOUT // 60} minutes to answer.", embed=None, view=None)


class TradeOfferView(discord.ui.View):
    """Public offer. Only the other person can accept or decline; only the sender can cancel."""

    def __init__(self, client, giver, taker, give: ca.Cat, take: Optional[ca.Cat]):
        super().__init__(timeout=OFFER_TIMEOUT)
        self.client, self.giver, self.taker, self.give, self.take = client, giver, taker, give, take
        self.message: Optional[discord.Message] = None
        for label, fn, style in (("Accept", self._accept, discord.ButtonStyle.success),
                                 ("Decline", self._decline, discord.ButtonStyle.danger),
                                 ("Cancel offer", self._cancel, discord.ButtonStyle.secondary)):
            b = discord.ui.Button(label=label, style=style)
            b.callback = fn
            self.add_item(b)

    async def _finish(self, interaction, text: str):
        self.stop()
        embed = interaction.message.embeds[0] if interaction.message and interaction.message.embeds else None
        if embed is not None:
            embed.description = text
        await interaction.response.edit_message(content=None, embed=embed, view=None)

    async def _accept(self, interaction: discord.Interaction):
        if interaction.user.id != self.taker.id:
            await interaction.response.send_message("Only the person this offer is for can accept it.", ephemeral=True)
            return
        status = await ca.trade(self.give.id, self.giver.id, self.take.id if self.take else None, self.taker.id,
                                _clone_id(self.client))
        if status == "ok":
            what = (f"{self.giver.mention} ↔ {self.taker.mention}: {cat_line(self.give)} for {cat_line(self.take)}"
                    if self.take else f"🎁 {self.giver.mention} gifted {cat_line(self.give)} to {self.taker.mention}")
            await self._finish(interaction, f"✅ **Trade done!** {what}. 💖")
        elif status == "limit":
            await self._finish(interaction, f"❌ {self.taker.mention}'s home is full ({ca.MAX_CATS} cats), so this "
                                            "gift can't go through.")
        else:
            await self._finish(interaction, "❌ That trade can't happen any more: one of the cats changed owners.")

    async def _decline(self, interaction):
        if interaction.user.id != self.taker.id:
            await interaction.response.send_message("Only the person this offer is for can decline it.", ephemeral=True)
            return
        await self._finish(interaction, f"❌ {self.taker.mention} declined the offer.")

    async def _cancel(self, interaction):
        if interaction.user.id != self.giver.id:
            await interaction.response.send_message("Only the sender can cancel this offer.", ephemeral=True)
            return
        await self._finish(interaction, f"↩️ {self.giver.mention} cancelled the offer.")

    async def on_timeout(self) -> None:
        if self.message is not None:
            try:
                await self.message.edit(content=None, view=None,
                                        embed=discord.Embed(title="🤝 Cat trade offer", color=PINK,
                                                            description="⏰ This offer expired."))
            except discord.HTTPException:
                pass


DYNAMIC_ITEMS = (AdoptButton, MyCatsButton)
