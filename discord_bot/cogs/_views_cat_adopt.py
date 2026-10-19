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
from typing import Dict, List, Optional

import discord
from discord_bot.cogs import _cat_visibility as vis

from modules import cat_care
from modules import cat_filter
from modules import cat_help
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


def _cat_body(cat: ca.Cat) -> str:
    return (f"**Rarity:** {ca.rarity_badge(cat.rarity)}\n**Personality:** {cat.trait}\n"
            f"**Rank:** {cr.badge(cat.cat_rank)}\n**Times traded:** {cat.traded_count}\n"
            f"**Cat ID:** #{cat.id}\n\n{cat_care.status_lines(cat)}")


def _cat_title(cat: ca.Cat) -> str:
    return f"{ca.RARITY_EMOJI.get(cat.rarity, '🐱')} {cat.name}"


async def cat_image_url(client, cat: ca.Cat) -> Optional[str]:
    """The cat's picture, or None when it can't be found (a missing picture never breaks a screen)."""
    try:
        return await cat_pool.url_for(client, cat.pool_id) or None
    except Exception:
        return None


async def cat_embed_full(client, cat: ca.Cat, position: str = "") -> discord.Embed:
    """Classic embed card of a cat. No live screen uses it since Phase 5 (public posts are V2 cards now); kept as the
    old card layout. The V2 screens use `_cat_body`."""
    e = discord.Embed(title=_cat_title(cat), color=PINK)
    e.description = _cat_body(cat)
    url = await cat_image_url(client, cat)
    if url:
        e.set_image(url=url)
    if position:
        e.set_footer(text=position)
    return e


# ── Components V2 building blocks ─────────────────────────────────────────
# One Container per screen: text, an optional picture, a Separator, then the ActionRows. A V2 message can't carry
# `content=`, `embed=` or `embeds=`, so notes and results live in the card's text.

def _box(text: str, rows=(), image: Optional[str] = None, alt: str = "Cat",
         colour: Optional[discord.Color] = None) -> discord.ui.Container:
    box = discord.ui.Container(accent_colour=colour or PINK)
    box.add_item(discord.ui.TextDisplay(text[:3900]))
    if image:
        box.add_item(discord.ui.MediaGallery(discord.MediaGalleryItem(str(image), description=alt[:256])))
    if rows:
        box.add_item(discord.ui.Separator())
        for row in rows:
            box.add_item(row)
    return box


def _notice(text: str) -> discord.ui.LayoutView:
    """A one-card V2 message with no buttons (results, 'no cats left', expired offers)."""
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(_box(text))
    return view


def view_text(view: discord.ui.BaseView) -> str:
    """All the text on a V2 screen, for tests and logs."""
    return "\n".join(c.content for c in view.walk_children() if isinstance(c, discord.ui.TextDisplay))


# ── Public spawn post (Components V2) ─────────────────────────────────────
# New spawn posts are one V2 card: heading, a line of text, the cat picture, and the five persistent buttons. Spawn
# posts that were sent before this change are still classic embeds, and they stay in channels (and keep working), so
# every edit of a spawn message checks which kind it is. A message can't change kind after posting: classic messages
# are edited with an embed, V2 messages with a V2 view.

NEXT_CAT_MARK = "**⏰ Next cat**"


message_is_v2 = vis.message_is_v2


def next_cat_tail(next_at) -> str:
    """The 'Next cat' lines of a spawn card (Discord shows <t:..> in each viewer's own time zone)."""
    if next_at is None:
        return ""
    ts = int(next_at.timestamp())
    return f"{NEXT_CAT_MARK}\nAround <t:{ts}:t> (<t:{ts}:R>)"


