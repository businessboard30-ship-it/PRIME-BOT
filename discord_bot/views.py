"""
Discord equivalent of handlers/premium_group_handler.py — now supporting
multiple, independently-priced premium groups per guild (per clone) instead
of a single hardcoded tier.

PremiumPayView (the persistent "Pay to Join Premium" button) and
VerifyPaymentView (the follow-up "I've Paid — Verify" button) both use FIXED
custom_ids and are registered once as persistent views on bot startup
(bot.add_view(...) in discord_bot/bot.py), so the buttons keep working after
a bot restart on every message they were ever attached to — exactly like
Telegram's callback_data-based buttons never expire.

Guild context comes from the interaction itself (interaction.guild_id) —
NOT encoded into custom_id — so one persistent view instance works correctly
across every guild the button is posted in. Which premium group a purchase
is for is resolved at click time (from the group list), never baked into a
button's custom_id, since a guild's set of groups can change at any time.
"""

import logging
import asyncio
from typing import Optional

import discord

from database import db
from payments import resolve_gateway, resolve_gateway_for_provider, gateway_charge_amount, charge_error_message

logger = logging.getLogger(__name__)

PAYMENT_TYPE = "premium_group_join"


def _clone_id_of(interaction: discord.Interaction) -> Optional[int]:
    """The running bot's own clone_id (None = main bot) — set once at
    startup on the client itself, see discord_bot/bot.py."""
    return getattr(interaction.client, "clone_id", None)


async def grant_premium_role(member: discord.Member, role_id: int, reason: str) -> bool:
    """Idempotent role grant via the live gateway connection (used when we
    already have a discord.py Member object in hand, e.g. inside the bot
    process). The webhook process instead uses discord_bot/role_grant.py's
    REST-based grant_role(), since it has no gateway connection."""
    if not role_id:
        logger.warning(f"[discord] No role configured for this premium group in guild {member.guild.id}")
        return False
    role = member.guild.get_role(role_id)
    if role is None:
        logger.warning(f"[discord] Configured role {role_id} not found in guild {member.guild.id}")
        return False
    if role in member.roles:
        return True  # already has it — idempotent no-op
    try:
        await member.add_roles(role, reason=reason)
        return True
    except discord.Forbidden:
        logger.error(f"[discord] Missing permission to grant role {role_id} in guild {member.guild.id}")
        return False
    except discord.HTTPException as e:
        logger.error(f"[discord] Failed to grant role {role_id} in guild {member.guild.id}: {e}")
        return False


async def _start_payment_for_group(interaction: discord.Interaction, group: dict):
    """Shared by PremiumPayView's button and the multi-group Select menu:
    kicks off a Paystack transaction for one specific premium group and
    hands the user a Pay + Verify message. Discord clone admins
    (DISCORD_CLONE_ADMIN_IDS) skip payment entirely and get the role
    granted immediately, same owner-bypass convention used elsewhere."""
    user = interaction.user
    guild_id = interaction.guild_id

    from config import DISCORD_CLONE_ADMIN_IDS
    if user.id in DISCORD_CLONE_ADMIN_IDS:
        member = interaction.guild.get_member(user.id) if interaction.guild else None
        if member is not None:
            await grant_premium_role(member, group["role_id"], reason="owner bypass — DISCORD_CLONE_ADMIN_IDS")
        await interaction.followup.send(
            f"You're the bot owner — **{group['name']}** granted without payment.", ephemeral=True
        )
        return

    if await db.has_paid(user.id, PAYMENT_TYPE, chat_id=guild_id, group_id=group["group_id"]):
        member = interaction.guild.get_member(user.id) if interaction.guild else None
        if member is not None:
            await grant_premium_role(member, group["role_id"], reason="already paid — idempotent re-grant")
        await interaction.followup.send(
            f"You've already paid for **{group['name']}** in this server — you're all set!", ephemeral=True
        )
        return

    price = float(group["fee_ghs"])
    clone_id = _clone_id_of(interaction) or 0
    gateway, api_key, _provider = await resolve_gateway(clone_id, platform="discord")
    email = f"user_{user.id}@animebot.com"

    charge = gateway_charge_amount(_provider, price)
    if charge.get("error"):
        await interaction.followup.send(charge_error_message(charge), ephemeral=True)
        return

    # gateway.initialize_payment() is a blocking `requests.post` call — push
    # it to a thread so it doesn't stall the bot's event loop for other
    # users while Paystack responds.
    payment_result = await asyncio.to_thread(
        gateway.initialize_payment,
        email,
        charge["amount_minor_units"],
        user.id,
        f"PremiumGroup_{user.id}_{guild_id}_{group['group_id']}",
        payment_type=PAYMENT_TYPE,
        extra_metadata={"guild_id": guild_id, "group_id": group["group_id"], "provider": "discord"},
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
        payment_type=PAYMENT_TYPE, chat_id=guild_id, group_id=group["group_id"],
        provider=_provider,
    )

    charged_amount_display = (
        f"GHS {price:g}" if charge["currency"] == "GHS"
        else f"{charge['amount_minor_units'] / 100:.2f} {charge['currency'].upper()} (converted from GHS {price:g})"
    )
    embed = discord.Embed(
        title=f"💎 {group['name']} Payment",
        description=f"**Amount:** {charged_amount_display}\n\nTap **Pay** below, complete checkout, then come back and tap **Verify**.",
        color=discord.Color.gold(),
    )
    view = VerifyPaymentView()
    view.add_item(discord.ui.Button(label="💳 Pay Now", url=payment_link, style=discord.ButtonStyle.link))
    await interaction.followup.send(embed=embed, view=view, ephemeral=True)


