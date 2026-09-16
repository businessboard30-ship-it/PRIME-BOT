# path: discord_bot/cogs/_views_pending_payments.py

"""
Components-v2 view backing /pendingpayments (clone_admin.py) — a global,
paged queue of every payment_logs row currently `status = 'awaiting_review'`
across the whole bot (every guild, every clone), not scoped to the
invoking guild. Same paged-Container shape as
_views_leveling_leaderboard.py's build_leaderboard_view (PAGE_SIZE, a
count-then-fetch DB call, prev/next DynamicItem buttons) and the same
DynamicItem custom_id convention as _views_leveling_wizard.py
(colon-delimited fields, "-" for a None slot) — see _encode/_decode below.

Authorization is per-row, not per-command: this file doesn't gate who can
run /pendingpayments (any admin can try), it gates what they SEE and can
ACT ON. Every row is checked against payments_manual._resolve_approvers
(main-bot admins always; a clone's owner only for that clone's own
payments; discord_clone payment_type rows only ever visible to
DISCORD_CLONE_ADMIN_IDS) and an unauthorized row is hidden entirely
rather than shown read-only — this matches /approvepayment's existing
behavior (deny with a message) more closely than a read-only row would,
since a clone owner currently has no way to even look up another clone's
reference to begin with.

Because that authorization check needs a live bot/clone lookup per row
(_resolve_approvers), it can't be pushed into the DB query the way
get_xp_leaderboard's guild/clone_id scoping can — this pulls a bounded
batch of the raw awaiting_review queue (_RAW_FETCH_LIMIT), filters it in
Python to the invoker's authorized rows, then paginates THAT filtered
list at PAGE_SIZE. Fine for a manual-review queue, which is never going
to be thousands of rows deep at once; if that stops being true,
_resolve_approvers-style scoping belongs in SQL instead.

Approve reuses payments_manual's actual resolution/amount-confirmation
logic (resolve_manual_payment_approval, the same GHS-amount modal
pattern as _ManualPayApproveAmountModal) rather than duplicating it —
only the button/modal shells here are new, because the DM card's
_ManualPayApproveButton/_ManualPayRejectButton assume they're the only
two buttons in their view (their callbacks disable/edit `self.view`
wholesale), which would wrongly disable every OTHER payment's buttons on
this page too. Reject calls resolve_manual_payment_rejection directly,
no modal, same as the existing DM button.

After a button resolves a payment, the view re-renders in place
(_rerender) so a resolved row doesn't sit on screen with a stale, still-
clickable Approve/Reject pair — same "don't leave dead buttons" care as
the rest of this codebase (e.g. the leaderboard's nav-button re-render).
"""

import math
import re

import discord

from database import db
from payments_manual import (
    _resolve_approvers, resolve_manual_payment_approval, resolve_manual_payment_rejection,
)

PAGE_SIZE = 5  # denser rows than the leaderboard's 10 (buyer/location/type/reference
                # text + its own Approve/Reject ActionRow per entry costs more
                # components per row) — keeps a full page comfortably under
                # Discord's 40-component ceiling.
_RAW_FETCH_LIMIT = 200  # bound on how much of the raw awaiting_review queue we pull
                         # before per-row authorization filtering; see module docstring.


def _location_line(row: dict, bot: discord.Client) -> str:
    """Same "guild name if guild_id is set, else Clone #{clone_id}" shape
    as send_manual_payment_approval_dms's location_line."""
    guild_id = row.get("chat_id")
    if guild_id:
        guild = bot.get_guild(guild_id)
        return guild.name if guild else f"Guild `{guild_id}`"
    clone_id = row.get("clone_id")
    return f"Clone #{clone_id}" if clone_id else "Main bot"


async def _authorized_rows(bot: discord.Client, invoker_id: int) -> list[dict]:
    rows = await db.get_pending_manual_payments(limit=_RAW_FETCH_LIMIT, offset=0)
    authorized = []
    for row in rows:
        approvers = await _resolve_approvers(bot, row.get("chat_id"), payment_type=row.get("payment_type"))
        if invoker_id in approvers:
            authorized.append(row)
    return authorized


