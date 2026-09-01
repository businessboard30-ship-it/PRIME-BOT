# path: discord_bot/cogs/_views_direct_paid.py

"""
"I've Paid" button for buyers who paid via a bare Selar link (broadcast DM
or the raw link shared outside any slash command) instead of going through
/welcome buyultra — see payments_manual.py's BuyerConfirmView for that
normal flow, which already knows guild_id because it was captured when the
buyer ran the command.

Here there is no prior command, so there is no known guild_id to attach.
Per how this was scoped: instead of asking the buyer to type their server
ID, this button looks up every server the CLICKING user has Manage Server
permission in, across every guild this bot process is in, and uses that
as the evidence for which server to unlock:

  - exactly one match  -> proceed exactly like the normal claim flow:
    log a pending payment for that guild and DM the approvers.
  - zero matches        -> can't be resolved automatically; tell the buyer
    and flag it to the approvers with no guild attached so a human can
    chase it (DM them for their server, check Selar manually, etc.).
  - 2+ matches           -> per the decision made when this was scoped,
    don't guess: DM the approvers a plain list of every matching server
    and let a human pick, rather than silently unlocking the wrong one.

Built as a discord.ui.DynamicItem (not a plain View) for the same reason
as every other DM/broadcast button in this codebase (see
_views_join_dm.py's docstring) — the payment_type rides in the custom_id
itself, so one button definition covers every SELAR_PRODUCT_LINKS product
a broadcast might advertise, survives bot restarts, and never times out.
Only ever sent via api/cron_discord_owner_broadcast.py's raw REST DMs, so
it must be registered persistently in setup_hook rather than relying on
an in-memory view instance staying alive.
"""

import re
import logging

import discord

from database import db
from config import SELAR_PRODUCT_LINKS, WELCOME_CARD_PACK_FEE_USD, ULTRA_PACK_FEE_USD, CLONE_MONETIZATION_FEE_GHS

logger = logging.getLogger(__name__)

# custom_id shape: direct_pay:<payment_type>
_DIRECT_PAID_RE = re.compile(r"^direct_pay:([a-z_]+)$")

# Mirrors the exact amount_display strings views_card_pack.py's
# start_manual_payment() callers pass in (f"${FEE:g} USD" etc.) — reused
# here so the approver DM shows a real price instead of just the
# payment_type's title-cased name. discord_clone_monetization is GHS, not
# USD, unlike the other two — kept as its own case rather than assuming a
# single currency across every SELAR_PRODUCT_LINKS entry.
_AMOUNT_DISPLAY = {
    "welcome_card_pack": f"${WELCOME_CARD_PACK_FEE_USD:g} USD",
    "ultra_welcome_pack": f"${ULTRA_PACK_FEE_USD:g} USD",
    "discord_clone_monetization": f"₵{CLONE_MONETIZATION_FEE_GHS:g} GHS",
}


def direct_paid_custom_id(payment_type: str) -> str:
    """Used by the broadcast sender (api/cron_discord_owner_broadcast.py)
    to build the button it attaches — kept here so the encoding has one
    source of truth shared with from_custom_id below."""
    return f"direct_pay:{payment_type}"


async def _guilds_with_manage_permission(client: discord.Client, user_id: int) -> list[discord.Guild]:
    """Every guild this bot process is currently in where user_id has
    Manage Server (or is the owner). Uses the member cache first
    (guild.get_member) and only falls back to a fetch when the member
    isn't cached, since a bot in many guilds shouldn't fire one REST call
    per guild on every button click when it doesn't have to.

    CAVEAT: with the Members intent on (see bot.py), the cache is
    populated from each guild's member chunk after connect, so a fetch
    fallback should be rare in steady state — but for a bot process that
    is itself in a very large number of guilds, a cold cache (e.g. right
    after a restart, before chunking finishes) could still mean many
    sequential fetch_member calls on a single click. Each 404 (not a
    member) is cheap, but a large uncached guild count could still make
    this noticeably slow. Not a hard blocker to ship on typical guild
    counts, just worth knowing if this process ends up in the high
    thousands of guilds."""
    matches = []
    for guild in client.guilds:
        member = guild.get_member(user_id)
        if member is None:
            try:
                member = await guild.fetch_member(user_id)
            except discord.HTTPException:
                continue
        if member.id == guild.owner_id or member.guild_permissions.manage_guild:
            matches.append(guild)
    return matches