def spawn_layout(spawn_id: Optional[int], heading: str, body: str = "", image=None, tail: str = "",
                 adopted: bool = False, gone: bool = False) -> discord.ui.LayoutView:
    """The V2 spawn card. Without a `spawn_id` (a picture that can't be adopted) it has no buttons."""
    text = heading + (f"\n{body}" if body else "") + (f"\n{tail}" if tail else "")
    view = discord.ui.LayoutView(timeout=None)
    box = discord.ui.Container(accent_colour=PINK)
    box.add_item(discord.ui.TextDisplay(text[:3900]))
    if image:
        box.add_item(discord.ui.MediaGallery(discord.MediaGalleryItem(str(image), description="Cat")))
    if spawn_id is not None:
        box.add_item(discord.ui.Separator())
        box.add_item(discord.ui.ActionRow(AdoptButton(spawn_id, adopted=adopted, gone=gone), MyCatsButton(),
                                          _market.MarketButton(), HelpButton(), MenuButton()))
    view.add_item(box)
    return view


def spawn_post(spawn_id: Optional[int], title: str, image, next_at=None, description: Optional[str] = None):
    """A brand-new public spawn post."""
    return spawn_layout(spawn_id, f"## {title}", description or "", image, next_cat_tail(next_at))


def _walk_components(components):
    for c in components or ():
        yield c
        yield from _walk_components(getattr(c, "children", None))


def spawn_parts(message) -> tuple:
    """(heading, next-cat tail, picture URL) read back from a V2 spawn message, so an edit keeps the original title,
    the 'Next cat' lines and the picture. Anything missing comes back empty. Never raises."""
    heading, tail, image = "", "", None
    try:
        for c in _walk_components(getattr(message, "components", None)):
            kind = getattr(c, "type", None)
            if kind == discord.ComponentType.text_display and not heading:
                lines = str(getattr(c, "content", "")).split("\n")
                if lines and lines[0].startswith("## "):
                    heading = lines[0]
                for i, line in enumerate(lines):
                    if line.startswith(NEXT_CAT_MARK):
                        tail = "\n".join(lines[i:])
                        break
            elif kind == discord.ComponentType.media_gallery and image is None:
                items = getattr(c, "items", None) or []
                if items:
                    image = items[0].media.url
    except Exception:
        logger.debug("[cat-adopt] couldn't read the spawn message", exc_info=True)
    return heading, tail, image


def spawn_edit_kwargs(message, spawn_id: int, body: str, adopted: bool = False, gone: bool = False) -> dict:
    """What to pass to `message.edit(...)` / `response.edit_message(...)` to show `body` on a spawn post, for either kind
    of spawn message: a classic embed message keeps its embed (new text) and classic buttons; a V2 message gets a
    fresh V2 card with the same title, picture and 'Next cat' lines."""
    if message_is_v2(message):
        heading, tail, image = spawn_parts(message)
        return {"view": spawn_layout(spawn_id, heading or "## 🐱 A cat", body, image, tail, adopted=adopted, gone=gone),
                "allowed_mentions": discord.AllowedMentions.none()}
    embeds = getattr(message, "embeds", None)
    embed = discord.Embed.from_dict(embeds[0].to_dict()) if embeds else discord.Embed(color=PINK)
    embed.description = body
    return {"embed": embed, "view": spawn_view(spawn_id, adopted=adopted, gone=gone)}


# ── Adopt button on a spawn ───────────────────────────────────────────────

def spawn_view(spawn_id: int, adopted: bool = False, gone: bool = False) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(AdoptButton(spawn_id, adopted=adopted, gone=gone))
    view.add_item(MyCatsButton())
    view.add_item(_market.MarketButton())
    view.add_item(HelpButton())
    view.add_item(MenuButton())
    return view


class MenuButton(discord.ui.DynamicItem[discord.ui.Button], template=r"^cat_menu$"):
    """Opens the same private /cat menu from a spawn, so a cat that just appeared and /cat are one flow."""

    def __init__(self):
        super().__init__(discord.ui.Button(label="Menu", emoji="🐱", style=discord.ButtonStyle.primary,
                                           custom_id="cat_menu"))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        return cls()

    async def callback(self, interaction: discord.Interaction):
        from discord_bot.cogs._views_cat_hub import open_hub
        await open_hub(interaction)


class HelpButton(discord.ui.DynamicItem[discord.ui.Button], template=r"^cat_help$"):
    """Explains the cat game to the person who tapped it. Private to them, changes nothing."""

    def __init__(self):
        super().__init__(discord.ui.Button(label="Help", emoji="❓", style=discord.ButtonStyle.secondary,
                                           custom_id="cat_help"))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        return cls()

    async def callback(self, interaction: discord.Interaction):
        await vis.send(interaction, view=HelpView())