async def build_pending_payments_view(bot: discord.Client, invoker_id: int, page: int = 0) -> "discord.ui.LayoutView | None":
    """Shared builder for /pendingpayments and every button callback below
    — exactly one place assembles the Container so both stay in sync.
    Returns None when there's nothing this invoker is authorized to
    review right now (either the queue is empty, or every row in it
    belongs to a different clone/admin scope) — same "return None for
    nothing to show" contract as build_leaderboard_view, so the caller can
    send a plain message instead."""
    authorized = await _authorized_rows(bot, invoker_id)
    total = len(authorized)
    if not total:
        return None

    total_pages = max(1, math.ceil(total / PAGE_SIZE))
    page = max(0, min(page, total_pages - 1))
    offset = page * PAGE_SIZE
    page_rows = authorized[offset:offset + PAGE_SIZE]

    view = discord.ui.LayoutView(timeout=None)
    container = discord.ui.Container(accent_colour=discord.Color.gold())
    container.add_item(discord.ui.TextDisplay(
        f"### 💰 Pending Payments\n{total} payment{'s' if total != 1 else ''} awaiting your review."
    ))
    container.add_item(discord.ui.Separator())

    for row in page_rows:
        buyer_id = row["user_id"]
        location = _location_line(row, bot)
        text = (
            f"<@{buyer_id}> (`{buyer_id}`)\n"
            f"{location} · `{row['payment_type']}`\n"
            f"`{row['paystack_reference']}`"
        )
        container.add_item(discord.ui.TextDisplay(text))
        action_row = discord.ui.ActionRow()
        action_row.add_item(PendingPaymentsApproveButton(row["payment_id"], invoker_id, page))
        action_row.add_item(PendingPaymentsRejectButton(row["payment_id"], invoker_id, page))
        container.add_item(action_row)
        container.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.small))

    nav_row = discord.ui.ActionRow()
    nav_row.add_item(PendingPaymentsNavButton(invoker_id, page, "prev", disabled=(page <= 0)))
    nav_row.add_item(discord.ui.Button(
        label=f"{page + 1}/{total_pages}", style=discord.ButtonStyle.secondary,
        disabled=True, custom_id=f"pendpay_pageind:{invoker_id}:{page}",
    ))
    nav_row.add_item(PendingPaymentsNavButton(invoker_id, page, "next", disabled=(page >= total_pages - 1)))
    container.add_item(nav_row)

    view.add_item(container)
    return view


async def _rerender(interaction: discord.Interaction, invoker_id: int, page: int) -> None:
    # Ack first, before any slow work — build_pending_payments_view does a
    # DB fetch plus a _resolve_approvers round-trip per row, which can
    # blow past Discord's 3-second component-interaction ack window on a
    # long queue. Same reasoning as _views_leveling_leaderboard.py's
    # _rerender.
    if not interaction.response.is_done():
        await interaction.response.defer()
    view = await build_pending_payments_view(interaction.client, invoker_id, page)
    if view is None:
        await interaction.edit_original_response(content="No pending payments awaiting your review.", view=None)
        return
    await interaction.edit_original_response(content=None, view=view)


async def _deny_if_not_invoker(interaction: discord.Interaction, invoker_id: int) -> bool:
    """This is an ephemeral, per-invoker view (only the invoker ever sees
    it), but DynamicItems are reconstructed purely from the custom_id, so
    nothing stops a crafted/replayed interaction from a different user —
    guard every callback the same way the wizard views' _check_access
    does."""
    if interaction.user.id != invoker_id:
        await interaction.response.send_message(
            "This isn't your view — run `/pendingpayments` yourself.", ephemeral=True,
        )
        return True
    return False


class PendingPaymentsNavButton(discord.ui.DynamicItem[discord.ui.Button],
                                template=r"^pendpay_nav:(\d+):(\d+):(prev|next)$"):
    def __init__(self, invoker_id: int, page: int, action: str, disabled: bool = False):
        self.invoker_id = invoker_id
        self.page = page
        self.action = action
        labels = {"prev": "◀", "next": "▶"}
        super().__init__(discord.ui.Button(
            label=labels[action], style=discord.ButtonStyle.secondary, disabled=disabled,
            custom_id=f"pendpay_nav:{invoker_id}:{page}:{action}",
        ))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: "re.Match"):
        return cls(int(match.group(1)), int(match.group(2)), match.group(3))

    async def callback(self, interaction: discord.Interaction):
        if await _deny_if_not_invoker(interaction, self.invoker_id):
            return
        new_page = self.page - 1 if self.action == "prev" else self.page + 1
        await _rerender(interaction, self.invoker_id, new_page)