class _DirectPaidButton(discord.ui.DynamicItem[discord.ui.Button], template=_DIRECT_PAID_RE.pattern):
    def __init__(self, payment_type: str):
        self.payment_type = payment_type
        super().__init__(discord.ui.Button(
            label="✅ I've Paid", style=discord.ButtonStyle.success,
            custom_id=direct_paid_custom_id(payment_type),
        ))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: "re.Match"):
        return cls(match.group(1))

    async def callback(self, interaction: discord.Interaction):
        from payments_manual import _send_approval_dms, _resolve_approvers, _reference_for, UNLOCK_HANDLERS  # avoid import cycle

        await interaction.response.defer(ephemeral=True)

        if self.payment_type not in UNLOCK_HANDLERS:
            # Defensive only — shouldn't happen from a button we generate
            # ourselves, but a broadcast advertising a payment_type that's
            # since been removed from UNLOCK_HANDLERS shouldn't log a
            # payment nobody can ever approve.
            await interaction.followup.send(
                "This payment type isn't set up anymore — please contact support directly.", ephemeral=True,
            )
            logger.error(f"[direct-paid] unknown payment_type={self.payment_type} from custom_id click")
            return

        amount_display = _AMOUNT_DISPLAY.get(self.payment_type, self.payment_type.replace("_", " ").title())
        buyer_id = interaction.user.id
        clone_id = getattr(interaction.client, "clone_id", None)
        matches = await _guilds_with_manage_permission(interaction.client, buyer_id)

        if len(matches) == 1:
            guild = matches[0]
            reference = _reference_for(self.payment_type, buyer_id)
            await db.log_payment(
                buyer_id, 0.0, reference, status="pending",
                payment_type=self.payment_type, chat_id=guild.id, provider="selar",
            )
            await _send_approval_dms(
                interaction.client, reference, self.payment_type, buyer_id,
                guild.id, clone_id, amount_display,
            )
            await interaction.followup.send(
                f"Thanks — flagged for review against **{guild.name}** (found via your Manage Server "
                f"permission there). You'll get a DM as soon as it's confirmed.", ephemeral=True,
            )
            return

        if len(matches) == 0:
            await _notify_approvers_unresolved(interaction.client, buyer_id, self.payment_type, clone_id, reason="no_matching_guild")
            await interaction.followup.send(
                "Thanks — flagged for review, but I couldn't automatically tell which server this is for "
                "(I don't see you with Manage Server permission in any server I'm in). "
                "DM your server ID along with your payment confirmation and it'll get sorted.", ephemeral=True,
            )
            return

        # 2+ matches — don't guess, hand the list to a human.
        await _notify_approvers_unresolved(
            interaction.client, buyer_id, self.payment_type, clone_id,
            reason="multiple_matching_guilds", candidate_guilds=matches,
        )
        await interaction.followup.send(
            "Thanks — flagged for review. You have Manage Server permission in more than one server I'm in, "
            "so a human will confirm which one this is for before unlocking it.", ephemeral=True,
        )


async def _notify_approvers_unresolved(client: discord.Client, buyer_id: int, payment_type: str,
                                        clone_id, reason: str, candidate_guilds=None) -> None:
    """Same approver set _send_approval_dms uses, but for a claim that
    couldn't be auto-resolved to exactly one guild — no Approve/Reject
    view (there's nothing to unlock yet without a guild_id), just the
    info a human needs to resolve it manually."""
    from payments_manual import _resolve_approvers

    if reason == "no_matching_guild":
        detail = "I couldn't find any server I'm in where they currently have Manage Server permission."
    else:
        names = ", ".join(f"**{g.name}** (`{g.id}`)" for g in candidate_guilds)
        detail = f"They have Manage Server permission in {len(candidate_guilds)} servers I'm in: {names}."

    msg = (
        f"💰 **Manual payment — direct DM claim needs manual guild resolution**\n"
        f"Buyer: <@{buyer_id}> (`{buyer_id}`)\n"
        f"Type: `{payment_type}`\n\n"
        f"{detail}\n\n"
        f"Check Selar for a matching sale (buyer email `user_{buyer_id}@animebot.com`), then approve manually "
        f"once you know the right guild_id (no button for this yet — this case needs a human to pick)."
    )
    approver_ids = await _resolve_approvers(client, guild_id=None)
    for admin_id in approver_ids:
        try:
            admin_user = await client.fetch_user(admin_id)
            await admin_user.send(msg)
        except discord.HTTPException:
            logger.warning(f"[direct-paid] couldn't DM approver {admin_id} about unresolved buyer {buyer_id}")


DYNAMIC_ITEMS = (_DirectPaidButton,)