class HelpView(discord.ui.LayoutView):
    """Short help for the cat game: a few lines per topic, switched with a menu. Private to whoever tapped Help."""

    def __init__(self, key: Optional[str] = None):
        super().__init__(timeout=300)
        self.key = key
        box = discord.ui.Container(accent_colour=PINK)
        box.add_item(discord.ui.Section(discord.ui.TextDisplay(self.text()),
                                        accessory=discord.ui.Thumbnail(cat_help.RING_URL)))
        box.add_item(discord.ui.Separator())
        box.add_item(discord.ui.ActionRow(HelpTopicSelect(key)))
        self.add_item(box)

    def text(self) -> str:
        rows = {k: (e, label) for k, e, label, _ in cat_help.topics()}
        title = "🐱 Cat help" if self.key not in rows else f"{rows[self.key][0]} {rows[self.key][1]}"
        return f"## {title}\n{cat_help.topic_text(self.key)}\n-# {cat_help.FOOTER}"


class HelpTopicSelect(discord.ui.Select):
    def __init__(self, current: Optional[str] = None):
        super().__init__(
            placeholder="Pick a topic",
            options=[discord.SelectOption(label=label, value=key, emoji=emoji, default=key == current)
                     for key, emoji, label, _ in cat_help.topics()])

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.edit_message(view=HelpView(self.values[0]))


class AdoptButton(discord.ui.DynamicItem[discord.ui.Button], template=r"^cat_adopt:(\d+)$"):
    def __init__(self, spawn_id: int, adopted: bool = False, gone: bool = False):
        self.spawn_id = spawn_id
        done = adopted or gone
        super().__init__(discord.ui.Button(
            label="Ran away 💨" if gone else "Adopted 💖" if adopted else "Adopt me!", emoji=None if done else "🐾",
            style=discord.ButtonStyle.secondary if done else discord.ButtonStyle.success,
            custom_id=f"cat_adopt:{spawn_id}", disabled=done))

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
        if res.status == "expired":
            await interaction.response.send_message("💨 This cat already wandered off. Watch for the next one!", ephemeral=True)
            try:                                    # also close the old spawn for everybody else
                await interaction.message.edit(**spawn_edit_kwargs(interaction.message, self.spawn_id, ca.EXPIRED_NOTE,
                                                                   gone=True))
            except Exception:
                logger.debug("[cat-adopt] couldn't mark the spawn as gone", exc_info=True)
            return
        if res.status in ("taken", "gone"):
            await interaction.response.send_message("😿 Someone else already adopted this cat." if res.status == "taken"
                                                    else "😿 This cat is no longer available.", ephemeral=True)
            return
        cat = res.cat
        cat_log.record(interaction.client, "adopt", **_event(interaction, cat))
        body = cat_announce.adopt_text(interaction.user.mention, cat.name, cat.id,
                                       ca.rarity_badge(cat.rarity), cat.trait, cat.rarity)
        await interaction.response.edit_message(**spawn_edit_kwargs(interaction.message, self.spawn_id, body, adopted=True))
        earned: list = []
        badge_lines = await cat_quests.on_adopt(interaction.user.id, _clone_id(interaction.client), cat.rarity, earned)
        await interaction.followup.send(view=_notice(
            f"🎉 You adopted **{cat.name}**, a {ca.rarity_badge(cat.rarity)} {cat.trait.lower()} cat! "
            + (f"\n{cat_wallet.reward_text(res.reward)}" if res.reward else "")
            + (("\n🏅 Badge earned: " + ", ".join(badge_lines)) if badge_lines else "")
            + "\nOpen **My cats** to rename, trade or gift."), ephemeral=True)
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
    await view.draw()
    await vis.send(interaction, view=view)


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
        self.parent.note = None
        await self.parent.draw()
        await interaction.response.edit_message(view=self.parent)


