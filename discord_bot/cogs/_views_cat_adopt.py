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

from modules import cat_filter
from modules import cat_quests
from modules import cat_adopt as ca
from modules import cat_announce
from modules import cat_ids
from modules import cat_log
from modules import cat_setup as cs
from modules import cat_pool
from modules import cat_rank as cr
from modules import cat_wallet

logger = logging.getLogger(__name__)

PINK = discord.Color.from_rgb(255, 182, 193)
OFFER_TIMEOUT = 600
GIFT_VALUE = "gift"
JUMP_PAGE = 25                   # a select holds 25 options; the jump list pages through bigger collections
ASK_PAGE = 23                    # their cats per page in the trade picker (+1 slot for "just gift mine")


def _clone_id(client):
    return getattr(client, "clone_id", None)


def _event(interaction, cat: ca.Cat, user=None) -> dict:
    """The facts for one storage-channel log line (see modules/cat_log.py)."""
    user = user or interaction.user
    guild = interaction.guild
    return dict(guild_id=getattr(guild, "id", None), guild_name=getattr(guild, "name", None), user_id=user.id,
                user_name=str(user), pool_id=cat.pool_id, cat_name=cat.name, rarity=cat.rarity, trait=cat.trait)


def _other(user, cat: Optional[ca.Cat]) -> dict:
    out = dict(other_id=user.id, other_name=str(user))
    if cat is not None:
        out.update(other_cat_name=cat.name, other_rarity=cat.rarity, other_trait=cat.trait)
    return out


def cat_line(cat: ca.Cat) -> str:
    return f"**{cat.name}** · {ca.rarity_badge(cat.rarity)} · {cat.trait}"


