# path: discord_bot/cogs/_views_leveling_wallet.py

"""
Boost Wallet UI — attached to /leaderboard alongside the existing "⚡ Boost
XP" (multiplier) button from _views_leveling_boost.py. Three controls:

  1. "💰 Buy Boost" — a StringSelect of config.XP_WALLET_TIERS (plus a
     server-wide option) that kicks off the same manual Selar payment flow
     as every other paid feature (see payments_manual.start_manual_payment).
  2. "🎒 Wallet" — ephemeral balance + expiry check, no payment involved.
  3. "🎁 Gift" — button -> ephemeral UserSelect to pick a recipient -> Modal
     to type an amount. Split into select-then-modal because Discord
     modals can only contain text inputs, never a user/member select, so
     picking WHO and typing HOW MUCH can't happen in one component.

All three are restart-proof DynamicItems, same pattern as BoostXPButton.
"""

import re

import discord

import config as app_config
from database import db

_TIER_ORDER = ("xp_wallet_small", "xp_wallet_medium", "xp_wallet_large", "xp_wallet_mega")
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
        options = [
            discord.SelectOption(
                label=app_config.XP_WALLET_TIERS[key]["label"],
                description=f"${app_config.XP_WALLET_TIERS[key]['fee_usd']:g} USD",
                value=key,
            )
            for key in _TIER_ORDER
        ]
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
        if payment_type in app_config.XP_SERVER_BOOST_TIERS:
            tier = app_config.XP_SERVER_BOOST_TIERS[payment_type]
            price_usd = float(tier["fee_usd"])
            amount_display = f"${tier['fee_usd']:g}"
            title = "⚡ Server XP Boost"
            description = f"Boosts XP for everyone in this server — {tier['multiplier']:g}x for {tier['duration_hours']}h."
        else:
            tier = app_config.XP_WALLET_TIERS[payment_type]
            price_usd = float(tier["fee_usd"])
            amount_display = f"${tier['fee_usd']:g} ({tier['xp']:,} XP)"
            title = "🎒 XP Wallet Top-Up"
            description = f"Adds {tier['xp']:,} XP to your personal wallet — gift it to any member anytime."
        await start_dual_mode_payment(
            interaction, payment_type=payment_type, price_usd=price_usd,
            product_title=title, product_description=description,
            amount_display_manual=amount_display, guild_id=self.guild_id,
        )


class WalletBalanceButton(discord.ui.DynamicItem[discord.ui.Button], template=r"^wallet_xp:(\d+):(-?\d+|-)$"):
    def __init__(self, guild_id: int, clone_id):
        self.guild_id = guild_id
        self.clone_id = clone_id
        super().__init__(discord.ui.Button(
            label="Wallet", emoji="🎒",
            style=discord.ButtonStyle.secondary,
            custom_id=f"wallet_xp:{guild_id}:{_clone_part(clone_id)}",
        ))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        return cls(int(match.group(1)), _parse_clone_part(match.group(2)))

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        wallet = await db.get_xp_wallet(self.guild_id, interaction.user.id, clone_id=self.clone_id)
        if wallet["balance"] <= 0:
            await interaction.followup.send(
                "🎒 Your Boost Wallet is empty. Tap **Buy Boost** to top it up.", ephemeral=True
            )
            return
        expiry_note = ""
        if wallet.get("expires_at"):
            expiry_note = f"\nExpires <t:{int(wallet['expires_at'].timestamp())}:R> if unspent."
        eligible = wallet["balance"] >= app_config.XP_WALLET_MIN_BALANCE_TO_GIFT
        gift_note = (
            "\nYou have enough to gift some of this — tap **Gift**."
            if eligible else
            f"\nNeed at least **{app_config.XP_WALLET_MIN_BALANCE_TO_GIFT:,} XP** in your wallet before you can gift."
        )
        await interaction.followup.send(
            f"🎒 **Boost Wallet** — **{wallet['balance']:,} XP**{expiry_note}{gift_note}",
            ephemeral=True,
        )


class GiftBoostButton(discord.ui.DynamicItem[discord.ui.Button], template=r"^giftboost_xp:(\d+):(-?\d+|-)$"):
    def __init__(self, guild_id: int, clone_id):
        self.guild_id = guild_id
        self.clone_id = clone_id
        super().__init__(discord.ui.Button(
            label="Gift", emoji="🎁",
            style=discord.ButtonStyle.success,
            custom_id=f"giftboost_xp:{guild_id}:{_clone_part(clone_id)}",
        ))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        return cls(int(match.group(1)), _parse_clone_part(match.group(2)))

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        wallet = await db.get_xp_wallet(self.guild_id, interaction.user.id, clone_id=self.clone_id)
        if wallet["balance"] < app_config.XP_WALLET_MIN_BALANCE_TO_GIFT:
            await interaction.followup.send(
                f"You need at least **{app_config.XP_WALLET_MIN_BALANCE_TO_GIFT:,} XP** in your Boost "
                f"Wallet to gift (you have **{wallet['balance']:,}**). Tap **Buy Boost** to top up.",
                ephemeral=True,
            )
            return
        view = discord.ui.View(timeout=180)
        view.add_item(GiftRecipientSelect(self.guild_id, self.clone_id))
        await interaction.followup.send("🎁 Who do you want to gift boost XP to?", view=view, ephemeral=True)