class CollectionView(discord.ui.LayoutView):
    """My cats: one card for the cat on show (picture, facts, purse, trade ID) with every button inside it.
    Results of an action (fed, ranked up, released ...) show as a line on top of the card until the next tap."""

    def __init__(self, client, user, cats: List[ca.Cat], index: int = 0):
        super().__init__(timeout=600)
        self.client, self.user, self.cats, self.index = client, user, cats, index
        self.all_cats = list(cats)                  # everything the person owns; self.cats is the filtered view of it
        self.filter = cat_filter.CatFilter()
        self.confirm_release = False
        self.confirm_rank_for = None        # (cat id, rank) the person tapped once; a second tap spends the coins
        self.wallet = cat_wallet.Wallet()
        self.note: Optional[str] = None     # the result of the last action, shown on top of the card
        self._urls: Dict[int, str] = {}     # pool id -> picture
        self._build()

    async def refresh_wallet(self) -> None:
        self.wallet = await cat_wallet.safe_wallet(self.user.id, _clone_id(self.client))

    @property
    def current(self) -> ca.Cat:
        return self.cats[self.index]

    async def prepare(self) -> None:
        """The async lookups a card needs (trade ID, picture), cached so a tap never waits for them twice."""
        if getattr(self, "trade_id", None) is None:
            self.trade_id = await cat_ids.safe_get(self.user.id, _clone_id(self.client)) or ""
        cat = self.current
        if not self._urls.get(cat.pool_id):
            url = await cat_image_url(self.client, cat)
            if url:
                self._urls[cat.pool_id] = url

    def _body(self) -> str:
        cat = self.current
        parts = []
        if self.note:
            parts += [self.note, ""]
        parts += [f"## {_cat_title(cat)}", _cat_body(cat), "",
                  f"**Your purse**\n{cat_wallet.wallet_text(self.wallet)}"]
        if getattr(self, "trade_id", None):
            parts += ["", f"**🆔 Your trade ID (yours for good)**\n`{self.trade_id}`\n"
                      "Members of your server can use it to offer you a trade, and ⭐ Premium servers can use it from any server. Nothing happens until you accept."]
        parts += ["", f"-# Cat {self.index + 1} of {len(self.cats)}"
                  + (f" (filtered from {len(self.all_cats)})" if len(self.cats) != len(self.all_cats) else "")
                  + " · just for fun, no real money"]
        return "\n".join(parts)

    async def text(self) -> str:
        await self.prepare()
        return self._body()

    async def draw(self) -> None:
        """Fetch what the card needs, then rebuild it. Call this before every edit of the message."""
        await self.prepare()
        self._build()

    def _build(self) -> None:
        self.clear_items()
        many = len(self.cats) > 1
        nav = []
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
            nav.append(b)
        rows = [discord.ui.ActionRow(*nav)]
        money = []
        have = self.wallet.diamonds
        for label, fn in ((f"{cat_wallet.DIAMOND} Convert 1", self._convert_one),
                          (f"{cat_wallet.DIAMOND} Convert all ({have})", self._convert_all)):
            b = discord.ui.Button(label=label, style=discord.ButtonStyle.secondary, disabled=have < 1)
            b.callback = fn
            money.append(b)
        cost = cr.next_cost(self.current)
        if cost is None:
            rb = discord.ui.Button(label="Top rank \U0001F31F", style=discord.ButtonStyle.secondary, disabled=True)
        else:
            armed = self.confirm_rank_for == (self.current.id, self.current.cat_rank)
            rb = discord.ui.Button(
                label=(f"Tap again: spend {cost:,} coins" if armed else f"\u2B50 Rank up \u00b7 {cost:,} coins")[:80],
                style=discord.ButtonStyle.danger if armed else discord.ButtonStyle.primary)
        rb.callback = self._rank_up
        money.append(rb)
        rows.append(discord.ui.ActionRow(*money))
        if many:
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
                         for i, c in enumerate(chunk)])
            jump.callback = self._jump
            rows.append(discord.ui.ActionRow(jump))
        fb = discord.ui.Button(label=(f"Filter ({self.filter.active})" if self.filter.active else "Filter / search"),
                               emoji="\U0001F50D", style=discord.ButtonStyle.secondary)
        fb.callback = self._open_filter
        rows.append(discord.ui.ActionRow(fb))
        hungry = cat_care.is_hungry(self.current)
        care = []
        for label, emoji, fn, style, disabled in (
                (f"Feed · {cat_care.FOOD_COST}", cat_care.FOOD, self._feed,
                 discord.ButtonStyle.danger if hungry else discord.ButtonStyle.secondary, not hungry),
                (f"Water · {cat_care.WATER_COST}", cat_care.DROP, self._water, discord.ButtonStyle.secondary, False),
                (f"Bed · {cat_care.BED_COST}", cat_care.BED, self._bed, discord.ButtonStyle.secondary, False)):
            b = discord.ui.Button(label=label, emoji=emoji, style=style, disabled=disabled)
            b.callback = fn
            care.append(b)
        rows.append(discord.ui.ActionRow(*care))
        self.add_item(_box(self._body(), rows, image=self._urls.get(self.current.pool_id), alt=self.current.name))

    async def _push(self, interaction, note: Optional[str] = None) -> None:
        """Show the card again (with `note` on top) by editing the message the person tapped on."""
        self.note = note
        await self.draw()
        await interaction.response.edit_message(view=self)

    async def _care(self, interaction, action, ok_text) -> None:
        cat = self.current
        try:
            res = await action(cat.id, interaction.user.id, _clone_id(self.client))
        except Exception:
            logger.exception("[cat-care] care action failed for cat %s", cat.id)
            await interaction.response.send_message("😿 Something went wrong, try again in a moment.", ephemeral=True)
            return
        until = f"<t:{int(res.until.timestamp())}:R>" if res.until else ""
        note = {
            "ok": ok_text.format(name=cat.name, coins=res.coins, cost=res.cost, until=until),
            "poor": f"You need {res.cost:,} coins and have {res.coins:,}. Adopt cats, sell a cat or convert a diamond to earn more.",
            "not_hungry": f"**{cat.name}** isn't hungry right now.",
            "already": f"**{cat.name}** is already set until {until}. You can top it up when less than "
                       f"{cat_care.RENEW_WINDOW_HOURS} hours are left.",
            "gone": f"**{cat.name}** isn't yours any more.",
        }.get(res.status, "Something went wrong, try again in a moment.")
        await self.reload(interaction.user.id)
        if not self.cats:                      # the cat wandered off while this screen was open and it was the last one
            await interaction.response.edit_message(view=_notice(note + "\nYou don't have any cats right now."))
            return
        await self.refresh_wallet()
        await self._push(interaction, note)

    async def _feed(self, interaction):
        await self._care(interaction, cat_care.feed, f"{cat_care.FOOD} **{{name}}** ate happily and purrs. ({{cost}} coins, {{coins:,}} left)")

    async def _water(self, interaction):
        await self._care(interaction, cat_care.water, f"{cat_care.DROP} **{{name}}** has fresh water until {{until}}. ({{cost}} coins, {{coins:,}} left)")

    async def _bed(self, interaction):
        await self._care(interaction, cat_care.rent_bed, f"{cat_care.BED} **{{name}}** has a cosy bed until {{until}}. ({{cost}} coins, {{coins:,}} left)")

    async def _rank_up(self, interaction):
        cat = self.current
        key = (cat.id, cat.cat_rank)
        if self.confirm_rank_for != key:                     # first tap: just arm the button
            self.confirm_rank_for = key
            await self._push(interaction)
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
        await self._push(interaction, note)
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
        note = ("You don't have a diamond to convert." if result is None else
                f"{cat_wallet.DIAMOND} Converted! You now have {result.coins:,} coins.")
        await self._push(interaction, note)

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
        if self.cats:                           # nothing to draw when the last cat is gone (sold, released, wandered off)
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
        await interaction.response.edit_message(view=FilterView(self))

    async def _show(self, interaction):
        await self._push(interaction)

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
                view=_notice(f"👋 **{cat.name}** went off to find a new home. You have no cats left."))
            return
        await self._push(interaction, f"👋 **{cat.name}** went off to find a new home.")

    async def _trade(self, interaction):
        if interaction.guild is None or interaction.channel is None:
            await interaction.response.send_message("Trades happen in a server channel, not in DMs.", ephemeral=True)
            return
        await self.prepare()
        cat = self.current
        text = (f"🤝 **{cat.name}** (cat #{cat.id}, {cr.badge(cat.cat_rank)}): pick a member to trade or gift with, "
                "use Trade by ID, or sell it for coins. (Trades and gifts are just for fun.)\n\n"
                + _trade_card_text(cat))
        view = TradeTargetView(self, text=text, image=self._urls.get(cat.pool_id))
        await interaction.response.edit_message(view=view)