async def cat_embed_full(client, cat: ca.Cat, position: str = "") -> discord.Embed:
    e = discord.Embed(title=f"{ca.RARITY_EMOJI.get(cat.rarity, '🐱')} {cat.name}", color=PINK)
    e.description = (f"**Rarity:** {ca.rarity_badge(cat.rarity)}\n**Personality:** {cat.trait}\n"
                     f"**Rank:** {cr.badge(cat.cat_rank)}\n**Times traded:** {cat.traded_count}\n"
                     f"**Cat ID:** #{cat.id}")
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
        cat_log.record(interaction.client, "adopt", **_event(interaction, cat))
        embed = (interaction.message.embeds[0] if interaction.message and interaction.message.embeds
                 else discord.Embed(color=PINK))
        embed.description = cat_announce.adopt_text(interaction.user.mention, cat.name, cat.id,
                                                    ca.rarity_badge(cat.rarity), cat.trait, cat.rarity)
        await interaction.response.edit_message(embed=embed, view=spawn_view(self.spawn_id, adopted=True))
        earned: list = []
        badge_lines = await cat_quests.on_adopt(interaction.user.id, _clone_id(interaction.client), cat.rarity, earned)
        await interaction.followup.send(
            f"🎉 You adopted **{cat.name}**, a {ca.rarity_badge(cat.rarity)} {cat.trait.lower()} cat! "
            + (f"\n{cat_wallet.reward_text(res.reward)}" if res.reward else "")
            + (("\n🏅 Badge earned: " + ", ".join(badge_lines)) if badge_lines else "")
            + "\nOpen **My cats** to rename, trade or gift.", ephemeral=True)
        await cat_announce.announce_badges(interaction, earned)


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
        self.all_cats = list(cats)                  # everything the person owns; self.cats is the filtered view of it
        self.filter = cat_filter.CatFilter()
        self.confirm_release = False
        self.confirm_rank_for = None        # (cat id, rank) the person tapped once; a second tap spends the coins
        self.wallet = cat_wallet.Wallet()
        self._build()

    async def refresh_wallet(self) -> None:
        self.wallet = await cat_wallet.safe_wallet(self.user.id, _clone_id(self.client))

    @property
    def current(self) -> ca.Cat:
        return self.cats[self.index]

    async def embed(self) -> discord.Embed:
        e = await cat_embed_full(self.client, self.current,
                                 f"Cat {self.index + 1} of {len(self.cats)}"
                                 + (f" (filtered from {len(self.all_cats)})" if len(self.cats) != len(self.all_cats) else "")
                                 + " · just for fun, no real money")
        e.add_field(name="Your purse", value=cat_wallet.wallet_text(self.wallet), inline=False)
        if getattr(self, "trade_id", None) is None:
            self.trade_id = await cat_ids.safe_get(self.user.id, _clone_id(self.client)) or ""
        if self.trade_id:
            e.add_field(name="🆔 Your trade ID (yours for good)", value=f"`{self.trade_id}`\n"
                        "Members of your server can use it to offer you a trade, and ⭐ Premium servers can use it from any server. Nothing happens until you accept.",
                        inline=False)
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
        if len(self.cats) > 1:
            page = self.index // JUMP_PAGE
            chunk = self.cats[page * JUMP_PAGE:(page + 1) * JUMP_PAGE]
            pages = -(-len(self.cats) // JUMP_PAGE)
            jump = discord.ui.Select(
                placeholder=(f"Jump to a cat ({page * JUMP_PAGE + 1}-{page * JUMP_PAGE + len(chunk)} of {len(self.cats)})"
                             if pages > 1 else "Jump to a cat"),
                options=[discord.SelectOption(label=f"{page * JUMP_PAGE + i + 1}. {c.name} \u00b7 {c.rarity.title()}"[:100],
                                              value=str(page * JUMP_PAGE + i), emoji=ca.RARITY_EMOJI.get(c.rarity),
                                              description=(cr.badge(c.cat_rank) if c.cat_rank else None),
                                              default=(page * JUMP_PAGE + i) == self.index)
                         for i, c in enumerate(chunk)], row=2)
            jump.callback = self._jump
            self.add_item(jump)
        fb = discord.ui.Button(label=(f"Filter ({self.filter.active})" if self.filter.active else "Filter / search"),
                               emoji="\U0001F50D", style=discord.ButtonStyle.secondary, row=3)
        fb.callback = self._open_filter
        self.add_item(fb)
        have = self.wallet.diamonds
        for label, fn in ((f"{cat_wallet.DIAMOND} Convert 1", self._convert_one),
                          (f"{cat_wallet.DIAMOND} Convert all ({have})", self._convert_all)):
            b = discord.ui.Button(label=label, style=discord.ButtonStyle.secondary, disabled=have < 1, row=1)
            b.callback = fn
            self.add_item(b)
        cost = cr.next_cost(self.current)
        if cost is None:
            rb = discord.ui.Button(label="Top rank \U0001F31F", style=discord.ButtonStyle.secondary, disabled=True, row=1)
        else:
            armed = self.confirm_rank_for == (self.current.id, self.current.cat_rank)
            rb = discord.ui.Button(
                label=(f"Tap again: spend {cost:,} coins" if armed else f"\u2B50 Rank up \u00b7 {cost:,} coins")[:80],
                style=discord.ButtonStyle.danger if armed else discord.ButtonStyle.primary, row=1)
        rb.callback = self._rank_up
        self.add_item(rb)

    async def _rank_up(self, interaction):
        cat = self.current
        key = (cat.id, cat.cat_rank)
        if self.confirm_rank_for != key:                     # first tap: just arm the button
            self.confirm_rank_for = key
            await self._show(interaction)
            return
        self.confirm_rank_for = None
        try:
            res = await cr.rank_up(cat.id, interaction.user.id, _clone_id(self.client), cat.cat_rank)
        except Exception:
            logger.exception("[cat-rank] rank up failed for cat %s", cat.id)
            await interaction.response.send_message("😿 Something went wrong, try again in a moment.", ephemeral=True)
            return
        note = {
            "ok": f"\u2B50 **{cat.name}** is now {cr.badge(res.rank)}! (spent {res.cost:,} coins, {res.coins:,} left)",
            "poor": f"You need {res.cost:,} coins to rank **{cat.name}** up and you have {res.coins:,}. "
                    "Adopt cats, sell a cat or convert a diamond to earn more.",
            "listed": f"**{cat.name}** is for sale. Take it off the market first (Market button).",
            "max": f"**{cat.name}** is already at the top rank.",
            "stale": f"**{cat.name}**'s rank just changed. Have a look and try again.",
            "not_yours": "That cat isn't yours any more.",
        }.get(res.status, "Something went wrong, try again in a moment.")
        await self.reload(interaction.user.id)
        await self.refresh_wallet()
        self._build()
        await interaction.response.edit_message(content=note, embed=await self.embed(), view=self)
        if res.status == "ok":
            await cat_announce.announce(interaction, cat_announce.rank_up_text(
                interaction.user.mention, cat.name, cat.id, res.rank), gold=res.rank >= cr.MAX_RANK)
            await cat_quests.on_rank(interaction.user.id, _clone_id(self.client))

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
        self.all_cats = await ca.list_cats(user_id, _clone_id(self.client))
        self._reapply()
        self.index = next((i for i, c in enumerate(self.cats) if c.id == keep), min(self.index, max(len(self.cats) - 1, 0)))
        self.confirm_release = False
        self._build()

    def _reapply(self) -> None:
        """Rebuild the visible list from everything owned. An untouched filter keeps the original order. If nothing
        matches any more (the last match was sold, say) the filter is cleared so the screen is never empty."""
        if self.filter == cat_filter.CatFilter():
            self.cats = list(self.all_cats)
            return
        shown = cat_filter.apply(self.all_cats, self.filter)
        if not shown and self.all_cats:
            self.filter = cat_filter.CatFilter()
            shown = list(self.all_cats)
        self.cats = shown

    async def _open_filter(self, interaction):
        self.confirm_release = False
        view = FilterView(self)
        await interaction.response.edit_message(content=view.status(), embed=None, view=view)

    async def _show(self, interaction):
        self._build()
        await interaction.response.edit_message(embed=await self.embed(), view=self)

    async def _jump(self, interaction):
        self.index, self.confirm_release = int(interaction.data["values"][0]), False
        await self._show(interaction)

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
        if await ca.release(cat.id, interaction.user.id):
            cat_log.record(interaction.client, "release", **_event(interaction, cat))
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
        cat = self.current
        await interaction.response.edit_message(
            content=(f"🤝 **{cat.name}** (cat #{cat.id}, {cr.badge(cat.cat_rank)}): pick a member to trade or gift with, "
                     "use Trade by ID, or sell it for coins. (Trades and gifts are just for fun.)"),
            embed=await cat_embed_full(self.client, cat, "This is the cat you are trading or selling"), view=view)


# ── trade flow ────────────────────────────────────────────────────────────

class TradeTargetView(discord.ui.View):
    def __init__(self, parent: CollectionView):
        super().__init__(timeout=300)
        self.parent = parent
        self.picker = discord.ui.UserSelect(placeholder="Pick a member", min_values=1, max_values=1)
        self.picker.callback = self._picked
        self.add_item(self.picker)
        by_id = discord.ui.Button(label="Trade by ID", emoji="🆔", style=discord.ButtonStyle.primary)
        by_id.callback = self._by_id
        self.add_item(by_id)
        sell = discord.ui.Button(label="Sell for coins", emoji="\U0001F3F7\uFE0F", style=discord.ButtonStyle.success)
        sell.callback = self._sell
        self.add_item(sell)
        back = discord.ui.Button(label="Back", style=discord.ButtonStyle.secondary)
        back.callback = self._back
        self.add_item(back)

    async def interaction_check(self, interaction):
        return await self.parent.interaction_check(interaction)

    async def _back(self, interaction):
        await interaction.response.edit_message(content=None, embed=await self.parent.embed(), view=self.parent)

    async def _sell(self, interaction: discord.Interaction):
        from discord_bot.cogs._views_cat_market import SellModal
        await interaction.response.send_modal(SellModal(self.parent.current))

    async def _by_id(self, interaction: discord.Interaction):
        # Everyone can type an ID. Whether it works across servers is decided after the ID is read (TradeIdModal).
        await interaction.response.send_modal(TradeIdModal(self.parent))

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


REMOTE_COOLDOWN = 60.0                 # seconds between two cross-server offers from the same person
REMOTE_PENDING_MAX = 3                 # open cross-server offers one person can have waiting at the same time
_remote_at: dict = {}
_remote_pending: dict = {}


async def _member_of(guild, user_id: int):
    """The person as a member of this server, or None (not here / can't tell). Never raises."""
    try:
        m = guild.get_member(user_id)
        if m is not None:
            return m
        return await guild.fetch_member(user_id)
    except discord.HTTPException:          # NotFound (not a member) is one too
        return None
    except Exception:
        logger.debug("[cat] member lookup failed", exc_info=True)
        return None


class TradeIdModal(discord.ui.Modal, title="Trade by ID"):
    trade_id = discord.ui.TextInput(label="Their trade ID", min_length=7, max_length=120,
                                    placeholder="142574D")

    def __init__(self, parent: "CollectionView"):
        super().__init__()
        self.parent = parent

    async def on_submit(self, interaction: discord.Interaction):
        client = interaction.client
        guild = interaction.guild
        typed = str(self.trade_id)
        if cat_ids.parse(typed) is None:
            # Wrong shape or a wrong check letter: a typo, caught without looking anything up (and not counted as a guess).
            await interaction.response.send_message(
                "🤔 That doesn't look like a trade ID. It is 6 digits and a letter, like `142574D`. Check it and try again "
                "(ask them to open **My cats** and copy theirs).", ephemeral=True)
            return
        if not cat_ids.lookup_allowed(interaction.user.id):
            await interaction.response.send_message(
                "😺 That's a lot of IDs that don't exist. Please wait a little while and try again.", ephemeral=True)
            return
        ident = await cat_ids.find_identity(typed)      # looks across the main bot and every clone
        owner_id, their_clone = ident if ident else (None, None)
        member = None
        if owner_id is not None and owner_id != interaction.user.id and guild is not None:
            member = await _member_of(guild, owner_id)
        premium = await cs.is_premium(guild.id, _clone_id(client)) if guild else False
        if owner_id is not None and owner_id != interaction.user.id and member is None \
                and not cat_ids.can_trade_across_servers(premium):
            # Free server and the ID isn't a member here. Unknown ID and outside person get the SAME answer, so
            # nobody can use this box to find out whether some ID exists.
            cat_ids.note_failed_lookup(interaction.user.id)         # same answer as "unknown ID", so it counts the same
            await interaction.response.send_message(
                "🤔 I can't find that ID **in this server**. Free servers trade by ID with members of this server only "
                "(ask them to open **My cats** and copy their ID). ⭐ **Premium** servers can trade with anyone from "
                "any server.", ephemeral=True)
            return
        if owner_id is None:
            cat_ids.note_failed_lookup(interaction.user.id)
            await interaction.response.send_message(
                "🤔 I don't know that ID. It is 6 digits and a letter (like `142574D`); "
                "ask them to open **My cats** and copy theirs." if premium else
                "🤔 I can't find that ID **in this server**. Free servers trade by ID with members of this server only "
                "(ask them to open **My cats** and copy their ID). ⭐ **Premium** servers can trade with anyone from "
                "any server.", ephemeral=True)
            return
        if owner_id == interaction.user.id:
            await interaction.response.send_message("That's your own ID 😄", ephemeral=True)
            return
        if member is not None:
            # Same server: works for free and Premium alike, and runs exactly like picking them from the member list
            # (the offer is posted here, nothing is sent by DM).
            if member.bot:
                await interaction.response.send_message("That ID doesn't belong to a person.", ephemeral=True)
                return
            theirs = await ca.list_cats(owner_id, their_clone)
            await interaction.response.edit_message(
                content=(f"🆔 Trading with **{member.display_name}** (a member of this server). What should "
                         f"**{self.parent.current.name}** be swapped for? Or just gift yours."),
                embed=None, view=TradeAskView(self.parent, member, theirs, their_clone=their_clone))
            return
        try:
            target = await client.fetch_user(owner_id)
        except discord.HTTPException:
            target = None
        if target is None or target.bot:
            await interaction.response.send_message("😿 I couldn't find that person right now.", ephemeral=True)
            return
        theirs = await ca.list_cats(owner_id, their_clone)
        view = TradeAskView(self.parent, target, theirs, remote=True, their_clone=their_clone)
        await interaction.response.edit_message(
            content=(f"🌍 Trading with **{target.display_name}** (from any server). What should "
                     f"**{self.parent.current.name}** be swapped for? Or just gift yours."), embed=None, view=view)


_UNSET = object()


class TradeAskView(discord.ui.View):
    def __init__(self, parent: CollectionView, target, their_cats: List[ca.Cat], remote: bool = False,
                 their_clone=_UNSET):
        super().__init__(timeout=300)
        self.remote = remote
        # which bot's collection their cats are in: this one unless the ID they gave belongs to another bot
        self.their_clone = _clone_id(getattr(parent, "client", None)) if their_clone is _UNSET else their_clone
        self.parent, self.target = parent, target
        self.their = {c.id: c for c in their_cats}             # every cat; the select shows one page at a time
        self.page = 0
        self._build()

    def _build(self) -> None:
        self.clear_items()
        cats = list(self.their.values())
        pages = max(1, -(-len(cats) // ASK_PAGE))
        self.page = min(self.page, pages - 1)
        chunk = cats[self.page * ASK_PAGE:(self.page + 1) * ASK_PAGE]
        options = [discord.SelectOption(label="\U0001F381 Just gift mine, ask for nothing", value=GIFT_VALUE)]
        options += [discord.SelectOption(label=f"{c.name} \u00b7 {c.rarity.title()}", description=c.trait, value=str(c.id),
                                         emoji=ca.RARITY_EMOJI.get(c.rarity)) for c in chunk]
        self.pick = discord.ui.Select(
            placeholder=(f"Swap for\u2026 (cats {self.page * ASK_PAGE + 1}-{self.page * ASK_PAGE + len(chunk)} of {len(cats)})"
                         if pages > 1 else "Swap for\u2026"), options=options)
        self.pick.callback = self._chosen
        self.add_item(self.pick)
        if pages > 1:
            for label, delta, off in (("\u25C0", -1, self.page <= 0), ("\u25B6", 1, self.page >= pages - 1)):
                b = discord.ui.Button(label=label, style=discord.ButtonStyle.secondary, disabled=off)
                b.callback = (lambda d: (lambda i: self._turn(i, d)))(delta)
                self.add_item(b)
        back = discord.ui.Button(label="Back", style=discord.ButtonStyle.secondary)
        back.callback = self._back
        self.add_item(back)

    async def _turn(self, interaction, delta: int):
        self.page += delta
        self._build()
        await interaction.response.edit_message(view=self)

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
        offer = TradeOfferView(interaction.client, interaction.user, self.target, mine, want, remote=self.remote,
                               taker_clone=self.their_clone)
        embed = discord.Embed(title="🤝 Cat trade offer", color=PINK)
        where = f" from **{interaction.guild.name}**" if self.remote and interaction.guild else ""
        embed.description = (
            f"{interaction.user.mention}{where} offers {cat_line(mine)}\n"
            + (f"in exchange for {self.target.mention}'s {cat_line(want)}" if want else
               f"as a **gift** to {self.target.mention} 🎁")
            + "\n\nJust for fun, no real money. Nothing moves until it's accepted.")
        if self.remote:
            premium = await cs.is_premium(interaction.guild.id, _clone_id(interaction.client)) if interaction.guild else False
            if not cat_ids.can_trade_across_servers(premium):
                await interaction.response.edit_message(content="⭐ Trading by ID needs a Premium server.", view=None)
                return
            import time
            wait = REMOTE_COOLDOWN - (time.monotonic() - _remote_at.get(interaction.user.id, -1e9))
            if wait > 0:
                await interaction.response.send_message(f"😺 Easy there, another offer in {wait:.0f}s.", ephemeral=True)
                return
            if _remote_pending.get(self.target.id, 0) >= REMOTE_PENDING_MAX:
                await interaction.response.send_message(
                    f"📬 {self.target.display_name} already has {REMOTE_PENDING_MAX} offers waiting. Try again later.",
                    ephemeral=True)
                return
            try:
                msg = await self.target.send(embed=embed, view=offer)
            except discord.HTTPException:
                await interaction.response.edit_message(
                    content="😿 I couldn't message them (their DMs are closed). Ask them to open DMs, or trade in a "
                            "server you share.", view=None)
                return
            _remote_at[interaction.user.id] = time.monotonic()
            _remote_pending[self.target.id] = _remote_pending.get(self.target.id, 0) + 1
            offer.counted = True
            offer.message = msg
            await interaction.response.edit_message(
                content=f"✅ Offer sent to {self.target.display_name} by DM. They have {OFFER_TIMEOUT // 60} minutes to "
                        "answer, and I'll tell you what they say.", embed=None, view=None)
            return
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

    def __init__(self, client, giver, taker, give: ca.Cat, take: Optional[ca.Cat], remote: bool = False,
                 taker_clone=_UNSET):
        super().__init__(timeout=OFFER_TIMEOUT)
        self.client, self.giver, self.taker, self.give, self.take = client, giver, taker, give, take
        self.taker_clone = _clone_id(client) if taker_clone is _UNSET else taker_clone
        self.remote, self.counted = remote, False
        self.message: Optional[discord.Message] = None
        for label, fn, style in (("Accept", self._accept, discord.ButtonStyle.success),
                                 ("Decline", self._decline, discord.ButtonStyle.danger),
                                 ("Cancel offer", self._cancel, discord.ButtonStyle.secondary)):
            b = discord.ui.Button(label=label, style=style)
            b.callback = fn
            self.add_item(b)

    def _release_slot(self) -> None:
        if self.counted:
            self.counted = False
            left = _remote_pending.get(self.taker.id, 1) - 1
            if left > 0:
                _remote_pending[self.taker.id] = left
            else:
                _remote_pending.pop(self.taker.id, None)

    async def _tell_giver(self, text: str) -> None:
        """Cross-server offers: the sender isn't in the same channel, so they get the answer by DM."""
        if self.remote:
            try:
                await self.giver.send(text)
            except discord.HTTPException:
                pass

    async def _finish(self, interaction, text: str):
        self.stop()
        self._release_slot()
        await self._tell_giver(text)
        embed = interaction.message.embeds[0] if interaction.message and interaction.message.embeds else None
        if embed is not None:
            embed.description = text
        await interaction.response.edit_message(content=None, embed=embed, view=None)

    async def _accept(self, interaction: discord.Interaction):
        if interaction.user.id != self.taker.id:
            await interaction.response.send_message("Only the person this offer is for can accept it.", ephemeral=True)
            return
        status = await ca.trade(self.give.id, self.giver.id, self.take.id if self.take else None, self.taker.id,
                                _clone_id(self.client), taker_clone_id=self.taker_clone)
        if status == "ok":
            cat_log.record(self.client, "trade" if self.take else "gift", **_event(interaction, self.give, self.giver),
                           **_other(self.taker, self.take))
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
        self._release_slot()
        await self._tell_giver("⏰ Your cat trade offer expired without an answer.")
        if self.message is not None:
            try:
                await self.message.edit(content=None, view=None,
                                        embed=discord.Embed(title="🤝 Cat trade offer", color=PINK,
                                                            description="⏰ This offer expired."))
            except discord.HTTPException:
                pass


from discord_bot.cogs import _views_cat_market as _market  # noqa: E402  (needs the names defined above)

DYNAMIC_ITEMS = (AdoptButton, MyCatsButton) + _market.DYNAMIC_ITEMS


# ── My cats: search, filters, sorting ──────────────────────────────────────

class SearchModal(discord.ui.Modal, title="Search your cats"):
    text = discord.ui.TextInput(label="Cat name or cat #ID", required=False, max_length=cat_filter.QUERY_MAX,
                                placeholder="leave empty to clear the search")

    def __init__(self, parent: "FilterView"):
        super().__init__()
        self.parent = parent
        self.text.default = parent.f.query or None

    async def on_submit(self, interaction: discord.Interaction):
        self.parent.f = self.parent.f.with_(query=cat_filter.clean_query(self.text.value))
        self.parent._build()
        await interaction.response.edit_message(content=self.parent.status(), view=self.parent)


class FilterView(discord.ui.View):
    """Pick a rarity, a trait, a sort order or a name search, then Done. Nothing is changed in the collection."""

    def __init__(self, parent: CollectionView):
        super().__init__(timeout=300)
        self.parent = parent
        self.f = parent.filter                     # a draft: the real filter only changes when Done is tapped
        self._build()

    def _matches(self) -> int:
        return len(cat_filter.apply(self.parent.all_cats, self.f))

    def status(self) -> str:
        f = self.f
        n, total = self._matches(), len(self.parent.all_cats)
        return (f"\U0001F50D **Filter your cats**: {n} of {total} match ({cat_filter.describe(f)}).\n"
                + ("Tap **Done** to see them." if n else "No cat matches. Change a filter or tap **Clear**."))

    def _build(self) -> None:
        self.clear_items()
        f = self.f
        rar = discord.ui.Select(placeholder="Rarity", row=0, options=[
            discord.SelectOption(label="Any rarity", value="any", default=f.rarity is None)] + [
            discord.SelectOption(label=r.title(), value=r, emoji=ca.RARITY_EMOJI.get(r), default=f.rarity == r)
            for r in ca.RARITY_RANK])
        rar.callback = self._rarity
        trait = discord.ui.Select(placeholder="Trait", row=1, options=[
            discord.SelectOption(label="Any trait", value="any", default=f.trait is None)] + [
            discord.SelectOption(label=t, value=t, default=f.trait == t) for t in ca.TRAITS])
        trait.callback = self._trait
        sort = discord.ui.Select(placeholder="Sort by", row=2, options=[
            discord.SelectOption(label=label, value=key, default=f.sort == key) for key, label in cat_filter.SORTS.items()])
        sort.callback = self._sort
        for item in (rar, trait, sort):
            self.add_item(item)
        for label, fn, style, disabled in (
            ("Search name / ID", self._search, discord.ButtonStyle.secondary, False),
            ("Clear", self._clear, discord.ButtonStyle.secondary, f == cat_filter.CatFilter()),
            ("Done", self._done, discord.ButtonStyle.success, self._matches() == 0),
        ):
            b = discord.ui.Button(label=label, style=style, disabled=disabled, row=3)
            b.callback = fn
            self.add_item(b)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await self.parent.interaction_check(interaction)

    async def _update(self, interaction, **kw):
        self.f = self.f.with_(**kw)
        self._build()
        await interaction.response.edit_message(content=self.status(), view=self)

    async def _rarity(self, interaction):
        v = interaction.data["values"][0]
        await self._update(interaction, rarity=None if v == "any" else v)

    async def _trait(self, interaction):
        v = interaction.data["values"][0]
        await self._update(interaction, trait=None if v == "any" else v)

    async def _sort(self, interaction):
        v = interaction.data["values"][0]
        await self._update(interaction, sort=v if v in cat_filter.SORTS else cat_filter.DEFAULT_SORT)

    async def _search(self, interaction):
        await interaction.response.send_modal(SearchModal(self))

    async def _clear(self, interaction):
        self.f = cat_filter.CatFilter()
        self._build()
        await interaction.response.edit_message(content=self.status(), view=self)

    async def _done(self, interaction):
        self.parent.filter = self.f
        self.parent._reapply()
        self.parent.index = 0
        self.parent._build()
        await interaction.response.edit_message(content=None, embed=await self.parent.embed(), view=self.parent)
