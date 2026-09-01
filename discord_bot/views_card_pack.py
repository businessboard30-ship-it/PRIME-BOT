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
from payments import resolve_gateway
from config import WELCOME_CARD_PACK_FEE_USD, ULTRA_PACK_FEE_USD
import utils.currency as fx

logger = logging.getLogger(__name__)

PAYMENT_TYPE = "welcome_card_pack"
ULTRA_PAYMENT_TYPE = "ultra_welcome_pack"


def _clone_id_of(interaction: discord.Interaction):
    return getattr(interaction.client, "clone_id", None)


async def _resolve_currency(interaction: discord.Interaction) -> str:
    """Same preference order as discover_players.py's helper: explicit
    /currency set > best-effort Discord locale guess > USD. Only used for
    the Paystack path — Stripe always charges in USD directly since the
    base price already is USD, so there's nothing to convert."""
    stored = await db.get_user_currency(interaction.user.id)
    if stored:
        return stored
    guessed = fx.currency_from_locale(getattr(interaction, "locale", None))
    return guessed or "USD"


async def start_card_pack_payment(interaction: discord.Interaction):
    """Kicks off a transaction for this guild's card pack.
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

    from config import PAYMENT_MODE
    if PAYMENT_MODE == "manual":
        from payments_manual import start_manual_payment
        await start_manual_payment(
            interaction, "welcome_card_pack", f"${WELCOME_CARD_PACK_FEE_USD:g} USD", guild_id=guild_id
        )
        return
    logger.warning(
        f"[card-pack] PAYMENT_MODE={PAYMENT_MODE!r} (not 'manual') — "
        f"routing user {user.id} guild {guild_id} through the auto gateway instead of Selar."
    )

    price_usd = float(WELCOME_CARD_PACK_FEE_USD)
    clone_id = _clone_id_of(interaction) or 0
    gateway, api_key, provider = await resolve_gateway(clone_id, platform="discord")
    email = f"user_{user.id}@animebot.com"

    if provider == "stripe":
        # Base price is already USD — no conversion needed, unlike the old
        # GHS-based flow which had to live-convert GHS->USD for Stripe.
        amount_minor_units = round(price_usd * 100)
        charge_currency = "usd"
    else:
        target_currency = await _resolve_currency(interaction)
        amount_minor_units, charge_currency = fx.usd_to_minor_units(price_usd, target_currency)

    payment_result = await asyncio.to_thread(
        gateway.initialize_payment,
        email,
        amount_minor_units,
        user.id,
        f"WelcomeCardPack_{user.id}_{guild_id}",
        payment_type=PAYMENT_TYPE,
        extra_metadata={"guild_id": guild_id, "provider": "discord"},
        api_key=api_key,
        currency=charge_currency,
    )

    if not payment_result or payment_result.get("status") != "success":
        logger.error(
            f"[card-pack] gateway.initialize_payment failed for user {user.id} guild {guild_id} "
            f"provider={provider!r} api_key_set={bool(api_key)} result={payment_result!r}"
        )
        await interaction.followup.send("Couldn't start a payment right now — please try again shortly.", ephemeral=True)
        return

    reference = payment_result["reference"]
    payment_link = payment_result["authorization_url"]

    await db.log_payment(
        user.id, price_usd, reference, status="pending",
        payment_type=PAYMENT_TYPE, chat_id=guild_id,
        provider=provider,
    )

    charged_amount_display = (
        f"${price_usd:g} USD" if charge_currency.upper() == "USD"
        else f"{amount_minor_units / fx.MINOR_UNIT_MULTIPLIER.get(charge_currency, 100):.2f} {charge_currency.upper()} (≈ ${price_usd:g} USD)"
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


async def start_ultra_pack_payment(interaction: discord.Interaction):
    """Same shape as start_card_pack_payment above, for the SEPARATE ultra
    pack — this one unlocks /welcome custombg (own png/jpeg background)
    rather than the fixed artist themes, so it's gated on
    ultra_pack_unlocked, not card_pack_unlocked, and has its own price/
    payment_type so a guild can own either pack independently.
    Call after interaction.response.defer(ephemeral=True, thinking=True)."""
    guild_id = interaction.guild_id
    user = interaction.user

    config = await db.get_welcome_config(guild_id, clone_id=_clone_id_of(interaction))
    if config.get("ultra_pack_unlocked"):
        await interaction.followup.send(
            "This server already owns the ultra pack — set your background with `/welcome custombg`.",
            ephemeral=True,
        )
        return

    from config import DISCORD_CLONE_ADMIN_IDS
    if user.id in DISCORD_CLONE_ADMIN_IDS:
        await db.unlock_ultra_pack(guild_id, clone_id=_clone_id_of(interaction))
        await interaction.followup.send(
            "You're the bot owner — ultra pack unlocked without payment. Set your background with `/welcome custombg`.",
            ephemeral=True,
        )
        return

    from config import PAYMENT_MODE
    if PAYMENT_MODE == "manual":
        from payments_manual import start_manual_payment
        await start_manual_payment(
            interaction, "ultra_welcome_pack", f"${ULTRA_PACK_FEE_USD:g} USD", guild_id=guild_id
        )
        return
    logger.warning(
        f"[ultra-pack] PAYMENT_MODE={PAYMENT_MODE!r} (not 'manual') — "
        f"routing user {user.id} guild {guild_id} through the auto gateway instead of Selar."
    )

    price_usd = float(ULTRA_PACK_FEE_USD)
    clone_id = _clone_id_of(interaction) or 0
    gateway, api_key, provider = await resolve_gateway(clone_id, platform="discord")
    email = f"user_{user.id}@animebot.com"

    if provider == "stripe":
        amount_minor_units = round(price_usd * 100)
        charge_currency = "usd"
    else:
        target_currency = await _resolve_currency(interaction)
        amount_minor_units, charge_currency = fx.usd_to_minor_units(price_usd, target_currency)

    payment_result = await asyncio.to_thread(
        gateway.initialize_payment,
        email,
        amount_minor_units,
        user.id,
        f"UltraWelcomePack_{user.id}_{guild_id}",
        payment_type=ULTRA_PAYMENT_TYPE,
        extra_metadata={"guild_id": guild_id, "provider": "discord"},
        api_key=api_key,
        currency=charge_currency,
    )

    if not payment_result or payment_result.get("status") != "success":
        logger.error(
            f"[ultra-pack] gateway.initialize_payment failed for user {user.id} guild {guild_id} "
            f"provider={provider!r} api_key_set={bool(api_key)} result={payment_result!r}"
        )
        await interaction.followup.send("Couldn't start a payment right now — please try again shortly.", ephemeral=True)
        return

    reference = payment_result["reference"]
    payment_link = payment_result["authorization_url"]

    await db.log_payment(
        user.id, price_usd, reference, status="pending",
        payment_type=ULTRA_PAYMENT_TYPE, chat_id=guild_id,
        provider=provider,
    )

    charged_amount_display = (
        f"${price_usd:g} USD" if charge_currency.upper() == "USD"
        else f"{amount_minor_units / fx.MINOR_UNIT_MULTIPLIER.get(charge_currency, 100):.2f} {charge_currency.upper()} (≈ ${price_usd:g} USD)"
    )
    embed = discord.Embed(
        title="🖼️ Ultra Welcome Pack",
        description=(
            f"**Amount:** {charged_amount_display}\n\n"
            f"Unlocks `/welcome custombg` — point the welcome card at YOUR OWN png/jpeg for fully "
            f"personalized branding, instead of picking from the fixed card-pack looks (one-time, applies "
            f"to every future join).\n\n"
            f"Tap **Pay** below, complete checkout, then come back and tap **Verify**."
        ),
        color=discord.Color.gold(),
    )
    view = VerifyUltraPackPaymentView()
    view.add_item(discord.ui.Button(label="💳 Pay Now", url=payment_link, style=discord.ButtonStyle.link))
    await interaction.followup.send(embed=embed, view=view, ephemeral=True)


class VerifyUltraPackPaymentView(discord.ui.View):
    """Ultra-pack counterpart to VerifyCardPackPaymentView below — same
    ephemeral, non-persistent, admin-only verify button, just checking the
    ultra_welcome_pack payment_type and calling db.unlock_ultra_pack()."""

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

        pending = await db.get_latest_pending_payment(user.id, ULTRA_PAYMENT_TYPE, guild_id)
        if not pending:
            await interaction.followup.send(
                "I don't see a pending ultra-pack payment for you here — run `/welcome buyultra` first.",
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
            await db.unlock_ultra_pack(guild_id, clone_id=_clone_id_of(interaction))
            # Best-effort: flips an already-open /welcome setup wizard's
            # "Buy Ultra Pack" button over to "Ultra Pack ✅" right away,
            # instead of leaving it stuck showing locked until someone
            # happens to touch another component and trigger a rerender.
            from discord_bot.cogs._views_welcome import refresh_posted_wizard
            await refresh_posted_wizard(interaction.client, guild_id, clone_id=_clone_id_of(interaction))
            await interaction.followup.send(
                "✅ Payment confirmed — ultra pack unlocked! Set your background with `/welcome custombg`.",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                "Payment not confirmed yet. If you just paid, wait a few seconds and tap Verify again.",
                ephemeral=True,
            )


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
