# path: discord_bot/cogs/_views_anime_plus.py

"""
Anime feed D5: the Upgrade button on every card and its private (ephemeral) card. Persistent DynamicItems: all state is in the
database (the person's latest payment row), nothing in memory, so Upgrade / Pay / Verify keep working on any old card after a restart.
A premium SERVER needs no upgrade (everything is already on); otherwise a person can subscribe to a personal monthly upgrade that never
makes the server premium. It is a real SUBSCRIPTION on the shared per-user plan system (modules/user_subs.py, product "anime_plus"):
Ghana pays through a recurring Paystack plan, everyone else through a Gumroad membership, and the existing webhooks create, renew,
cancel and end it. Nothing here grants access; only those webhooks do.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime
from typing import Dict, Optional

import discord

from modules import anime_plus as plus
from discord_bot.cogs._views_anime_feed import ACCENT, OOPS

logger = logging.getLogger(__name__)

NO_PINGS = discord.AllowedMentions.none()
UPGRADE_HINT = "Tap **Upgrade** on any anime card to unlock it."


def _clone_of(interaction: discord.Interaction) -> int:
    return getattr(interaction.client, "clone_id", None) or 0


def _gid(interaction: discord.Interaction) -> Optional[int]:
    return interaction.guild.id if interaction.guild else None


def _stamp(dt: Optional[datetime]) -> str:
    return f"<t:{int(dt.timestamp())}:D>" if dt else "soon"


def upgrade_text(state: str, expires: Optional[datetime], price_text: str, pending: bool, plan_state: str = "active") -> str:
    feats = "\n".join(f"• {f}" for f in plus.FEATURES)
    if state == "server":
        body = "✅ **This server is premium**, so everything below is already on for you. Nothing to buy."
        if expires:
            body += f"\n-# Your own personal upgrade (active until {_stamp(expires)}) also works in servers that are not premium."
    elif state == "personal":
        if plan_state == "cancelled":
            body = f"✅ **Your upgrade ends on {_stamp(expires)}** and will not renew. Subscribe again to keep it."
        elif plan_state == "past_due":
            body = f"⚠️ **Your last renewal payment failed.** Your upgrade runs until {_stamp(expires)} unless the payment goes through. Subscribe again to fix it."
        elif plan_state == "legacy":
            body = f"✅ **Your upgrade is active until {_stamp(expires)}** (a one-time payment). Subscribe to keep it going after that."
        else:
            body = f"✅ **You're subscribed.** Your upgrade renews on {_stamp(expires)}. Cancel any time and keep it to the end of the period."
    else:
        body = f"Unlock more for yourself, in any server: **{price_text} per month**. Renews monthly, cancel any time."
    parts = ["## ⭐ Anime upgrade", body, "", "**What you get**", feats, "",
             "-# It's personal: it never makes a server premium. Premium servers unlock all of this for everyone in them."]
    if pending:
        parts.append("-# You have an older checkout waiting. Paid already? Tap **I've paid** to check.")
    return "\n".join(parts)


def can_subscribe(state: str, plan_state: str) -> bool:
    """A premium server needs nothing, and a live subscription must never be paid for twice."""
    return state != "server" and not (state == "personal" and plan_state == "active")


def upgrade_view(state: str, expires: Optional[datetime], price_text: str, pending: bool, plan_state: str = "active") -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=None)
    box = discord.ui.Container(accent_colour=ACCENT)
    box.add_item(discord.ui.TextDisplay(upgrade_text(state, expires, price_text, pending, plan_state)[:3900]))
    buttons = []
    if can_subscribe(state, plan_state):
        buttons.append(_PayButton("Subscribe again" if state == "personal" else "Subscribe"))
    if pending:
        buttons.append(_VerifyButton())
    if buttons:
        box.add_item(discord.ui.ActionRow(*buttons))
    view.add_item(box)
    return view


def _price_text() -> str:
    import config
    return f"${config.ANIME_PLUS_FEE_USD:g} USD"


_watchers: Dict[int, "asyncio.Task"] = {}                              # one per person, strong references so a watcher is not garbage collected
WATCH_EVERY = 15
WATCH_FOR = 15 * 60


async def _watch(user: discord.abc.User, clone: int) -> None:
    """Waits (15 s steps, 15 minutes) for the subscription to go live, then DMs once. It only READS the entitlement; the webhook (or the
    Verify button on the payment card) is what creates it. If the bot restarts mid-wait nothing is lost, the upgrade is still granted."""
    try:
        waited = 0
        while waited < WATCH_FOR:
            await asyncio.sleep(WATCH_EVERY)
            waited += WATCH_EVERY
            if await plus.personal_state(user.id, clone, fresh=True) == "active":
                expires = await plus.personal_expiry(user.id, clone)
                try:
                    await user.send(f"✅ You're subscribed. Your anime upgrade is active and renews on {_stamp(expires)}.")
                except discord.HTTPException:
                    pass                                              # closed DMs: the upgrade is active regardless
                return
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.warning("[anime-plus] subscription watcher stopped", exc_info=True)


def _start_watch(user, clone: int) -> None:
    old = _watchers.pop(user.id, None)
    if old and not old.done():
        old.cancel()
    task = asyncio.create_task(_watch(user, clone))
    _watchers[user.id] = task
    task.add_done_callback(lambda t, uid=user.id: _watchers.pop(uid, None) if _watchers.get(uid) is t else None)


async def _pending(user_id: int, clone_id: int) -> bool:
    try:
        row = await plus.latest_payment(user_id, clone_id)
    except Exception:
        return False
    return bool(row and row["status"] == "pending")


async def _fail(interaction: discord.Interaction) -> None:
    try:
        if interaction.response.is_done():
            await interaction.followup.send(OOPS, ephemeral=True)
        else:
            await interaction.response.send_message(OOPS, ephemeral=True)
    except discord.HTTPException:
        pass


class _UpgradeButton(discord.ui.DynamicItem[discord.ui.Button], template=r"^anime_plus$"):
    def __init__(self):
        super().__init__(discord.ui.Button(label="Upgrade", style=discord.ButtonStyle.secondary, emoji="⭐", custom_id="anime_plus"))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        return cls()

    async def callback(self, interaction: discord.Interaction):
        try:
            await interaction.response.defer(ephemeral=True)
            clone = _clone_of(interaction)
            state, expires = await plus.status(interaction.user.id, _gid(interaction), clone)
            plan_state = await plus.personal_state(interaction.user.id, clone)
            view = upgrade_view(state, expires, _price_text(), await _pending(interaction.user.id, clone), plan_state)
            await interaction.followup.send(view=view, ephemeral=True, allowed_mentions=NO_PINGS)
        except Exception:
            logger.exception("[anime-plus] upgrade card failed")
            await _fail(interaction)


class _PayButton(discord.ui.DynamicItem[discord.ui.Button], template=r"^anime_plus_pay$"):
    """Starts the monthly subscription checkout through the shared recurring flow (payments_manual.start_geo_payment): Paystack plan for
    Ghana, Gumroad membership elsewhere. The price comes from config via modules/user_subs.py, never from the click."""

    def __init__(self, label: str = "Subscribe"):
        super().__init__(discord.ui.Button(label=label, style=discord.ButtonStyle.success, emoji="💳", custom_id="anime_plus_pay"))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        return cls()

    async def callback(self, interaction: discord.Interaction):
        try:
            await interaction.response.defer(ephemeral=True)
            clone = _clone_of(interaction)
            state, _ = await plus.status(interaction.user.id, _gid(interaction), clone)
            plan_state = await plus.personal_state(interaction.user.id, clone, fresh=True)
            if state == "server":                                           # never charge someone who already has it for free
                await interaction.followup.send("✅ This server is premium, so you already have everything. Nothing to buy.", ephemeral=True)
                return
            if not can_subscribe(state, plan_state):                        # never charge twice for a live subscription
                await interaction.followup.send("✅ You're already subscribed. Nothing more to pay.", ephemeral=True)
                return
            import config
            from modules import user_subs
            from payments_manual import start_geo_payment
            price_usd = user_subs.price_usd("anime_plus")
            _start_watch(interaction.user, clone)
            await start_geo_payment(
                interaction, payment_type="anime_plus", price_usd=price_usd,
                product_title=f"💳 {config.ANIME_PLUS_PRODUCT_NAME}",
                product_description=config.ANIME_PLUS_PRODUCT_DESCRIPTION,
                amount_display=f"${price_usd:g} USD / month")
        except Exception:
            logger.exception("[anime-plus] checkout failed")
            await _fail(interaction)


class _VerifyButton(discord.ui.DynamicItem[discord.ui.Button], template=r"^anime_plus_verify$"):
    """Checks the person's latest checkout. If the webhook already completed it, this just confirms."""

    def __init__(self):
        super().__init__(discord.ui.Button(label="I've paid", style=discord.ButtonStyle.primary, emoji="✅", custom_id="anime_plus_verify"))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        return cls()

    async def callback(self, interaction: discord.Interaction):
        try:
            await interaction.response.defer(ephemeral=True)
            clone = _clone_of(interaction)
            row = await plus.latest_payment(interaction.user.id, clone)
            if row and row["status"] == "pending":                           # an older one-off checkout still waiting: check it as before
                from payments import resolve_gateway_for_provider
                gateway, api_key = await resolve_gateway_for_provider(clone, row.get("provider"), platform="discord")
                result = await asyncio.to_thread(gateway.verify_payment, row["payment_reference"], api_key=api_key)
                if result.get("status") == "success":
                    await plus.complete_payment(row["payment_reference"])     # None means the webhook got there first: same result
            plus.forget(interaction.user.id, clone)
            state = await plus.personal_state(interaction.user.id, clone, fresh=True)
            expires = await plus.personal_expiry(interaction.user.id, clone)
            if state == "none":
                await interaction.followup.send("Not confirmed yet. Finish the payment, then tap **Verify** on the payment card (or this button) again.", ephemeral=True)
                return
            await interaction.followup.send(f"✅ Your anime upgrade is active until {_stamp(expires)}.", ephemeral=True, allowed_mentions=NO_PINGS)
        except Exception:
            logger.exception("[anime-plus] verify failed")
            await _fail(interaction)


PLUS_ITEMS = (_UpgradeButton, _PayButton, _VerifyButton)
