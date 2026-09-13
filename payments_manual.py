"""Manual payment path (Selar + web confirmation + DM approval), used
whenever config.PAYMENT_MODE == "manual" instead of the Paystack/Stripe
flow in payments.py/resolve_gateway().

Shape: a caller (views_card_pack.py's ultra/card-pack flow, clone_admin.py's
/registerclone flow, etc.) calls start_manual_payment() instead of
resolve_gateway()+initialize_payment(). That logs a pending payment the
same way the automatic path does (db.log_payment, provider="selar") and
sends the buyer a DM with a plain "Pay on Selar" link — nothing else.
Confirmation happens entirely on the web, not in Discord:

  1. Buyer taps "Pay on Selar" in the DM. This is a bare Selar checkout
     link — Selar's product-level "redirect after purchase" is a single
     STATIC url per product (confirmed: nothing is appended to it, no
     per-buyer query params), so unlike the old design there is nothing
     dynamic to bake into the link itself. Each Selar product's own Custom
     Checkout Form asks the buyer to type their Discord username and
     server name — that's the human-readable evidence an admin cross-checks
     against the Selar dashboard later; see _prefilled_selar_link.
  2. The moment the button is shown (before the buyer ever leaves Discord),
     this module already wrote a 'pending' payment_logs row keyed by
     (user_id, payment_type[, chat_id=guild_id][, clone_id]) via
     db.log_payment — that row is what the web step below matches against.
  3. Selar redirects EVERY buyer of that product to the same static
     .../unlock?payment_type=<type> page on the Next.js frontend
     (app/unlock/unlock-status.tsx). That page asks the buyer to sign in
     with Discord (reusing api/discord_login_oauth.py) — this is the only
     identity check the web step has, since Selar's redirect carries none.
  4. Once signed in, the buyer taps "I've Paid". That posts to
     api/selar_submit.py with their OAuth session id + payment_type.
     selar_submit resolves the real Discord user_id from the session
     server-side (never trusts anything the browser sent directly), looks
     up db.get_latest_pending_selar_payment(user_id, payment_type) — the
     row from step 2 — and atomically claims it for review
     (db.claim_manual_payment_for_review; a second tap/reload is a no-op
     instead of a second admin DM), then DMs every approver.
  5. Tapping Approve/Reject in that DM calls this payment_type's entry in
     UNLOCK_HANDLERS — the SAME unlock functions the automatic path
     already calls after a gateway confirms — so nothing about what "paid"
     means diverges between the two modes; only how a payment gets
     *confirmed* differs. The admin still double-checks against the Selar
     dashboard's buyer-typed username/server name before approving —
     that's the actual fraud check; the OAuth match only proves WHICH
     Discord account is tapping "I've Paid", not that they actually paid.

Approve/Reject are discord.ui.DynamicItems (not a plain View), so they
survive a bot restart: the DM itself is sent over plain REST from
api/selar_submit.py (that process has no live gateway connection to build
a discord.ui.View on — see discord_bot/dm_send.py), and whichever process
IS connected to the gateway when a button is actually clicked reconstructs
the item from its custom_id alone, matching every other persistent-button
pattern in this codebase (see discord_bot/cogs/roast.py's
_RoastApproveButton for the closest precedent).

Not wired to a Selar webhook — Selar currently provides no webhook
delivery, so confirmation is always a human tapping Approve after checking
the Selar dashboard. The web step's job is narrower than in the old
signed-redirect design: it no longer proves a specific checkout completed
(Selar's static redirect can't tell us that), it only identifies WHO is
claiming to have paid, cheaply, instead of trusting whatever the buyer's
Discord client sends with zero verification at all.

utils/selar_signing.py and api/selar_redirect.py implement the OLD
signed-redirect design and are no longer part of this live path — left in
place rather than deleted; safe to remove in a follow-up.
"""

import logging
import re
import secrets
from typing import Optional
from urllib.parse import urlencode

import discord

