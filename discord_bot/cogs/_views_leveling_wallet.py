# path: discord_bot/cogs/_views_leveling_wallet.py

"""
Buy Boost UI — attached to /leaderboard alongside the existing "⚡ Boost
XP" (multiplier) button from _views_leveling_boost.py. A single "💰 Buy
Boost" StringSelect of config.XP_SERVER_BOOST_TIERS, kicking off the same
manual/dual-mode payment flow as every other paid feature (see
payments_manual.start_dual_mode_payment).

The personal XP Wallet (giftable, stored-value XP credit) and its Gift
flow used to live in this file too — removed entirely, not just
unlinked. A purchasable, person-to-person-transferable balance is exactly
the "credits that can be monetized, re-sold or converted to... digital
goods or services or otherwise exit the virtual world" pattern several
payment processors (Gumroad among them) explicitly prohibit, and no
amount of wording around it changes what the mechanic actually is. The
XP Server Boost tiers that remain are a temporary RATE MULTIPLIER, not a
stored/transferable balance — nothing is held, nothing can be gifted,
nothing "exits" anywhere, so they don't have the same problem.

database.get_xp_wallet / database.gift_wallet_xp still exist (harmless,
unreachable dead code — not deleted from that 10k+-line file to avoid a
risky edit there) but nothing in the bot calls them anymore as of this
file's rewrite.

This DynamicItem is restart-proof, same pattern as BoostXPButton.
"""

import re

import discord

import config as app_config

_SERVER_BOOST_TIER_ORDER = ("xp_server_boost", "xp_server_boost_month")


def _clone_part(clone_id) -> str:
    return "-" if clone_id is None else str(clone_id)


def _parse_clone_part(part: str):
    return None if part == "-" else int(part)


class BuyBoostSelect(discord.ui.DynamicItem[discord.ui.Select], template=r"^buyboost_xp:(\d+):(-?\d+|-)$"):
    """guild_id baked into custom_id, same reasoning as BoostXPButton — this
    survives being clicked from a forwarded/DM'd copy of the message."""

    def __init__(self, guild_id: int, clone_id):
        self.guild_id = guild_id
        self.clone_id = clone_id
        options = []
        for key in _SERVER_BOOST_TIER_ORDER:
            tier = app_config.XP_SERVER_BOOST_TIERS[key]
            options.append(discord.SelectOption(
                label=f"Boost the whole server — {tier['label']}",
                description=f"${tier['fee_usd']:g} USD — {tier['multiplier']:g}x XP for everyone, "
                            f"{tier['duration_hours']}h",
                value=key,
            ))
        super().__init__(discord.ui.Select(
            placeholder="💰 Buy Boost — pick a tier",
            options=options,
            custom_id=f"buyboost_xp:{guild_id}:{_clone_part(clone_id)}",
        ))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        return cls(int(match.group(1)), _parse_clone_part(match.group(2)))

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        from payments_manual import start_dual_mode_payment
        payment_type = self.item.values[0]
        tier = app_config.XP_SERVER_BOOST_TIERS[payment_type]
        price_usd = float(tier["fee_usd"])
        amount_display = f"${tier['fee_usd']:g}"
        title = "⚡ Server XP Boost"
        description = f"Boosts XP for everyone in this server — {tier['multiplier']:g}x for {tier['duration_hours']}h."
        await start_dual_mode_payment(
            interaction, payment_type=payment_type, price_usd=price_usd,
            product_title=title, product_description=description,
            amount_display_manual=amount_display, guild_id=self.guild_id,
        )


def build_boost_wallet_row(guild_id: int, clone_id) -> discord.ui.ActionRow:
    """Second row of boost controls for the leaderboard, alongside the
    existing multiplier BoostXPButton's row — kept separate since a
    StringSelect can't share an ActionRow with buttons in Components v2
    (same constraint _views_leveling_leaderboard.py's mode_row already
    works around)."""
    select_row = discord.ui.ActionRow()
    select_row.add_item(BuyBoostSelect(guild_id, clone_id))
    return select_row


DYNAMIC_ITEMS = (BuyBoostSelect,)