class GiftRecipientSelect(discord.ui.DynamicItem[discord.ui.UserSelect], template=r"^giftrecip_xp:(\d+):(-?\d+|-)$"):
    """Not persistent in practice (lives on a short-lived ephemeral
    followup, not a message that needs to survive a restart), but built as
    a DynamicItem anyway for the same reason every other component in this
    file is — consistency, and it costs nothing."""

    def __init__(self, guild_id: int, clone_id):
        self.guild_id = guild_id
        self.clone_id = clone_id
        super().__init__(discord.ui.UserSelect(
            placeholder="Choose a member",
            custom_id=f"giftrecip_xp:{guild_id}:{_clone_part(clone_id)}",
        ))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        return cls(int(match.group(1)), _parse_clone_part(match.group(2)))

    async def callback(self, interaction: discord.Interaction):
        recipient = self.item.values[0]
        if recipient.id == interaction.user.id:
            await interaction.response.send_message("You can't gift boost XP to yourself.", ephemeral=True)
            return
        if recipient.bot:
            await interaction.response.send_message("You can't gift boost XP to a bot.", ephemeral=True)
            return
        await interaction.response.send_modal(GiftAmountModal(self.guild_id, self.clone_id, recipient))


class GiftAmountModal(discord.ui.Modal, title="Gift Boost XP"):
    amount = discord.ui.TextInput(
        label="How much XP to gift?",
        placeholder=f"1 – {app_config.XP_WALLET_MAX_GIFT_XP:,}",
        max_length=6,
    )

    def __init__(self, guild_id: int, clone_id, recipient: discord.User):
        super().__init__()
        self.guild_id = guild_id
        self.clone_id = clone_id
        self.recipient = recipient

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        raw = str(self.amount.value).strip().replace(",", "")
        if not raw.isdigit():
            await interaction.followup.send("That's not a whole number of XP.", ephemeral=True)
            return
        amount = int(raw)
        if amount <= 0:
            await interaction.followup.send("Amount must be greater than 0.", ephemeral=True)
            return
        if amount > app_config.XP_WALLET_MAX_GIFT_XP:
            await interaction.followup.send(
                f"A single gift can't exceed **{app_config.XP_WALLET_MAX_GIFT_XP:,} XP**.", ephemeral=True
            )
            return

        result = await db.gift_wallet_xp(
            self.guild_id, interaction.user.id, self.recipient.id, amount, clone_id=self.clone_id
        )
        if result is None:
            wallet = await db.get_xp_wallet(self.guild_id, interaction.user.id, clone_id=self.clone_id)
            await interaction.followup.send(
                f"You don't have **{amount:,} XP** available to gift (your balance is **{wallet['balance']:,}**).",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            f"🎁 Gifted **{amount:,} XP** to **{self.recipient.display_name}**! "
            f"Your wallet balance is now **{result['sender_new_balance']:,} XP**.",
            ephemeral=True,
        )

        if result["recipient_new_level"] > result["recipient_old_level"]:
            guild = interaction.guild
            member = guild.get_member(self.recipient.id) if guild else None
            leveling_cog = interaction.client.get_cog("LevelingCog")
            if member and leveling_cog:
                config = await db.get_leveling_config(self.guild_id, clone_id=self.clone_id)
                announce_channel = await leveling_cog._ensure_announce_channel(guild, config, clone_id=self.clone_id)
                channel = announce_channel or interaction.channel
                try:
                    await channel.send(
                        f"🎉 {member.mention} leveled up to **level {result['recipient_new_level']}** "
                        f"(gifted boost XP)!"
                    )
                except discord.Forbidden:
                    pass
                await leveling_cog._grant_level_roles(member, result["recipient_new_level"], clone_id=self.clone_id)


def build_boost_wallet_row(guild_id: int, clone_id) -> discord.ui.ActionRow:
    """Second row of boost controls for the leaderboard, alongside the
    existing multiplier BoostXPButton's row — kept separate since a
    StringSelect can't share an ActionRow with buttons in Components v2
    (same constraint _views_leveling_leaderboard.py's mode_row already
    works around)."""
    select_row = discord.ui.ActionRow()
    select_row.add_item(BuyBoostSelect(guild_id, clone_id))
    return select_row


def build_boost_wallet_buttons_row(guild_id: int, clone_id) -> discord.ui.ActionRow:
    button_row = discord.ui.ActionRow()
    button_row.add_item(WalletBalanceButton(guild_id, clone_id))
    button_row.add_item(GiftBoostButton(guild_id, clone_id))
    return button_row


DYNAMIC_ITEMS = (BuyBoostSelect, WalletBalanceButton, GiftBoostButton, GiftRecipientSelect)