from database import db
from config import SELAR_PRODUCT_LINKS, DISCORD_CLONE_ADMIN_IDS

logger = logging.getLogger(__name__)

PROVIDER = "selar"

_APPROVAL_ID_RE = r"(\d+)"


def _reference_for(payment_type: str, user_id: int) -> str:
    """Same spirit as the gateway references elsewhere (unique, traceable
    to the user) but generated locally since Selar never hands one back."""
    return f"selar_{payment_type}_{user_id}_{secrets.token_hex(4)}"


def _prefilled_selar_link(payment_type: str, user_id: int, guild_id: Optional[int],
                           clone_id: Optional[int], reference: str) -> Optional[str]:
    """Appends add_to_cart=1 + a synthetic email carrying the Discord user
    id, same trick views_card_pack.py already uses for Paystack
    (f"user_{user.id}@animebot.com") — whatever shows in the Selar
    dashboard's buyer email is the Discord id, for an admin eyeballing the
    dashboard directly.

    No redirect_url param anymore: Selar's product-level "redirect after
    purchase" is a single static URL per product (set once, in the Selar
    dashboard itself — see 011_selar_static_redirect_flow.sql), so there is
    nothing dynamic left to append here. guild_id/clone_id/reference are
    unused by the link itself now — they're only needed by the caller to
    write the matching db.log_payment row (see start_manual_payment)."""
    base = SELAR_PRODUCT_LINKS.get(payment_type)
    if not base:
        return None
    params = {
        "add_to_cart": "1",
        "email": f"user_{user_id}@animebot.com",
    }
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}{urlencode(params)}"


async def _resolve_approvers(bot: discord.Client, guild_id: Optional[int],
                              payment_type: Optional[str] = None) -> list[int]:
    """Main-bot admins always get the DM. If this payment happened inside
    a guild running a Discord clone, that clone's owner is added too —
    looked up via the clone's owner_id, not guesswork.

    Carve-out: discord_clone registrations (including clone-of-clone ones
    made through a hosting clone's own "Build Bot" wizard) never add the
    hosting clone's owner here, regardless of which clone process the
    registration happened through — that revenue and approval right
    belongs solely to the main project owner (DISCORD_CLONE_ADMIN_IDS),
    never to whichever clone owner happened to host the sub-clone's
    registration."""
    approvers = set(DISCORD_CLONE_ADMIN_IDS)
    if payment_type == "discord_clone":
        return list(approvers)
    clone_id = getattr(bot, "clone_id", None)
    if clone_id:
        clone = await db.get_discord_clone(clone_id)
        if clone and clone.get("owner_id"):
            approvers.add(clone["owner_id"])
    return list(approvers)


async def send_manual_payment_approval_dms(bot: discord.Client, payment_id: int, reference: str,
                                            payment_type: str, buyer_id: int, guild_id: Optional[int],
                                            clone_id: Optional[int], amount_display: str) -> None:
    """Called from api/selar_submit.py once the buyer's web "I've Paid"
    submission has been atomically claimed (db.claim_manual_payment_for_review
    already returned True for this reference — this function assumes that
    already happened and does not re-check it, so callers must not invoke
    it speculatively). DMs every approver a persistent Approve/Reject card
    keyed by payment_id, the payment_logs primary key, since DynamicItem
    custom_ids need something short and numeric rather than the full
    reference string."""
    approver_ids = await _resolve_approvers(bot, guild_id, payment_type=payment_type)
    location_line = f"Guild: `{guild_id}`" if guild_id is not None else f"Clone: `#{clone_id}`"
    msg = (
        f"💰 **Manual payment — buyer confirmed on the web**\n"
        f"Buyer: <@{buyer_id}> (`{buyer_id}`)\n"
        f"Type: `{payment_type}` — {amount_display}\n"
        f"Reference: `{reference}`\n"
        f"{location_line}\n\n"
        f"Check Selar for a matching sale (buyer email `user_{buyer_id}@animebot.com`), "
        f"then Approve or Reject below."
    )
    view = ManualApprovalView(payment_id)
    for admin_id in approver_ids:
        try:
            admin_user = await bot.fetch_user(admin_id)
            await admin_user.send(msg, view=view)
        except discord.HTTPException:
            logger.warning(f"[manual-pay] couldn't DM approver {admin_id} for reference {reference}")


