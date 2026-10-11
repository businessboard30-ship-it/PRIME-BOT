from __future__ import annotations

import asyncio
import io
import logging

import discord

from modules import cat_adopt
from modules import cat_announce
from modules import cat_profile_card
from modules import cat_public
from modules import cat_features
from modules import cat_quests
from modules import cat_share
from modules import cat_wallet
from modules import cat_wishlist

logger = logging.getLogger(__name__)

PINK = discord.Color.from_rgb(255, 182, 193)
GOLD = discord.Color.gold()


def feature_button_view() -> discord.ui.View:
    view = discord.ui.View(timeout=300)
    view.add_item(CareButton())
    view.add_item(CosmeticsButton())
    view.add_item(QuestsButton())
    view.add_item(BadgesButton())
    view.add_item(WishlistButton())
    view.add_item(ProfileButton())
    return view


class CareButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Daily care", emoji="💖", style=discord.ButtonStyle.primary, custom_id="cat_feature:care")

    async def callback(self, interaction: discord.Interaction):
        try:
            result = await cat_features.care(interaction.user.id, getattr(interaction.client, "clone_id", None))
        except Exception:
            await interaction.response.send_message("Daily care is temporarily unavailable. Try again shortly.", ephemeral=True)
            return
        share = discord.ui.View(timeout=120)
        share.add_item(ShareButton("care", streak=result.streak))
        if result.status == "already":
            await interaction.response.send_message(f"You already cared for your cats today. Streak: **{result.streak} days**.", view=share, ephemeral=True)
            return
        diamond = f" and 💎 **{result.diamonds} diamond**" if result.diamonds else ""
        await interaction.response.send_message(f"💖 Care complete! Streak: **{result.streak} days**. You earned 🪙 **{result.coins} coins**{diamond}.", view=share, ephemeral=True)
        if result.streak in cat_announce.MILESTONE_STREAKS:
            await cat_announce.announce(interaction, cat_announce.streak_text(interaction.user.mention, result.streak), kind="milestone")


class CosmeticBuySelect(discord.ui.Select):
    def __init__(self, items):
        self.items = {str(i.id): i for i in items}
        super().__init__(placeholder="Buy a cosmetic…", min_values=1, max_values=1, options=[
            discord.SelectOption(label=i.name[:100], value=str(i.id), emoji=i.emoji,
                                 description=(f"{i.price_coins:,} coins" + (f" + {i.price_diamonds} diamonds" if i.price_diamonds else ""))[:100])
            for i in items[:25]])

    async def callback(self, interaction: discord.Interaction):
        item = self.items.get(self.values[0])
        try:
            status = await cat_features.buy_cosmetic(interaction.user.id, getattr(interaction.client, "clone_id", None), int(self.values[0]))
        except Exception:
            await interaction.response.send_message("The cosmetic shop is temporarily unavailable.", ephemeral=True)
            return
        text = {"ok": f"{item.emoji} You bought **{item.name}**! It stays yours even if you sell or release cats.",
                "owned": "You already own that one.", "poor": "You don't have enough coins/diamonds for that yet.",
                "missing": "That item is no longer in the shop."}.get(status, "Something went wrong.")
        await interaction.response.send_message(text, ephemeral=True)


class CosmeticsButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Cosmetics", emoji="✨", style=discord.ButtonStyle.secondary, custom_id="cat_feature:cosmetics")

    async def callback(self, interaction: discord.Interaction):
        try:
            items = await cat_features.cosmetics()
        except Exception:
            await interaction.response.send_message("The cosmetic shop is temporarily unavailable.", ephemeral=True)
            return
        embed = discord.Embed(title="✨ Cat cosmetics", description="Hats, collars and card backgrounds for your public profile.", color=PINK)
        embed.add_field(name="Available", value="\n".join(
            f"{item.emoji} **{item.name}** — {item.price_coins:,} coins" + (f" + {item.price_diamonds} diamonds" if item.price_diamonds else "")
            for item in items) or "Nothing in stock yet.", inline=False)
        embed.set_footer(text="Cosmetics are play-only rewards.")
        view = discord.ui.View(timeout=120)
        if items:
            view.add_item(CosmeticBuySelect(items))
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


class ProfileButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="My profile", emoji="📖", style=discord.ButtonStyle.secondary, custom_id="cat_feature:profile")

    async def callback(self, interaction: discord.Interaction):
        try:
            profile = await cat_features.public_profile(interaction.user.id, _clone(interaction))
            badges = await cat_quests.owned_badges(interaction.user.id, _clone(interaction))
        except Exception:
            await interaction.response.send_message("Your profile is temporarily unavailable.", ephemeral=True)
            return
        embed = cat_share.profile_embed(interaction.user.display_name, profile, badges, _avatar(interaction.user))
        if not profile["show_profile"]:
            embed.set_footer(text="Hidden: only you can see this. Tap Show profile to let others see it.")
        await interaction.response.send_message(embed=embed, view=ProfileView(profile), ephemeral=True)
        await upgrade_to_card(interaction, interaction.user, profile, badges,
                              footer=embed.footer.text if not profile["show_profile"] else None)


class ShareButton(discord.ui.Button):
    """Posts a card publicly, only because the player tapped it. Rebuilt from the database at that moment."""

    def __init__(self, kind: str, streak: int = 0, row=None):
        super().__init__(label="Share", emoji="📣", style=discord.ButtonStyle.success, row=row)
        self.kind, self.streak = kind, streak

    async def callback(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message("Sharing only works in a server channel.", ephemeral=True)
            return
        wait = cat_share.cooldown_left(interaction.user.id, self.kind)
        if wait > 0:
            await interaction.response.send_message(f"You just shared that. Try again in {wait:.0f}s.", ephemeral=True)
            return
        await interaction.response.defer()
        try:
            card = await self._card(interaction)
        except Exception:
            await interaction.followup.send("Sharing is temporarily unavailable.", ephemeral=True)
            return
        if isinstance(card, str):
            await interaction.followup.send(card, ephemeral=True)
            return
        posted = await (cat_announce.announce(interaction, card[0], kind="share") if card[0] is not None
                        else cat_announce.announce_embed(interaction, card[1], kind="share",
                                                         png=card[2] if len(card) > 2 else None))
        if posted:
            cat_share.mark_shared(interaction.user.id, self.kind)
        else:
            await interaction.followup.send("I couldn't post in this channel.", ephemeral=True)

    async def _card(self, interaction):
        """Returns a private message (str) when there is nothing to share, else (text, None) or (None, embed)."""
        who, clone = interaction.user, _clone(interaction)
        if self.kind == "care":
            return cat_share.care_text(who.mention, self.streak), None
        if self.kind == "badges":
            embed = cat_share.badges_embed(who.display_name, await cat_quests.owned_badges(who.id, clone))
            return "You have no badges to show yet." if embed is None else (None, embed)
        profile = await cat_features.public_profile(who.id, clone)
        if not profile["show_profile"]:
            return "Your profile is hidden, so it can't be shared. Tap Show profile first."
        badges = await cat_quests.owned_badges(who.id, clone)
        png = await cat_profile_card.render(interaction.client, who, who.display_name, profile, badges)
        if png is not None:
            return None, cat_share.profile_card_embed(who.display_name), png
        return None, cat_share.profile_embed(who.display_name, profile, badges, _avatar(who))


class BioModal(discord.ui.Modal, title="Edit your cat bio"):
    bio = discord.ui.TextInput(label="Bio (plain text)", style=discord.TextStyle.paragraph, required=False,
                               max_length=cat_share.BIO_MAX, placeholder="Tell people about your cats")

    def __init__(self, current: str = ""):
        super().__init__()
        self.bio.default = current or None

    async def on_submit(self, interaction: discord.Interaction):
        text = cat_share.clean_bio(self.bio.value)
        if text is None:
            await interaction.response.send_message("A bio can't contain links or invites. Keep it to plain text.", ephemeral=True)
            return
        await _update_profile(interaction, bio=text, done="📖 Bio saved.")


async def _update_profile(interaction, bio=None, pinned=None, show=None, done="Saved."):
    """Change one thing on the profile and keep the rest. Never raises."""
    try:
        cur = await cat_features.public_profile(interaction.user.id, _clone(interaction))
        await cat_features.save_profile(
            interaction.user.id, _clone(interaction),
            cur["bio"] if bio is None else bio,
            [c.id for c in cur["pinned"]] if pinned is None else pinned,
            cur["show_profile"] if show is None else show)
    except Exception:
        await interaction.response.send_message("Your profile is temporarily unavailable.", ephemeral=True)
        return
    await interaction.response.send_message(done, ephemeral=True)


class PinSelect(discord.ui.Select):
    def __init__(self, cats):
        super().__init__(placeholder="Pick up to 3 cats to show off…", min_values=1, max_values=min(3, len(cats)), options=[
            discord.SelectOption(label=c.name[:100], value=str(c.id), emoji=cat_adopt.RARITY_EMOJI.get(c.rarity),
                                 description=f"{c.rarity.title()} · {c.trait}"[:100]) for c in cats])

    async def callback(self, interaction: discord.Interaction):
        await _update_profile(interaction, pinned=[int(v) for v in self.values], done="🐱 Showcase updated.")


class ProfileView(discord.ui.View):
    def __init__(self, profile: dict):
        super().__init__(timeout=180)
        self.profile = profile
        share = ShareButton("profile", row=0)
        share.disabled = not profile["show_profile"]
        self.add_item(share)
        for label, emoji, cb in (("Edit bio", "✏️", self._bio), ("Pin cats", "📌", self._pin), ("Clear pins", "🧹", self._clear)):
            b = discord.ui.Button(label=label, emoji=emoji, style=discord.ButtonStyle.secondary, row=0)
            b.callback = cb
            self.add_item(b)
        vis = discord.ui.Button(label="Hide profile" if profile["show_profile"] else "Show profile",
                                emoji="🙈" if profile["show_profile"] else "👀", style=discord.ButtonStyle.secondary, row=0)
        vis.callback = self._toggle
        self.add_item(vis)

    async def _bio(self, interaction: discord.Interaction):
        await interaction.response.send_modal(BioModal(self.profile["bio"]))

    async def _pin(self, interaction: discord.Interaction):
        try:
            cats = (await cat_adopt.list_cats(interaction.user.id, _clone(interaction)))[:25]
        except Exception:
            await interaction.response.send_message("Your cats are temporarily unavailable.", ephemeral=True)
            return
        if not cats:
            await interaction.response.send_message("Adopt a cat first, then you can pin it.", ephemeral=True)
            return
        view = discord.ui.View(timeout=120)
        view.add_item(PinSelect(cats))
        note = " Showing your 25 rarest." if len(cats) == 25 else ""
        await interaction.response.send_message("Which cats should your profile show off?" + note, view=view, ephemeral=True)

    async def _clear(self, interaction: discord.Interaction):
        await _update_profile(interaction, pinned=[], done="🧹 Showcase cleared.")

    async def _toggle(self, interaction: discord.Interaction):
        now_show = not self.profile["show_profile"]
        await _update_profile(interaction, show=now_show,
                              done="👀 Your profile is public." if now_show else "🙈 Your profile is hidden from others.")


async def show_public_profile(interaction: discord.Interaction, target) -> None:
    """`/cat profile:@player`: anyone can look. A hidden profile stays hidden, and the card never has private data."""
    if getattr(target, "bot", False):
        await interaction.response.send_message("Bots don't collect cats 🐾", ephemeral=True)
        return
    try:
        profile = await asyncio.wait_for(cat_features.public_profile(target.id, _clone(interaction)), 2.0)
        badges = await asyncio.wait_for(cat_quests.owned_badges(target.id, _clone(interaction)), 2.0)
    except Exception:
        await interaction.response.send_message("Profiles are temporarily unavailable. Try again shortly.", ephemeral=True)
        return
    if not profile["show_profile"] and target.id != interaction.user.id:
        await interaction.response.send_message("That player keeps their cat profile private.", ephemeral=True)
        return
    if not profile["collection_size"] and not profile["bio"]:
        await interaction.response.send_message(f"{target.display_name} hasn't adopted any cats yet.", ephemeral=True)
        return
    embed = cat_share.profile_embed(target.display_name, profile, badges, _avatar(target))
    mine_hidden = not profile["show_profile"]
    await interaction.response.send_message(embed=embed, ephemeral=mine_hidden, allowed_mentions=discord.AllowedMentions.none())
    await upgrade_to_card(interaction, target, profile, badges, footer=None)


async def upgrade_to_card(interaction, user, profile: dict, badges, footer=None) -> None:
    """The plain profile embed is already on screen (so the 3-second reply window is never at risk). Draw the card and
    swap it in. If anything goes wrong the plain embed simply stays. Never raises."""
    try:
        png = await cat_profile_card.render(interaction.client, user, user.display_name, profile, badges)
        if png is None:
            return
        embed = cat_share.profile_card_embed(user.display_name)
        if footer:
            embed.set_footer(text=footer)
        await interaction.edit_original_response(
            embed=embed, attachments=[discord.File(io.BytesIO(png), filename=cat_profile_card.FILE_NAME)])
    except Exception:
        logging.getLogger(__name__).info("[cat-profile] kept the plain profile (card not shown)", exc_info=True)


def _avatar(user):
    """The user's current Discord avatar (server one if they have it), or None. Never raises."""
    try:
        return user.display_avatar.url
    except Exception:
        return None


def _clone(interaction):
    return getattr(interaction.client, "clone_id", None)


class QuestClaimSelect(discord.ui.Select):
    def __init__(self, ready):
        super().__init__(placeholder="Claim a finished quest…", min_values=1, max_values=1, options=[
            discord.SelectOption(label=q.title[:100], value=q.key, emoji="🎁", description=f"+{q.reward} diamond(s)")
            for q in ready])

    async def callback(self, interaction: discord.Interaction):
        try:
            status = await cat_quests.claim(interaction.user.id, _clone(interaction), self.values[0])
        except Exception:
            await interaction.response.send_message("🎯 Quests are temporarily unavailable.", ephemeral=True)
            return
        text = {"ok": "🎁 Quest claimed! Your 💎 were added to your wallet.",
                "not_ready": "That quest isn't ready, or you already claimed it."}.get(status, "Something went wrong.")
        await interaction.response.send_message(text, ephemeral=True)
        if status == "ok":
            try:
                done = all(q.claimed for q in await cat_quests.quests(interaction.user.id, _clone(interaction)))
            except Exception:
                done = False
            if done:
                await cat_announce.announce(interaction, cat_announce.quests_text(interaction.user.mention), kind="milestone")


class QuestsButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Daily quests", emoji="🎯", style=discord.ButtonStyle.primary, custom_id="cat_feature:quests")

    async def callback(self, interaction: discord.Interaction):
        try:
            items = await cat_quests.quests(interaction.user.id, _clone(interaction))
        except Exception:
            await interaction.response.send_message("🎯 Quests are temporarily unavailable.", ephemeral=True)
            return
        lines = [f"{'✅' if q.progress >= q.target else '🟦'} **{q.title}** — {q.progress}/{q.target} · 💎 {q.reward}"
                 + (" · claimed" if q.claimed else "") for q in items]
        embed = discord.Embed(title="🎯 Daily cat quests", color=GOLD,
                              description="Finish these before UTC midnight, then claim your diamonds.\n\n" + "\n".join(lines))
        view = discord.ui.View(timeout=120)
        ready = [q for q in items if q.ready]
        if ready:
            view.add_item(QuestClaimSelect(ready))
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


class BadgesButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Badges", emoji="🏅", style=discord.ButtonStyle.secondary, custom_id="cat_feature:badges")

    async def callback(self, interaction: discord.Interaction):
        try:
            new = await cat_quests.award_badges(interaction.user.id, _clone(interaction))
            have = await cat_quests.owned_badges(interaction.user.id, _clone(interaction))
        except Exception:
            await interaction.response.send_message("🏅 Badges are temporarily unavailable.", ephemeral=True)
            return
        lines = [f"{'✅' if slug in have else '🔒'} {emoji} **{name}** — {how} (+{reward} 💎)"
                 for slug, (emoji, name, how, reward) in cat_quests.BADGES.items()]
        text = "\n".join(lines)
        if new:
            text = "🎉 New: " + ", ".join(cat_quests.badge_line(s) for s in new) + "\n\n" + text
        view = discord.ui.View(timeout=120)
        if have or new:
            view.add_item(ShareButton("badges"))
        await interaction.response.send_message(embed=discord.Embed(title="🏅 Collection badges", description=text, color=GOLD), view=view, ephemeral=True)
        await cat_announce.announce_badges(interaction, new)


# ── Wishlist ─────────────────────────────────────────────────────────────────

def _board_embed(wants, total: int, page: int, rarity) -> discord.Embed:
    lines = [f"<@{w.owner_id}> is looking for {w.text()}" for w in wants]
    desc = "\n".join(lines) or "Nobody is looking for anything yet. Be the first to post a want!"
    e = discord.Embed(title="🔎 Cat wishlist" + (f" · {rarity}" if rarity else ""), description=desc, color=PINK)
    pages = max(1, -(-total // cat_wishlist.PAGE_SIZE))
    e.set_footer(text=f"Page {page + 1}/{pages} · {total} open · Got a match? Open My cats, then Trade / gift. Wants expire after "
                      f"{cat_wishlist.LIFETIME_DAYS} days.")
    return e


class WishlistBoardView(discord.ui.View):
    def __init__(self, page: int = 0, total: int = 0, rarity=None):
        super().__init__(timeout=300)
        self.page, self.rarity = page, rarity
        pages = max(1, -(-total // cat_wishlist.PAGE_SIZE))
        for label, delta, off in (("◀", -1, page <= 0), ("▶", 1, page >= pages - 1)):
            b = discord.ui.Button(label=label, style=discord.ButtonStyle.secondary, disabled=off, row=0)
            b.callback = self._turn(delta)
            self.add_item(b)
        post = discord.ui.Button(label="Post a want", emoji="➕", style=discord.ButtonStyle.success, row=0)
        post.callback = self._post
        mine = discord.ui.Button(label="My wants", emoji="📝", style=discord.ButtonStyle.secondary, row=0)
        mine.callback = self._mine
        self.add_item(post)
        self.add_item(mine)
        sel = discord.ui.Select(placeholder="Show only…", row=1, options=[
            discord.SelectOption(label="All rarities", value="any", default=rarity is None)] + [
            discord.SelectOption(label=r.title(), value=r, emoji=cat_adopt.RARITY_EMOJI.get(r), default=rarity == r)
            for r in cat_wishlist.RARITIES])
        sel.callback = self._filter
        self.add_item(sel)

    async def _render(self, interaction, page: int, rarity, edit: bool = True):
        wants, total = await cat_wishlist.board(page, rarity=rarity)
        view = WishlistBoardView(page, total, rarity)
        embed = _board_embed(wants, total, page, rarity)
        if edit:
            await interaction.response.edit_message(embed=embed, view=view)
        else:
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    def _turn(self, delta):
        async def cb(interaction: discord.Interaction):
            try:
                await self._render(interaction, max(0, self.page + delta), self.rarity)
            except Exception:
                await interaction.response.send_message("The wishlist is temporarily unavailable.", ephemeral=True)
        return cb

    async def _filter(self, interaction: discord.Interaction):
        v = interaction.data["values"][0]
        try:
            await self._render(interaction, 0, None if v == "any" else v)
        except Exception:
            await interaction.response.send_message("The wishlist is temporarily unavailable.", ephemeral=True)

    async def _post(self, interaction: discord.Interaction):
        view = WishlistPostView()
        await interaction.response.send_message("What are you looking for? Pick a rarity (and a trait if you like).",
                                                view=view, ephemeral=True)

    async def _mine(self, interaction: discord.Interaction):
        try:
            mine = await cat_wishlist.mine(interaction.user.id, _clone(interaction))
        except Exception:
            await interaction.response.send_message("The wishlist is temporarily unavailable.", ephemeral=True)
            return
        if not mine:
            await interaction.response.send_message("You have no open wants. Tap **Post a want** to add one.", ephemeral=True)
            return
        view = discord.ui.View(timeout=120)
        view.add_item(WishlistRemoveSelect(mine))
        await interaction.response.send_message("\n".join(f"• {w.text()}" for w in mine) + "\n\nPick one to remove it.",
                                                view=view, ephemeral=True)


class WishlistNoteModal(discord.ui.Modal, title="Post a want"):
    note = discord.ui.TextInput(label="Optional note (plain text)", required=False, max_length=cat_wishlist.NOTE_MAX,
                                placeholder="e.g. will swap a rare for it")

    def __init__(self, rarity: str, trait, share: bool = False):
        super().__init__()
        self.rarity, self.trait, self.share = rarity, trait, share

    async def on_submit(self, interaction: discord.Interaction):
        try:
            status = await cat_wishlist.add(interaction.user.id, _clone(interaction), self.rarity, self.trait, self.note.value)
        except Exception:
            await interaction.response.send_message("The wishlist is temporarily unavailable.", ephemeral=True)
            return
        text = {"ok": "🔎 Posted! Traders can see your want for 14 days.",
                "limit": f"You already have {cat_wishlist.MAX_OPEN} open wants. Remove one in **My wants** first.",
                "duplicate": "You already posted that exact want.",
                "bad": "That note can't contain links or invites. Keep it to plain text."}.get(status, "Something went wrong.")
        await interaction.response.send_message(text, ephemeral=True)
        if status == "ok" and self.share:
            if not cat_public.wish_ok(interaction.user.id):
                await interaction.followup.send("It's on the board. You can also post one want to the channel per day, "
                                                "and you've used today's.", ephemeral=True)
            elif await cat_announce.announce(interaction, cat_public.wish_text(
                    interaction.user.mention, self.rarity, self.trait, self.note.value)):
                cat_public.mark_wish(interaction.user.id)


class WishlistPostView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)
        self.rarity, self.trait = None, None
        rar = discord.ui.Select(placeholder="Rarity (required)", row=0, options=[
            discord.SelectOption(label=r.title(), value=r, emoji=cat_adopt.RARITY_EMOJI.get(r)) for r in cat_wishlist.RARITIES])
        rar.callback = self._rarity
        trait = discord.ui.Select(placeholder="Trait (optional)", row=1, options=[
            discord.SelectOption(label="Any trait", value="any")] + [discord.SelectOption(label=t, value=t) for t in cat_adopt.TRAITS])
        trait.callback = self._trait
        self.go = discord.ui.Button(label="Continue", style=discord.ButtonStyle.success, disabled=True, row=2)
        self.go.callback = self._go
        self.share = False
        self.share_btn = discord.ui.Button(label="Also post in this channel: off", emoji="📣",
                                           style=discord.ButtonStyle.secondary, row=2)
        self.share_btn.callback = self._toggle_share
        for item in (rar, trait, self.go, self.share_btn):
            self.add_item(item)

    async def _toggle_share(self, interaction: discord.Interaction):
        self.share = not self.share
        self.share_btn.label = f"Also post in this channel: {'on' if self.share else 'off'}"
        self.share_btn.style = discord.ButtonStyle.primary if self.share else discord.ButtonStyle.secondary
        await interaction.response.edit_message(view=self)

    async def _rarity(self, interaction: discord.Interaction):
        self.rarity = interaction.data["values"][0]
        self.go.disabled = False
        await interaction.response.edit_message(view=self)

    async def _trait(self, interaction: discord.Interaction):
        v = interaction.data["values"][0]
        self.trait = None if v == "any" else v
        await interaction.response.defer()

    async def _go(self, interaction: discord.Interaction):
        await interaction.response.send_modal(WishlistNoteModal(self.rarity, self.trait, self.share))


class WishlistRemoveSelect(discord.ui.Select):
    def __init__(self, mine):
        super().__init__(placeholder="Remove a want…", options=[
            discord.SelectOption(label=(f"{w.rarity.title()} {w.trait or ''}".strip())[:100], value=str(w.id)) for w in mine])

    async def callback(self, interaction: discord.Interaction):
        try:
            ok = await cat_wishlist.remove(int(self.values[0]), interaction.user.id)
        except Exception:
            await interaction.response.send_message("The wishlist is temporarily unavailable.", ephemeral=True)
            return
        await interaction.response.send_message("🗑 Removed." if ok else "That want is already gone.", ephemeral=True)


class WishlistButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Wishlist", emoji="🔎", style=discord.ButtonStyle.secondary, custom_id="cat_feature:wishlist")

    async def callback(self, interaction: discord.Interaction):
        try:
            wants, total = await cat_wishlist.board()
        except Exception:
            await interaction.response.send_message("The wishlist is temporarily unavailable.", ephemeral=True)
            return
        await interaction.response.send_message(embed=_board_embed(wants, total, 0, None),
                                                view=WishlistBoardView(0, total, None), ephemeral=True)
