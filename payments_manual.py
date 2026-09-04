"""Manual payment path (Selar + DM approval), used whenever
config.PAYMENT_MODE == "manual" instead of the Paystack/Stripe flow in
payments.py/resolve_gateway().

Shape: a caller (views_card_pack.py's ultra/card-pack flow, clone_admin.py's
/registerclone flow, etc.) calls start_manual_payment() instead of
resolve_gateway()+initialize_payment(). That logs a pending payment the
same way the automatic path does (db.log_payment, provider="selar") and
DMs every approver (main-bot admins + the relevant clone owner, if any) a
card with Approve/Reject buttons. Tapping Approve calls this payment_type's
entry in UNLOCK_HANDLERS — the SAME unlock functions the automatic path
already calls after a gateway confirms — so nothing about what "paid"
means diverges between the two modes; only how a payment gets *confirmed*
differs.

Not wired to Zapier or any other webhook — confirmation is always a human
tapping a button after checking the Selar dashboard, by design (see
conversation this was scoped in). No public endpoint is needed anywhere
in this module.
"""

import logging
import secrets
from typing import Optional
from urllib.parse import urlencode

import discord

from database import db
from config import SELAR_PRODUCT_LINKS, DISCORD_CLONE_ADMIN_IDS

logger = logging.getLogger(__name__)

PROVIDER = "selar"


def _reference_for(payment_type: str, user_id: int) -> str:
    """Same spirit as the gateway references elsewhere (unique, traceable
    to the user) but generated locally since Selar never hands one back."""
    return f"selar_{payment_type}_{user_id}_{secrets.token_hex(4)}"


def _prefilled_selar_link(payment_type: str, user_id: int) -> Optional[str]:
    """Appends add_to_cart=1 + a synthetic email carrying the Discord user
    id, same trick views_card_pack.py already uses for Paystack
    (f"user_{user.id}@animebot.com") — Selar has no raw 'reference' field,
    so this doubles as one: whatever shows in the Selar dashboard's buyer
    email is the Discord id to match against the DM."""
    base = SELAR_PRODUCT_LINKS.get(payment_type)
    if not base:
        return None
    params = {"add_to_cart": "1", "email": f"user_{user_id}@animebot.com"}
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}{urlencode(params)}"


async def _resolve_approvers(bot: discord.Client, guild_id: Optional[int]) -> list[int]:
    """Main-bot admins always get the DM. If this payment happened inside
    a guild running a Discord clone, that clone's owner is added too —
    looked up via the clone's owner_id, not guesswork."""
    approvers = set(DISCORD_CLONE_ADMIN_IDS)
    clone_id = getattr(bot, "clone_id", None)
    if clone_id:
        clone = await db.get_discord_clone(clone_id)
        if clone and clone.get("owner_id"):
            approvers.add(clone["owner_id"])
    return list(approvers)


async def _send_approval_dms(bot: discord.Client, reference: str, payment_type: str, buyer_id: int,
                              guild_id: Optional[int], clone_id: Optional[int], amount_display: str) -> None:
    """Actually DMs the approvers with the Approve/Reject card. Split out
    from start_manual_payment so it can be triggered by the buyer's
    "I've Paid" tap (BuyerConfirmView below) instead of firing the moment
    checkout starts — we only want you notified once someone claims they
    actually paid, not on every buyer who clicks the Selar link."""
    approver_ids = await _resolve_approvers(bot, guild_id)
    location_line = f"Guild: `{guild_id}`" if guild_id is not None else f"Clone: `#{clone_id}`"
    msg = (
        f"💰 **Manual payment — buyer says they've paid**\n"
        f"Buyer: <@{buyer_id}> (`{buyer_id}`)\n"
        f"Type: `{payment_type}` — {amount_display}\n"
        f"Reference: `{reference}`\n"
        f"{location_line}\n\n"
        f"Check Selar for a matching sale (buyer email `user_{buyer_id}@animebot.com`), "
        f"then Approve or Reject below."
    )
    for admin_id in approver_ids:
        try:
            admin_user = await bot.fetch_user(admin_id)
            await admin_user.send(
                msg,
                view=ManualApprovalView(reference, payment_type, buyer_id, guild_id, clone_id, amount_display),
            )
        except discord.HTTPException:
            logger.warning(f"[manual-pay] couldn't DM approver {admin_id} for reference {reference}")