class _ManualPayApproveButton(discord.ui.DynamicItem[discord.ui.Button], template=rf"^manualpay_approve:{_APPROVAL_ID_RE}$"):
    """Approve half of the admin DM card. DynamicItem (not a plain View)
    because the DM is sent from api/selar_submit.py — a process with no
    live gateway connection — so nothing about this button can rely on
    in-memory state; everything it needs is re-derived from payment_id at
    click time via a fresh DB lookup."""

    def __init__(self, payment_id: int):
        self.payment_id = payment_id
        super().__init__(discord.ui.Button(
            label="✅ Approve", style=discord.ButtonStyle.success,
            custom_id=f"manualpay_approve:{payment_id}",
        ))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: "re.Match"):
        return cls(int(match.group(1)))

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        row = await db.get_payment_row_by_id(self.payment_id)
        if not row or row.get("status") != "awaiting_review":
            await interaction.followup.send("This payment's already been resolved or wasn't found.", ephemeral=True)
            return

        handler = UNLOCK_HANDLERS.get(row["payment_type"])
        if handler is None:
            await interaction.followup.send(
                f"No unlock handler wired for `{row['payment_type']}` yet — approve manually in code.", ephemeral=True
            )
            return

        await db.mark_payment_paid(row["paystack_reference"])
        await handler(row["paystack_reference"], row["user_id"], row.get("chat_id"), row.get("group_id"))

        for child in self.view.children:
            child.disabled = True
        await interaction.message.edit(
            content=f"{interaction.message.content}\n\n✅ **Approved** by {interaction.user.mention}",
            view=self.view,
        )
        await _notify_buyer(interaction.client, row["user_id"], row["payment_type"], approved=True)


class _ManualPayRejectButton(discord.ui.DynamicItem[discord.ui.Button], template=rf"^manualpay_reject:{_APPROVAL_ID_RE}$"):
    def __init__(self, payment_id: int):
        self.payment_id = payment_id
        super().__init__(discord.ui.Button(
            label="❌ Reject", style=discord.ButtonStyle.danger,
            custom_id=f"manualpay_reject:{payment_id}",
        ))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: "re.Match"):
        return cls(int(match.group(1)))

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        row = await db.get_payment_row_by_id(self.payment_id)
        if not row or row.get("status") != "awaiting_review":
            await interaction.followup.send("This payment's already been resolved or wasn't found.", ephemeral=True)
            return

        await db.mark_manual_payment_rejected(self.payment_id)
        for child in self.view.children:
            child.disabled = True
        await interaction.message.edit(
            content=f"{interaction.message.content}\n\n❌ **Rejected** by {interaction.user.mention}",
            view=self.view,
        )
        await _notify_buyer(interaction.client, row["user_id"], row["payment_type"], approved=False)


class ManualApprovalView(discord.ui.View):
    """timeout=None + DynamicItem children (see above) so this survives a
    bot restart instead of expiring in-memory — a real concern here since
    review can happen well after the DM was sent."""

    def __init__(self, payment_id: int):
        super().__init__(timeout=None)
        self.payment_id = payment_id
        self.add_item(_ManualPayApproveButton(payment_id))
        self.add_item(_ManualPayRejectButton(payment_id))


