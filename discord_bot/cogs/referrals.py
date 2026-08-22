"""
Ad/Marketplace Referral System (Phase 4) — Discord commands for
modules/referrals.py.

/referral mycode  — get (or generate) your personal referral code
/referral use     — redeem someone else's code, once, ever
/referral stats    — your referred-user count + tracked (not real) earnings

See modules/referrals.py's module docstring for why "earnings" here are a
tracked ledger figure rather than real payouts: ads_marketplace.py never
actually collects budget_usd/price_usd as real payment, so there's no real
money for a referral commission to be a cut of yet.
"""

import discord
from discord import app_commands
from discord.ext import commands

from modules.referrals import get_or_create_referral_code, use_referral_code, get_referral_stats
from discord_bot.cogs._views_shared import NavCardView, refresh_button


class ReferralsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    referral = app_commands.Group(name="referral", description="Ad/marketplace referral program (tracked, not a real payout)")

    @referral.command(name="mycode", description="Get your referral code")
    async def referral_mycode(self, interaction: discord.Interaction):
        code = await get_or_create_referral_code(interaction.user.id)
        if not code:
            await interaction.response.send_message("❌ Couldn't get a code right now. Try again.", ephemeral=True)
            return
        await interaction.response.send_message(
            f"Your referral code: `{code}`\n"
            f"Share it — if someone redeems it with `/referral use` and later submits an ad or "
            f"marketplace listing, it's logged to your `/referral stats` as a tracked figure.",
            ephemeral=True,
        )

    @referral.command(name="use", description="Redeem someone else's referral code")
    @app_commands.describe(code="The code they shared with you")
    async def referral_use(self, interaction: discord.Interaction, code: str):
        result = await use_referral_code(interaction.user.id, code)
        messages = {
            "applied": "✅ Applied! That referral is now locked in for your account.",
            "already_set": "You've already got a referrer set — this only works once, and it already happened.",
            "self": "You can't use your own referral code.",
            "not_found": "That code doesn't match anyone. Double-check it and try again.",
            "error": "❌ Something went wrong. Try again.",
        }
        await interaction.response.send_message(messages.get(result["reason"], messages["error"]), ephemeral=True)

    @referral.command(name="stats", description="See your referral count and tracked earnings")
    async def referral_stats(self, interaction: discord.Interaction):
        stats = await get_referral_stats(interaction.user.id)
        lines = [
            f"Your code: `{stats['code']}`" if stats["code"] else "Your code: not generated yet — use `/referral mycode`",
            f"Users referred: {stats['referred_count']}",
            f"Tracked earnings: ${stats['tracked_total_usd']:.2f}",
        ]
        if stats["recent"]:
            lines.append("**Recent activity**")
            for r in stats["recent"]:
                lines.append(f"{r['source_type']} #{r['source_id']} — ${r['tracked_amount_usd']:.2f}")
        lines.append("-# Tracked figure only — ads/marketplace listings don't collect real payment yet, so this isn't a real payout balance.")
        buttons = [refresh_button(self, "referral_stats")]
        card = NavCardView("📊 Your referral stats", lines, discord.Color.blurple(), buttons)
        await interaction.response.send_message(view=card, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(ReferralsCog(bot))