class BuyerConfirmView(discord.ui.View):
    """Sent to the BUYER alongside the Selar pay link. They tap this only
    after actually completing checkout — that's what triggers the
    approval DM(s), instead of firing one the moment they start checkout
    (which would fire for every click, paid or not, and give no signal
    about whether they actually finished)."""

    def __init__(self, reference: str, payment_type: str, buyer_id: int,
                 guild_id: Optional[int], clone_id: Optional[int], amount_display: str):
        super().__init__(timeout=None)
        self.reference = reference
        self.payment_type = payment_type
        self.buyer_id = buyer_id
        self.guild_id = guild_id
        self.clone_id = clone_id
        self.amount_display = amount_display

    @discord.ui.button(label="✅ I've Paid", style=discord.ButtonStyle.success, custom_id="manual_pay_ive_paid")
    async def ive_paid(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.buyer_id:
            await interaction.response.send_message("This isn't your payment to confirm.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)

        pending = await db.get_latest_pending_payment(self.buyer_id, self.payment_type, chat_id=self.guild_id)
        if not pending or pending.get("paystack_reference") != self.reference:
            await interaction.followup.send(
                "Couldn't find this pending payment anymore — it may already be resolved.", ephemeral=True
            )
            return

        await _send_approval_dms(
            interaction.client, self.reference, self.payment_type, self.buyer_id,
            self.guild_id, self.clone_id, self.amount_display,
        )

        button.disabled = True
        button.label = "⏳ Reported — awaiting confirmation"
        try:
            await interaction.message.edit(view=self)
        except (discord.NotFound, discord.Forbidden):
            # Original message/DM channel gone (e.g. buyer deleted the DM,
            # or Discord returns 403 Missing Access once the DM channel is
            # no longer reachable — same "harmless" case as NotFound, just
            # a different error code) — the approval DM already went out
            # above; just skip the visual update instead of crashing the
            # interaction.
            pass
        await interaction.followup.send(
            "Thanks — flagged for review. You'll get a DM as soon as it's confirmed.", ephemeral=True
        )


class ManualApprovalView(discord.ui.View):
    """Sent to every approver DM. Only the Approve/Reject tap matters —
    this view has no persistent custom_id registration (same reasoning as
    BuyCardPackView: never posted publicly, so an on-restart re-register
    isn't needed — if the bot restarts mid-review, the approver can just
    check the pending payment manually and re-run the unlock)."""

    def __init__(self, reference: str, payment_type: str, buyer_id: int,
                 guild_id: Optional[int], clone_id: Optional[int], amount_display: str):
        super().__init__(timeout=None)
        self.reference = reference
        self.payment_type = payment_type
        self.buyer_id = buyer_id
        self.guild_id = guild_id
        self.clone_id = clone_id
        self.amount_display = amount_display

    @discord.ui.button(label="✅ Approve", style=discord.ButtonStyle.success, custom_id="manual_pay_approve")
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        pending = await db.get_latest_pending_payment(self.buyer_id, self.payment_type, chat_id=self.guild_id)
        if not pending or pending.get("paystack_reference") != self.reference:
            # Reference already handled (approved/rejected elsewhere) or gone.
            await interaction.followup.send("This payment's already been resolved or wasn't found.", ephemeral=True)
            return

        handler = UNLOCK_HANDLERS.get(self.payment_type)
        if handler is None:
            await interaction.followup.send(
                f"No unlock handler wired for `{self.payment_type}` yet — approve manually in code.", ephemeral=True
            )
            return

        await db.mark_payment_paid(self.reference)
        await handler(self.reference, self.buyer_id, self.guild_id, self.clone_id)

        for item in self.children:
            item.disabled = True
        await interaction.message.edit(
            content=f"{interaction.message.content}\n\n✅ **Approved** by {interaction.user.mention}", view=self
        )
        await _notify_buyer(interaction.client, self.buyer_id, self.payment_type, approved=True)

    @discord.ui.button(label="❌ Reject", style=discord.ButtonStyle.danger, custom_id="manual_pay_reject")
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        pool_row = await db.get_latest_pending_payment(self.buyer_id, self.payment_type, chat_id=self.guild_id)
        if not pool_row or pool_row.get("paystack_reference") != self.reference:
            await interaction.followup.send("This payment's already been resolved or wasn't found.", ephemeral=True)
            return

        for item in self.children:
            item.disabled = True
        await interaction.message.edit(
            content=f"{interaction.message.content}\n\n❌ **Rejected** by {interaction.user.mention}", view=self
        )
        await _notify_buyer(interaction.client, self.buyer_id, self.payment_type, approved=False)


async def _notify_buyer(bot: discord.Client, buyer_id: int, payment_type: str, approved: bool) -> None:
    try:
        user = await bot.fetch_user(buyer_id)
        if approved:
            await user.send(f"✅ Your payment for **{payment_type}** was confirmed and applied — enjoy!")
        else:
            await user.send(
                f"❌ We couldn't confirm your payment for **{payment_type}**. "
                f"If you already paid, contact support with your reference."
            )
    except discord.HTTPException:
        logger.warning(f"[manual-pay] couldn't DM buyer {buyer_id} about {payment_type} decision")


async def start_manual_payment(interaction: discord.Interaction, payment_type: str,
                                amount_display: str, guild_id: Optional[int] = None) -> None:
    """Call after interaction.response.defer(ephemeral=True, thinking=True) —
    mirrors start_card_pack_payment's calling convention in views_card_pack.py.

    guild_id: pass the guild this purchase is FOR when it's a whole-guild
    unlock (card pack, ultra pack) — None for account-level purchases
    (discord_clone). Matches how log_payment's chat_id is already used
    elsewhere, so has_paid()/get_latest_pending_payment() scoping stays
    consistent between the manual and automatic paths.
    """
    user = interaction.user
    link = _prefilled_selar_link(payment_type, user.id)
    if not link:
        await interaction.followup.send(
            "Manual payments aren't set up for this yet — please try again later.", ephemeral=True
        )
        logger.error(f"[manual-pay] no SELAR_PRODUCT_LINKS entry for payment_type={payment_type}")
        return

    reference = _reference_for(payment_type, user.id)
    await db.log_payment(
        user.id, 0.0, reference, status="pending",
        payment_type=payment_type, chat_id=guild_id, provider=PROVIDER,
    )

    clone_id = getattr(interaction.client, "clone_id", None)
    confirm_view = BuyerConfirmView(reference, payment_type, user.id, guild_id, clone_id, amount_display)
    confirm_view.add_item(discord.ui.Button(label="💳 Pay on Selar", url=link, style=discord.ButtonStyle.link))
    await interaction.followup.send(
        f"Pay **{amount_display}** on Selar using the button below. "
        f"Once you've completed checkout, tap **I've Paid** so it gets reviewed and confirmed.",
        view=confirm_view, ephemeral=True,
    )


# ─────────────────────────────────────────────────────────────────────
# Unlock handlers — reused as-is from the automatic path. Each takes
# (reference, buyer_id, guild_id, clone_id).
# ─────────────────────────────────────────────────────────────────────

async def _unlock_welcome_card_pack(reference: str, buyer_id: int, guild_id: Optional[int], clone_id: Optional[int]):
    await db.unlock_welcome_card_pack(guild_id, clone_id=clone_id)


async def _unlock_ultra_pack(reference: str, buyer_id: int, guild_id: Optional[int], clone_id: Optional[int]):
    await db.unlock_ultra_pack(guild_id, clone_id=clone_id)


async def _unlock_discord_clone(reference: str, buyer_id: int, guild_id: Optional[int], clone_id: Optional[int]):
    """discord_clone's pending row (token, bot_user_id, etc.) was already
    stashed by clone_admin.py BEFORE payment via
    store_discord_clone_pending_payment(reference=reference, ...) — this
    mirrors what api/paystack_webhook.py's discord_clone case does, and is
    idempotent the same way."""
    await db.complete_discord_clone_pending_payment(reference)


async def _unlock_discord_clone_monetization(reference: str, buyer_id: int, guild_id: Optional[int], clone_id: Optional[int]):
    """Mirrors activate_discord_monetization_subscription_by_reference's
    existing role as the webhook backstop for /clonemonetize activate —
    looks the target clone up by payment_reference (stashed by
    db.start_discord_monetization_payment before payment) instead of
    needing the target clone_id passed in here."""
    from config import CLONE_MONETIZATION_DAYS
    await db.activate_discord_monetization_subscription_by_reference(reference, days=CLONE_MONETIZATION_DAYS)


UNLOCK_HANDLERS = {
    "welcome_card_pack": _unlock_welcome_card_pack,
    "ultra_welcome_pack": _unlock_ultra_pack,
    "discord_clone": _unlock_discord_clone,
    "discord_clone_monetization": _unlock_discord_clone_monetization,
}