# Registered in discord_bot/bot.py via bot.add_dynamic_items(*MANUAL_PAYMENT_DYNAMIC_ITEMS)
# so Approve/Reject keep working after a restart, same mechanism as every
# other persistent button in this codebase.
MANUAL_PAYMENT_DYNAMIC_ITEMS = (_ManualPayApproveButton, _ManualPayRejectButton)


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
                                amount_display: str, guild_id: Optional[int] = None,
                                reference: Optional[str] = None) -> str:
    """Call after interaction.response.defer(ephemeral=True, thinking=True) —
    mirrors start_card_pack_payment's calling convention in views_card_pack.py.

    reference: pass a pre-generated reference when the caller needs to
    stash payment-type-specific data (e.g. clone_admin.py's
    store_discord_clone_pending_payment) under the exact same reference
    this function logs and Selar's redirect later reports back — that
    write has to happen before this call anyway (so the pending row
    exists the moment a webhook/redirect fires), which means it can't
    wait for this function to generate one internally. Defaults to
    generating one as before when the caller doesn't need to. Returns the
    reference either way, in case the caller wants to log/store it after
    the fact instead.

    guild_id: pass the guild this purchase is FOR when it's a whole-guild
    unlock (card pack, ultra pack) — None for account-level purchases
    (discord_clone). Matches how log_payment's chat_id is already used
    elsewhere, so has_paid()/get_latest_pending_payment() scoping stays
    consistent between the manual and automatic paths.

    Buyer identification no longer depends on Selar's redirect carrying
    any dynamic data — Selar's product-level "redirect after purchase" is
    a single static URL, same for every buyer, with nothing appended
    (confirmed). Instead: the Selar product's own Custom Checkout Form
    (Selar dashboard feature) collects the buyer's Discord username and
    server name as compulsory checkout questions, visible per-sale in the
    Selar dashboard for manual cross-checking — same idea as the
    synthetic buyer email, just via Selar's own form fields instead of a
    URL param a buyer could silently overwrite. The web /unlock page
    itself stays generic (no prefill, no per-buyer state) — its only job
    is a plain "I've Paid" button.
    """
    user = interaction.user
    reference = reference or _reference_for(payment_type, user.id)
    clone_id = getattr(interaction.client, "clone_id", None)
    link = _prefilled_selar_link(payment_type, user.id, guild_id, clone_id, reference)
    if not link:
        await interaction.followup.send(
            "Manual payments aren't set up for this yet — please try again later.", ephemeral=True
        )
        logger.error(f"[manual-pay] no SELAR_PRODUCT_LINKS entry for payment_type={payment_type}")
        return reference

    await db.log_payment(
        user.id, 0.0, reference, status="pending",
        payment_type=payment_type, chat_id=guild_id, provider=PROVIDER,
        clone_id=clone_id,
    )

    pay_view = discord.ui.View(timeout=None)
    pay_view.add_item(discord.ui.Button(label="💳 Pay on Selar", url=link, style=discord.ButtonStyle.link))
    await interaction.followup.send(
        f"Pay **{amount_display}** on Selar using the button below. "
        f"You'll be asked for your Discord username and server name at checkout — "
        f"enter them exactly as they appear so an admin can match your payment. "
        f"Once checkout completes, Selar will send you to a confirmation page — "
        f"tap **I've Paid** there and it'll be reviewed shortly.",
        view=pay_view, ephemeral=True,
    )
    return reference


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


async def _unlock_custom_role(reference: str, buyer_id: int, guild_id: Optional[int], clone_id: Optional[int]):
    """Per-user entitlement, not the role itself — see
    db.grant_custom_role_entitlement's docstring. guild_id is required here
    (start_manual_payment must be called with guild_id=interaction.guild.id
    for this payment_type) since the perk is scoped to one guild, same as
    the wizard that consumes it."""
    await db.grant_custom_role_entitlement(guild_id, buyer_id, clone_id=clone_id)


UNLOCK_HANDLERS = {
    "welcome_card_pack": _unlock_welcome_card_pack,
    "ultra_welcome_pack": _unlock_ultra_pack,
    "discord_clone": _unlock_discord_clone,
    "discord_clone_monetization": _unlock_discord_clone_monetization,
    "custom_role": _unlock_custom_role,
}