class PremiumGroupSelect(discord.ui.Select):
    """Shown (ephemerally, non-persistent — it's only alive for the
    duration of one interaction) when a guild has more than one active
    premium group, so the member picks which one to pay for."""

    def __init__(self, groups: list):
        options = [
            discord.SelectOption(
                label=g["name"][:100],
                description=f"GHS {float(g['fee_ghs']):g}",
                value=str(g["group_id"]),
            )
            for g in groups[:25]  # Discord hard-caps select menus at 25 options
        ]
        super().__init__(placeholder="Choose a premium group to pay for…", options=options)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        group = await db.get_premium_group(int(self.values[0]))
        if not group or not group["active"]:
            await interaction.followup.send("That premium group is no longer available.", ephemeral=True)
            return
        await _start_payment_for_group(interaction, group)


class PremiumGroupSelectView(discord.ui.View):
    def __init__(self, groups: list):
        super().__init__(timeout=120)
        self.add_item(PremiumGroupSelect(groups))


class VerifyPaymentView(discord.ui.View):
    """Sent (ephemerally) right after a Paystack transaction is initialized.
    Mirrors handlers/premium_group_handler.py's "I've Paid — Verify" button."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="✅ I've Paid — Verify",
        style=discord.ButtonStyle.success,
        custom_id="premium_pay_verify",
    )
    async def verify(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild_id = interaction.guild_id
        user = interaction.user

        if guild_id is None:
            await interaction.followup.send(
                "This button only works inside the server you're paying to join.", ephemeral=True
            )
            return

        # Latest pending payment for this user in this guild, REGARDLESS of
        # which group — a user is only ever expected to have one checkout
        # in flight at a time. The row itself carries group_id, so we don't
        # need it encoded in the button.
        pending = await db.get_latest_pending_payment(user.id, PAYMENT_TYPE, guild_id)
        if not pending:
            await interaction.followup.send(
                "I don't see a pending payment for you here — tap **💎 Pay to Join Premium** first.",
                ephemeral=True,
            )
            return

        group = await db.get_premium_group(pending["group_id"]) if pending.get("group_id") else None
        if not group:
            await interaction.followup.send(
                "That premium group no longer exists — contact an admin about your payment.", ephemeral=True
            )
            return

        reference = pending["paystack_reference"]
        clone_id = _clone_id_of(interaction) or 0
        # Use the SAME provider this payment was started under (stored on
        # the pending row) rather than resolve_gateway()'s "whatever the
        # clone's settings currently say" — otherwise a clone owner
        # switching payment providers between checkout and this tap would
        # strand the payment (verify needs the same key that created it).
        gateway, api_key = await resolve_gateway_for_provider(clone_id, pending.get("provider") or "paystack", platform="discord")
        # gateway.verify_payment() is a blocking `requests` call — run it
        # off the event loop so a slow Paystack response doesn't freeze
        # every other interaction this bot is handling at the same time.
        result = await asyncio.to_thread(gateway.verify_payment, reference, api_key=api_key)

        if result and result.get("status") == "success":
            await db.mark_payment_paid(reference)
            await self._finish_grant(interaction, guild_id, user, group)
        else:
            await interaction.followup.send(
                "Payment not confirmed yet. If you just paid, wait a few seconds and tap Verify again.",
                ephemeral=True,
            )

    @staticmethod
    async def _finish_grant(interaction: discord.Interaction, guild_id: int, user: discord.abc.User, group: dict):
        member = interaction.guild.get_member(user.id) if interaction.guild else None
        if member is None:
            await interaction.followup.send(
                f"✅ Payment confirmed for **{group['name']}**! You'll get the role automatically as soon as you're in the server.",
                ephemeral=True,
            )
            return
        ok = await grant_premium_role(member, group["role_id"], reason=f"{PAYMENT_TYPE} payment verified: {group['name']}")
        if ok:
            await interaction.followup.send(f"✅ Payment confirmed — **{group['name']}** role granted!", ephemeral=True)
        else:
            await interaction.followup.send(
                f"✅ Payment confirmed for **{group['name']}**, but I couldn't grant the role automatically — an admin will sort it out shortly.",
                ephemeral=True,
            )


class PremiumPayView(discord.ui.View):
    """The persistent "💎 Pay to Join Premium" button — Discord equivalent
    of premium_group_handler.py's premium_group_button(), attached to
    whatever welcome/announcement message replaces the Telegram broadcast.

    With multiple groups now supported: if the guild has exactly one active
    group, tapping this jumps straight into that group's checkout (same UX
    as before multi-group support). If it has several, this shows a picker
    first."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="💎 Pay to Join Premium",
        style=discord.ButtonStyle.primary,
        custom_id="premium_pay_init",
    )
    async def pay(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild_id = interaction.guild_id
        if guild_id is None:
            await interaction.followup.send("This only works inside a server.", ephemeral=True)
            return

        clone_id = _clone_id_of(interaction)
        groups = await db.list_premium_groups(guild_id, clone_id=clone_id, active_only=True)
        if not groups:
            await interaction.followup.send(
                "This server hasn't set up a premium group yet — ask an admin to run `/createpremium`.",
                ephemeral=True,
            )
            return

        if len(groups) == 1:
            await _start_payment_for_group(interaction, groups[0])
        else:
            await interaction.followup.send(
                "This server has more than one premium group — pick one:",
                view=PremiumGroupSelectView(groups),
                ephemeral=True,
            )
