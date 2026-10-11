from __future__ import annotations

import discord

from modules import cat_features
from modules import cat_wallet

PINK = discord.Color.from_rgb(255, 182, 193)
GOLD = discord.Color.gold()


def feature_button_view() -> discord.ui.View:
    view = discord.ui.View(timeout=300)
    view.add_item(CareButton())
    view.add_item(CosmeticsButton())
    view.add_item(ProfileButton())
    view.add_item(QuestsButton())
    view.add_item(BadgesButton())
    view.add_item(AuctionButton())
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


class CosmeticsButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Cosmetics", emoji="✨", style=discord.ButtonStyle.secondary, custom_id="cat_feature:cosmetics")

    async def callback(self, interaction: discord.Interaction):
        try:
            items = await cat_features.cosmetics()
        except Exception:
            await interaction.response.send_message("The cosmetic shop is temporarily unavailable.", ephemeral=True)
            return
        embed = discord.Embed(title="✨ Cat cosmetics", description="Collect a look for your public profile.", color=PINK)
        embed.add_field(name="Available", value="\n".join(
            f"{item.emoji} **{item.name}** — {item.price_coins:,} coins" + (f" + {item.price_diamonds} diamonds" if item.price_diamonds else "")
            for item in items) or "Nothing in stock yet.", inline=False)
        embed.set_footer(text="Cosmetics are play-only rewards.")
        await interaction.response.send_message(embed=embed, ephemeral=True)


class QuestsButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Daily quests", emoji="🎯", style=discord.ButtonStyle.primary, custom_id="cat_feature:quests")

    async def callback(self, interaction: discord.Interaction):
        from modules import cat_phase2
        try:
            items = await cat_phase2.quests(interaction.user.id, getattr(interaction.client, "clone_id", None))
        except Exception:
            await interaction.response.send_message("🎯 Quests are temporarily unavailable.", ephemeral=True)
            return
        lines = [f"{('✅' if q.progress >= q.target else '🟦')} **{q.title}** — {q.progress}/{q.target} · 💎 {q.reward}" + (" · claimed" if q.claimed else "") for q in items]
        embed = discord.Embed(title="🎯 Daily cat quests", description="Complete these before UTC midnight for diamond rewards.\n\n" + "\n".join(lines), color=GOLD)
        await interaction.response.send_message(embed=embed, ephemeral=True)


class BadgesButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Badges", emoji="🏅", style=discord.ButtonStyle.secondary, custom_id="cat_feature:badges")

    async def callback(self, interaction: discord.Interaction):
        from modules import cat_phase2
        try:
            awarded = await cat_phase2.award_badges(interaction.user.id, getattr(interaction.client, "clone_id", None))
        except Exception:
            await interaction.response.send_message("🏅 Badges are temporarily unavailable.", ephemeral=True)
            return
        text = ", ".join(awarded) if awarded else "No new badges today — keep collecting!"
        await interaction.response.send_message(f"🏅 Collection check complete! {text}", ephemeral=True)


class AuctionButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Auctions", emoji="🔨", style=discord.ButtonStyle.secondary, custom_id="cat_feature:auctions")

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_message("🔨 Auctions are ready for 24-hour bids. Open a listing from My cats to start one.", ephemeral=True)


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