def _trade_card_text(cat: ca.Cat) -> str:
    """The cat being traded or sold, kept on screen through the trade steps."""
    return f"## {_cat_title(cat)}\n{_cat_body(cat)}\n-# This is the cat you are trading or selling"


# ── trade flow ────────────────────────────────────────────────────────────

class TradeTargetView(discord.ui.LayoutView):
    """Step 1 of a trade: the cat on show, a member picker, Trade by ID, Sell for coins and Back."""

    def __init__(self, parent: CollectionView, text: str = "", image: Optional[str] = None):
        super().__init__(timeout=300)
        self.parent = parent
        self.text = text or "🤝 Pick a member to trade or gift with, use Trade by ID, or sell it for coins."
        self.image = image
        self.picker = discord.ui.UserSelect(placeholder="Pick a member", min_values=1, max_values=1)
        self.picker.callback = self._picked
        by_id = discord.ui.Button(label="Trade by ID", emoji="🆔", style=discord.ButtonStyle.primary)
        by_id.callback = self._by_id
        sell = discord.ui.Button(label="Sell for coins", emoji="\U0001F3F7\uFE0F", style=discord.ButtonStyle.success)
        sell.callback = self._sell
        back = discord.ui.Button(label="Back", style=discord.ButtonStyle.secondary)
        back.callback = self._back
        self.add_item(_box(self.text, [discord.ui.ActionRow(self.picker), discord.ui.ActionRow(by_id, sell, back)],
                           image=image, alt=getattr(getattr(parent, "current", None), "name", "Cat")))

    async def interaction_check(self, interaction):
        return await self.parent.interaction_check(interaction)

    async def _back(self, interaction):
        await self.parent._push(interaction)

    async def _sell(self, interaction: discord.Interaction):
        from discord_bot.cogs._views_cat_market import open_sell_wizard
        await open_sell_wizard(interaction, self.parent.current)

    async def _by_id(self, interaction: discord.Interaction):
        # Everyone can type an ID. Whether it works across servers is decided after the ID is read (TradeIdModal).
        await interaction.response.send_modal(TradeIdModal(self.parent))

    async def _picked(self, interaction: discord.Interaction):
        target = self.picker.values[0]
        if target.bot or target.id == interaction.user.id:
            await interaction.response.send_message("Pick another member (not yourself, not a bot).", ephemeral=True)
            return
        theirs = await ca.list_cats(target.id, _clone_id(interaction.client))
        text = (f"🤝 What should **{self.parent.current.name}** be swapped for? Pick one of {target.display_name}'s "
                "cats, or just gift yours.\n\n" + _trade_card_text(self.parent.current))
        view = TradeAskView(self.parent, target, theirs, text=text, image=self.image)
        await interaction.response.edit_message(view=view)


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
            await interaction.response.edit_message(view=TradeAskView(
                self.parent, member, theirs, their_clone=their_clone,
                text=(f"🆔 Trading with **{member.display_name}** (a member of this server). What should "
                      f"**{self.parent.current.name}** be swapped for? Or just gift yours.")))
            return
        try:
            target = await client.fetch_user(owner_id)
        except discord.HTTPException:
            target = None
        if target is None or target.bot:
            await interaction.response.send_message("😿 I couldn't find that person right now.", ephemeral=True)
            return
        theirs = await ca.list_cats(owner_id, their_clone)
        view = TradeAskView(self.parent, target, theirs, remote=True, their_clone=their_clone,
                            text=(f"🌍 Trading with **{target.display_name}** (from any server). What should "
                                  f"**{self.parent.current.name}** be swapped for? Or just gift yours."))
        await interaction.response.edit_message(view=view)


