from __future__ import annotations

import discord

from modules import cat_features
from modules import cat_quests
from modules import cat_wallet

PINK = discord.Color.from_rgb(255, 182, 193)
GOLD = discord.Color.gold()


def feature_button_view() -> discord.ui.View:
    view = discord.ui.View(timeout=300)
    view.add_item(CareButton())
    view.add_item(CosmeticsButton())
    view.add_item(QuestsButton())
    view.add_item(BadgesButton())
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
        if result.status == "already":
            await interaction.response.send_message(f"You already cared for your cats today. Streak: **{result.streak} days**.", ephemeral=True)
            return
        diamond = f" and 💎 **{result.diamonds} diamond**" if result.diamonds else ""
        await interaction.response.send_message(f"💖 Care complete! Streak: **{result.streak} days**. You earned 🪙 **{result.coins} coins**{diamond}.", ephemeral=True)


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
            profile = await cat_features.public_profile(interaction.user.id, getattr(interaction.client, "clone_id", None))
        except Exception:
            await interaction.response.send_message("Your profile is temporarily unavailable.", ephemeral=True)
            return
        if not profile["show_profile"]:
            await interaction.response.send_message("Your profile is hidden. You can make it public from profile settings.", ephemeral=True)
            return
        embed = discord.Embed(title=f"📖 {interaction.user.display_name}'s cat profile", color=GOLD)
        embed.description = profile["bio"] or "No bio yet — your cats are waiting for a story."
        embed.add_field(name="Collection", value=f"🐾 **{profile['collection_size']}** cats", inline=True)
        rarest = profile["rarest"]
        embed.add_field(name="Rarest", value=rarest and f"{rarest.name} · {rarest.rarity.title()}" or "None yet", inline=True)
        if profile["pinned"]:
            embed.add_field(name="Showcase", value="\n".join(f"🐱 **{cat.name}** · {cat.trait}" for cat in profile["pinned"]), inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)


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
        await interaction.response.send_message(embed=discord.Embed(title="🏅 Collection badges", description=text, color=GOLD), ephemeral=True)
