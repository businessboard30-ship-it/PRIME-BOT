# path: discord_bot/cogs/_views_payment_card.py

"""The order card shown with every pay button, plus its 'Check status' button.

Why: buyers were unsure what they were paying for, which server it would
unlock, and whether it worked. The card answers all three BEFORE they pay
(product, price, server, account, reference) and the button answers "did it
work?" AFTER, without a support message.

Check status is a persistent DynamicItem (custom_id
`paystatus:<user_id>:<payment_type>:<guild_id or 0>`), so it survives
restarts. It looks payments up by buyer + product + server rather than by
reference because the split (Paystack/Gumroad) checkout only creates its
references when the buyer clicks through. Only the buyer named in the
custom_id can use it.
"""

import logging
import re
from typing import List, Optional

import discord

from database import db

logger = logging.getLogger(__name__)

_STATUS_RE = re.compile(r"^paystatus:(?P<user_id>\d+):(?P<ptype>[a-z0-9_]+):(?P<guild_id>\d+)$")


def product_label(payment_type: str) -> str:
    from discord_bot.cogs._views_gumroad_claim import _PRODUCT_LABELS
    return _PRODUCT_LABELS.get(payment_type) or payment_type.replace("_", " ").title()


def _server_name(interaction: discord.Interaction, guild_id: int) -> str:
    """Never raises: the card is decoration, so a lookup problem must fall back
    to the server ID rather than block the checkout it sits on."""
    try:
        guild = interaction.guild if (interaction.guild and interaction.guild.id == guild_id) else None
        guild = guild or interaction.client.get_guild(guild_id)
        name = getattr(guild, "name", None)
        if isinstance(name, str) and name:
            return discord.utils.escape_markdown(name)[:60]
    except Exception:
        logger.exception("[pay-card] server name lookup failed")
    return f"server `{guild_id}`"


def _price_text(payment_type: str, price_display: str) -> str:
    if payment_type == "premium" and "month" not in price_display.lower():
        return f"{price_display} / month"
    return price_display


def order_summary(interaction: discord.Interaction, payment_type: str, price_display: str,
                  guild_id: Optional[int], reference: Optional[str] = None) -> str:
    """The checklist: product, price, server, account (and reference when known)."""
    user = interaction.user
    lines = [
        "🧾 **Your order**",
        f"**Product:** {product_label(payment_type)}",
        f"**Price:** {_price_text(payment_type, price_display)}",
    ]
    if guild_id:
        lines.append(f"**Server:** {_server_name(interaction, int(guild_id))}")
    else:
        lines.append("**Server:** not tied to a server (account purchase)")
    uname = getattr(user, "name", None)
    uname = discord.utils.escape_markdown(uname) if isinstance(uname, str) and uname else "you"
    lines.append(f"**Account:** {uname} (`{user.id}`)")
    if reference:
        lines.append(f"**Ref:** `{reference}`")
    return "\n".join(lines)


def wrong_server_note(guild_id: Optional[int]) -> str:
    if not guild_id:
        return ""
    return "Wrong server? Close this and run the command in the server you want."


def auto_unlock_note(guild_id: Optional[int]) -> str:
    """Reassurance line for checkouts that confirm by themselves (Gumroad)."""
    parts = ["⚡ Unlocks automatically within seconds of paying, and you'll get a DM."]
    wrong = wrong_server_note(guild_id)
    if wrong:
        parts.append(wrong)
    return "\n".join(parts)


def status_message(rows: List[dict], owned: bool, label: str, server: Optional[str]) -> str:
    """Pure: turn a buyer's payment rows (newest first) into the status reply."""
    where = f" on **{server}**" if server else ""
    if owned:
        return f"✅ **{label}** is unlocked{where}."
    completed = [r for r in rows if r.get("status") == "completed"]
    if completed:
        if completed[0].get("chat_id"):
            return f"✅ Payment received and applied{where}."
        return "✅ Payment received — check your DMs for the **Choose server** button to finish."
    if not rows:
        return "No payment found for this order yet. Tap the **Pay** button first."
    latest = rows[0]
    ref = latest.get("paystack_reference") or "?"
    if latest.get("status") == "rejected":
        return f"❌ This payment was rejected. Contact support with your reference: `{ref}`."
    return (
        "⏳ Not received yet. If you just paid, wait about 30 seconds and tap this again.\n"
        f"Still nothing? Contact support with your reference: `{ref}`."
    )


class CheckStatusButton(discord.ui.DynamicItem[discord.ui.Button], template=_STATUS_RE.pattern):
    def __init__(self, user_id: int, payment_type: str, guild_id: Optional[int]):
        self.user_id = int(user_id)
        self.payment_type = payment_type
        self.guild_id = int(guild_id) if guild_id else None
        super().__init__(discord.ui.Button(
            label="Check status", style=discord.ButtonStyle.secondary, emoji="🔎",
            custom_id=f"paystatus:{self.user_id}:{payment_type}:{self.guild_id or 0}",
        ))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button,
                             match: re.Match, /):
        gid = int(match["guild_id"])
        return cls(int(match["user_id"]), match["ptype"], gid or None)

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This button isn't for you.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            rows = await db.get_order_payments(self.user_id, self.payment_type, self.guild_id)
            owned = False
            server = None
            if self.guild_id:
                server = _server_name(interaction, self.guild_id)
                from discord_bot.cogs._views_gumroad_claim import _already_owned
                clone_id = getattr(interaction.client, "clone_id", None)
                owned = await _already_owned(self.payment_type, self.guild_id, clone_id)
            text = status_message(rows, owned, product_label(self.payment_type), server)
        except Exception:
            logger.exception("[pay-card] status lookup failed")
            text = "Couldn't check right now — please try again in a moment."
        await interaction.followup.send(text, ephemeral=True)


def add_check_status_button(view: discord.ui.View, user_id: int, payment_type: str,
                            guild_id: Optional[int]) -> None:
    view.add_item(CheckStatusButton(user_id, payment_type, guild_id))


DYNAMIC_ITEMS = (CheckStatusButton,)