_UNSET = object()


class TradeAskView(discord.ui.LayoutView):
    """Step 2 of a trade: what to ask for in return (one of their cats, or nothing for a gift)."""

    def __init__(self, parent: CollectionView, target, their_cats: List[ca.Cat], remote: bool = False,
                 their_clone=_UNSET, text: str = "", image: Optional[str] = None):
        super().__init__(timeout=300)
        self.remote = remote
        # which bot's collection their cats are in: this one unless the ID they gave belongs to another bot
        self.their_clone = _clone_id(getattr(parent, "client", None)) if their_clone is _UNSET else their_clone
        self.parent, self.target = parent, target
        self.text, self.image = text or "🤝 What should it be swapped for? Or just gift yours.", image
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
        buttons = []
        if pages > 1:
            for label, delta, off in (("\u25C0", -1, self.page <= 0), ("\u25B6", 1, self.page >= pages - 1)):
                b = discord.ui.Button(label=label, style=discord.ButtonStyle.secondary, disabled=off)
                b.callback = (lambda d: (lambda i: self._turn(i, d)))(delta)
                buttons.append(b)
        back = discord.ui.Button(label="Back", style=discord.ButtonStyle.secondary)
        back.callback = self._back
        buttons.append(back)
        self.add_item(_box(self.text, [discord.ui.ActionRow(self.pick), discord.ui.ActionRow(*buttons)],
                           image=self.image, alt=getattr(getattr(self.parent, "current", None), "name", "Cat")))

    async def _turn(self, interaction, delta: int):
        self.page += delta
        self._build()
        await interaction.response.edit_message(view=self)

    async def interaction_check(self, interaction):
        return await self.parent.interaction_check(interaction)

    async def _back(self, interaction):
        await self.parent._push(interaction)

    async def _chosen(self, interaction: discord.Interaction):
        mine = self.parent.current
        value = self.pick.values[0]
        want = None if value == GIFT_VALUE else self.their.get(int(value))
        if value != GIFT_VALUE and want is None:
            await interaction.response.send_message("That cat isn't available any more.", ephemeral=True)
            return
        where = f" from **{interaction.guild.name}**" if self.remote and interaction.guild else ""
        offer = TradeOfferView(interaction.client, interaction.user, self.target, mine, want, remote=self.remote,
                               taker_clone=self.their_clone, where=where)
        if self.remote:
            premium = await cs.is_premium(interaction.guild.id, _clone_id(interaction.client)) if interaction.guild else False
            if not cat_ids.can_trade_across_servers(premium):
                await interaction.response.edit_message(view=_notice("⭐ Trading by ID needs a Premium server."))
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
                msg = await self.target.send(view=offer)
            except discord.HTTPException:
                await interaction.response.edit_message(view=_notice(
                    "😿 I couldn't message them (their DMs are closed). Ask them to open DMs, or trade in a "
                    "server you share."))
                return
            _remote_at[interaction.user.id] = time.monotonic()
            _remote_pending[self.target.id] = _remote_pending.get(self.target.id, 0) + 1
            offer.counted = True
            offer.message = msg
            await interaction.response.edit_message(view=_notice(
                f"✅ Offer sent to {self.target.display_name} by DM. They have {OFFER_TIMEOUT // 60} minutes to "
                "answer, and I'll tell you what they say."))
            return
        try:
            msg = await interaction.channel.send(
                view=offer,
                allowed_mentions=discord.AllowedMentions(users=[self.target], everyone=False, roles=False))
        except discord.HTTPException:
            await interaction.response.edit_message(
                view=_notice("😿 I couldn't post the offer in this channel (I need Send Messages)."))
            return
        offer.message = msg
        await interaction.response.edit_message(view=_notice(
            f"✅ Offer sent to {self.target.mention}. They have {OFFER_TIMEOUT // 60} minutes to answer."))