class _PendingPaymentsApproveAmountModal(discord.ui.Modal, title="Confirm amount paid"):
    """Same amount-confirmation prompt as payments_manual's
    _ManualPayApproveAmountModal, and calls the SAME
    resolve_manual_payment_approval underneath — this exists as its own
    class only because the two callers edit different things afterward:
    the DM card's modal disables/edits that one DM message in place,
    while this one re-renders the whole /pendingpayments page (since the
    resolved row needs to disappear from a shared, multi-row list, not
    just have its own two buttons disabled)."""

    amount = discord.ui.TextInput(
        label="Amount paid, in GHS (from Selar's dashboard)",
        placeholder="e.g. 35.00",
        required=True, max_length=12,
    )

    def __init__(self, payment_id: int, invoker_id: int, page: int):
        super().__init__()
        self.payment_id = payment_id
        self.invoker_id = invoker_id
        self.page = page

    async def on_submit(self, interaction: discord.Interaction):
        try:
            amount_value = float(str(self.amount).strip())
        except ValueError:
            await interaction.response.send_message(
                "That doesn't look like a number — tap Approve again and enter e.g. `35.00`.", ephemeral=True,
            )
            return
        await interaction.response.defer()

        result = await resolve_manual_payment_approval(interaction.client, self.payment_id, amount=amount_value)
        if not result.ok:
            await interaction.followup.send(result.message, ephemeral=True)
        await _rerender(interaction, self.invoker_id, self.page)


class PendingPaymentsApproveButton(discord.ui.DynamicItem[discord.ui.Button],
                                    template=r"^pendpay_approve:(\d+):(\d+):(\d+)$"):
    def __init__(self, payment_id: int, invoker_id: int, page: int):
        self.payment_id = payment_id
        self.invoker_id = invoker_id
        self.page = page
        super().__init__(discord.ui.Button(
            label="✅ Approve", style=discord.ButtonStyle.success,
            custom_id=f"pendpay_approve:{payment_id}:{invoker_id}:{page}",
        ))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: "re.Match"):
        return cls(int(match.group(1)), int(match.group(2)), int(match.group(3)))

    async def callback(self, interaction: discord.Interaction):
        if await _deny_if_not_invoker(interaction, self.invoker_id):
            return
        row = await db.get_payment_row_by_id(self.payment_id)
        if not row or row.get("status") != "awaiting_review":
            await interaction.response.send_message(
                "This payment's already been resolved — refreshing the list.", ephemeral=True,
            )
            return
        approvers = await _resolve_approvers(interaction.client, row.get("chat_id"), payment_type=row.get("payment_type"))
        if interaction.user.id not in approvers:
            await interaction.response.send_message("You're not an approver for this payment.", ephemeral=True)
            return
        # Modal must be the FIRST response to this interaction — same
        # constraint as _ManualPayApproveButton's callback.
        await interaction.response.send_modal(_PendingPaymentsApproveAmountModal(self.payment_id, self.invoker_id, self.page))


class PendingPaymentsRejectButton(discord.ui.DynamicItem[discord.ui.Button],
                                   template=r"^pendpay_reject:(\d+):(\d+):(\d+)$"):
    def __init__(self, payment_id: int, invoker_id: int, page: int):
        self.payment_id = payment_id
        self.invoker_id = invoker_id
        self.page = page
        super().__init__(discord.ui.Button(
            label="❌ Reject", style=discord.ButtonStyle.danger,
            custom_id=f"pendpay_reject:{payment_id}:{invoker_id}:{page}",
        ))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: "re.Match"):
        return cls(int(match.group(1)), int(match.group(2)), int(match.group(3)))

    async def callback(self, interaction: discord.Interaction):
        if await _deny_if_not_invoker(interaction, self.invoker_id):
            return
        row = await db.get_payment_row_by_id(self.payment_id)
        if not row or row.get("status") != "awaiting_review":
            await interaction.response.send_message(
                "This payment's already been resolved — refreshing the list.", ephemeral=True,
            )
            return
        approvers = await _resolve_approvers(interaction.client, row.get("chat_id"), payment_type=row.get("payment_type"))
        if interaction.user.id not in approvers:
            await interaction.response.send_message("You're not an approver for this payment.", ephemeral=True)
            return
        await interaction.response.defer()
        result = await resolve_manual_payment_rejection(interaction.client, self.payment_id)
        if not result.ok:
            await interaction.followup.send(result.message, ephemeral=True)
        await _rerender(interaction, self.invoker_id, self.page)


DYNAMIC_ITEMS = (PendingPaymentsNavButton, PendingPaymentsApproveButton, PendingPaymentsRejectButton)
