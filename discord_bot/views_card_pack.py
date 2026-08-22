"""
Payment flow for the premium welcome-card pack — a one-time, whole-GUILD
unlock (not per-user like discord_bot/views.py's PremiumPayView/
discord_premium_groups) that lets an admin pick one of the extra card
themes in modules/welcome_card.py's PREMIUM_THEMES via /welcome theme.

Reuses the same generic payment_logs plumbing as views.py (log_payment/
has_paid/mark_payment_paid/get_latest_pending_payment) under its own
payment_type so it never collides with premium-group or other paywalled
features, but on success calls db.unlock_welcome_card_pack() instead of
granting a role — there's no role here, just a config flag checked by
welcome.py's `theme` command and read at render time.

Only a Manage Server admin can trigger/verify a purchase (see welcome.py's
`buypack` command, which is the only place BuyCardPackView gets sent) —
unlike PremiumPayView this is never posted as a persistent public button,
so there's no need for a fixed custom_id/on-restart re-registration.
"""

import asyncio
import logging

import discord

from database import db
from payments import resolve_gateway, gateway_charge_amount, charge_error_message
from config import WELCOME_CARD_PACK_FEE_GHS

logger = logging.getLogger(__name__)

PAYMENT_TYPE = "welcome_card_pack"


def _clone_id_of(interaction: discord.Interaction):
    return getattr(interaction.client, "clone_id", None)


async def start_card_pack_payment(interaction: discord.Interaction):
    """Kicks off a Paystack transaction for this guild's card pack.
    Call after interaction.response.defer(ephemeral=True, thinking=True)."""
    guild_id = interaction.guild_id
    user = interaction.user

    config = await db.get_welcome_config(guild_id, clone_id=_clone_id_of(interaction))
    if config.get("card_pack_unlocked"):
        await interaction.followup.send(
            "This server already owns the premium welcome-card pack — pick a look with `/welcome theme`.",
            ephemeral=True,
        )
        return

    from config import DISCORD_CLONE_ADMIN_IDS
    if user.id in DISCORD_CLONE_ADMIN_IDS:
        await db.unlock_welcome_card_pack(guild_id, clone_id=_clone_id_of(interaction))
        await interaction.followup.send(
            "You're the bot owner — premium card pack unlocked without payment. Pick a look with `/welcome theme`.",
            ephemeral=True,
        )
        return

    price = float(WELCOME_CARD_PACK_FEE_GHS)
    clone_id = _clone_id_of(interaction) or 0
    gateway, api_key, provider = await resolve_gateway(clone_id, platform="discord")
    email = f"user_{user.id}@animebot.com"

    charge = gateway_charge_amount(provider, price)
    if charge.get("error"):
        await interaction.followup.send(charge_error_message(charge), ephemeral=True)
        return

    payment_result = await asyncio.to_thread(
        gateway.initialize_payment,
        email,
        charge["amount_minor_units"],
        user.id,
        f"WelcomeCardPack_{user.id}_{guild_id}",
        payment_type=PAYMENT_TYPE,
        extra_metadata={"guild_id": guild_id, "provider": "discord"},
        api_key=api_key,
        currency=charge["currency"],
    )

    if not payment_result or payment_result.get("status") != "success":
        await interaction.followup.send("Couldn't start a payment right now — please try again shortly.", ephemeral=True)
        return

    reference = payment_result["reference"]
    payment_link = payment_result["authorization_url"]

    await db.log_payment(
        user.id, price, reference, status="pending",
        payment_type=PAYMENT_TYPE, chat_id=guild_id,
        provider=provider,
    )

    charged_amount_display = (
        f"GHS {price:g}" if charge["currency"] == "GHS"
        else f"{charge['amount_minor_units'] / 100:.2f} {charge['currency'].upper()} (converted from GHS {price:g})"
    )
    embed = discord.Embed(
        title="🎨 Premium Welcome-Card Pack",
        description=(
            f"**Amount:** {charged_amount_display}\n\n"
            f"Unlocks the extra welcome-card looks for this server (one-time, applies to every future join).\n\n"
            f"Tap **Pay** below, complete checkout, then come back and tap **Verify**."
        ),
        color=discord.Color.gold(),
    )
    view = VerifyCardPackPaymentView()
    view.add_item(discord.ui.Button(label="💳 Pay Now", url=payment_link, style=discord.ButtonStyle.link))
    await interaction.followup.send(embed=embed, view=view, ephemeral=True)


class VerifyCardPackPaymentView(discord.ui.View):
    """Ephemeral "I've Paid — Verify" button sent right after checkout
    starts. Not persistent (no fixed custom_id) since it's only ever handed
    to the admin who just triggered /welcome buypack, unlike the public,
    always-live PremiumPayView/VerifyPaymentView in views.py."""

    def __init__(self):
        super().__init__(timeout=600)

    @discord.ui.button(label="✅ I've Paid — Verify", style=discord.ButtonStyle.success)
    async def verify(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild_id = interaction.guild_id
        user = interaction.user

        if guild_id is None:
            await interaction.followup.send(
                "This button only works inside the server you're buying the pack for.", ephemeral=True
            )
            return

        pending = await db.get_latest_pending_payment(user.id, PAYMENT_TYPE, guild_id)
        if not pending:
            await interaction.followup.send(
                "I don't see a pending card-pack payment for you here — run `/welcome buypack` first.",
                ephemeral=True,
            )
            return

        reference = pending["paystack_reference"]
        clone_id = _clone_id_of(interaction) or 0
        from payments import resolve_gateway_for_provider
        gateway, api_key = await resolve_gateway_for_provider(clone_id, pending.get("provider") or "paystack", platform="discord")
        result = await asyncio.to_thread(gateway.verify_payment, reference, api_key=api_key)

        if result and result.get("status") == "success":
            await db.mark_payment_paid(reference)
            await db.unlock_welcome_card_pack(guild_id, clone_id=_clone_id_of(interaction))
            await interaction.followup.send(
                "✅ Payment confirmed — premium card pack unlocked! Pick a look with `/welcome theme`.",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                "Payment not confirmed yet. If you just paid, wait a few seconds and tap Verify again.",
                ephemeral=True,
            )