def _offer_done(text: str) -> discord.ui.LayoutView:
    """What a trade offer turns into once it is answered, cancelled or expired: the same title, no buttons."""
    return _notice(f"## 🤝 Cat trade offer\n{text}")


class TradeOfferView(discord.ui.LayoutView):
    """Public offer. Only the other person can accept or decline; only the sender can cancel."""

    def __init__(self, client, giver, taker, give: ca.Cat, take: Optional[ca.Cat], remote: bool = False,
                 taker_clone=_UNSET, where: str = ""):
        super().__init__(timeout=OFFER_TIMEOUT)
        self.client, self.giver, self.taker, self.give, self.take = client, giver, taker, give, take
        self.taker_clone = _clone_id(client) if taker_clone is _UNSET else taker_clone
        self.remote, self.counted = remote, False
        self.message: Optional[discord.Message] = None
        self.text = (
            f"## 🤝 Cat trade offer\n{giver.mention}{where} offers {cat_line(give)}\n"
            + (f"in exchange for {taker.mention}'s {cat_line(take)}" if take else
               f"as a **gift** to {taker.mention} 🎁")
            + "\n\nJust for fun, no real money. Nothing moves until it's accepted.")
        buttons = []
        for label, fn, style in (("Accept", self._accept, discord.ButtonStyle.success),
                                 ("Decline", self._decline, discord.ButtonStyle.danger),
                                 ("Cancel offer", self._cancel, discord.ButtonStyle.secondary)):
            b = discord.ui.Button(label=label, style=style)
            b.callback = fn
            buttons.append(b)
        self.add_item(_box(self.text, [discord.ui.ActionRow(*buttons)]))

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
        await interaction.response.edit_message(view=_offer_done(text))

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
                await self.message.edit(view=_offer_done("⏰ This offer expired."))
            except discord.HTTPException:
                pass


from discord_bot.cogs import _views_cat_market as _market  # noqa: E402  (needs the names defined above)

DYNAMIC_ITEMS = (AdoptButton, MyCatsButton, HelpButton, MenuButton) + _market.DYNAMIC_ITEMS


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
        await interaction.response.edit_message(view=self.parent)


class FilterView(discord.ui.LayoutView):
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
        rar = discord.ui.Select(placeholder="Rarity", options=[
            discord.SelectOption(label="Any rarity", value="any", default=f.rarity is None)] + [
            discord.SelectOption(label=r.title(), value=r, emoji=ca.RARITY_EMOJI.get(r), default=f.rarity == r)
            for r in ca.RARITY_RANK])
        rar.callback = self._rarity
        trait = discord.ui.Select(placeholder="Trait", options=[
            discord.SelectOption(label="Any trait", value="any", default=f.trait is None)] + [
            discord.SelectOption(label=t, value=t, default=f.trait == t) for t in ca.TRAITS])
        trait.callback = self._trait
        sort = discord.ui.Select(placeholder="Sort by", options=[
            discord.SelectOption(label=label, value=key, default=f.sort == key) for key, label in cat_filter.SORTS.items()])
        sort.callback = self._sort
        buttons = []
        for label, fn, style, disabled in (
            ("Search name / ID", self._search, discord.ButtonStyle.secondary, False),
            ("Clear", self._clear, discord.ButtonStyle.secondary, f == cat_filter.CatFilter()),
            ("Done", self._done, discord.ButtonStyle.success, self._matches() == 0),
        ):
            b = discord.ui.Button(label=label, style=style, disabled=disabled)
            b.callback = fn
            buttons.append(b)
        self.add_item(_box(self.status(), [discord.ui.ActionRow(rar), discord.ui.ActionRow(trait),
                                           discord.ui.ActionRow(sort), discord.ui.ActionRow(*buttons)]))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await self.parent.interaction_check(interaction)

    async def _update(self, interaction, **kw):
        self.f = self.f.with_(**kw)
        self._build()
        await interaction.response.edit_message(view=self)

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
        await interaction.response.edit_message(view=self)

    async def _done(self, interaction):
        self.parent.filter = self.f
        self.parent._reapply()
        self.parent.index = 0
        await self.parent._push(interaction)
