# path: discord_bot/cogs/_views_gumroad_claim.py

"""Persistent "choose your server" flow for paid sales that arrived with no
server attached.

Why this exists: a guild-scoped Gumroad product (card pack, Customize Card,
custom role, Music Pro, Premium) needs to know WHICH server to unlock. If the
checkout was started somewhere interaction.guild_id was empty (e.g. a DM copy
of a wizard) the payment row has chat_id NULL. gumroad_payments.py used to
crash/park those sales. Now, once the sale is verified, it marks the payment
completed (money is in), stores a claim record under the global setting
`gumguild:<reference>` and DMs the buyer a persistent "Choose server" button
(custom_id `gumpick:<reference>`).

Clicking it (works after restarts and any time later) shows a dropdown built
LIVE from the servers this bot is in where the buyer has Manage Server (and
that don't already own the product). Picking one runs the normal
UNLOCK_HANDLERS entry for that product.

Safety:
- Only the buyer recorded on the claim can use it.
- Manage Server (or ownership) is re-checked in the chosen guild at pick time.
- The claim is single-use: it's taken with an atomic DELETE ... RETURNING, so
  a double click / two open menus can't unlock two servers. If the unlock
  then fails, the claim is put back so they can retry.
"""

import json
import logging
import re

import discord

from database import db, get_pool

logger = logging.getLogger(__name__)

CLAIM_KEY = "gumguild:{reference}"
_MAX_OPTIONS = 25  # Discord select-menu limit

_PRODUCT_LABELS = {
    "welcome_card_pack": "Welcome Card Pack",
    "ultra_welcome_pack": "Customize Card",
    "custom_role": "Custom Role",
    "music_pro": "Music Pro",
    "premium": "Premium",
}


def custom_id_for(reference: str) -> str:
    return f"gumpick:{reference}"


async def _peek_claim(reference: str):
    raw = await db.get_global_setting(CLAIM_KEY.format(reference=reference))
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


async def _take_claim(reference: str):
    """Atomically remove and return the claim (single use). None if gone."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        raw = await conn.fetchval(
            "DELETE FROM bot_global_settings WHERE key = $1 RETURNING value",
            CLAIM_KEY.format(reference=reference),
        )
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


async def _put_claim_back(reference: str, record: dict) -> None:
    try:
        await db.set_global_setting(CLAIM_KEY.format(reference=reference), json.dumps(record))
    except Exception:
        logger.exception(f"[gumroad-claim] couldn't restore claim for {reference} — restore by hand")


def _can_manage(guild: discord.Guild, user_id: int) -> bool:
    if guild.owner_id == user_id:
        return True
    member = guild.get_member(user_id)
    return bool(member and member.guild_permissions.manage_guild)


async def _already_owned(payment_type: str, guild_id: int, clone_id) -> bool:
    try:
        if payment_type in ("welcome_card_pack", "ultra_welcome_pack"):
            cfg = await db.get_welcome_config(guild_id, clone_id=clone_id)
            key = "card_pack_unlocked" if payment_type == "welcome_card_pack" else "ultra_pack_unlocked"
            return bool(cfg.get(key))
    except Exception:
        logger.exception("[gumroad-claim] ownership check failed; listing the server anyway")
    return False


NO_SERVER_HINT = (
    "I couldn't find a server for you. Make sure the bot is in the server and that you have "
    "**Manage Server** there (and that it doesn't already own this).\n"
    "💡 **Try again from inside the server you want to unlock it for** — run the command there "
    "and it will be tied to that server automatically."
)


def eligible_guilds(client: discord.Client, user_id: int, payment_type: str, clone_id) -> list:
    """Servers this bot is in where the user has Manage Server. (Ownership of the
    product is checked separately, async, by the caller via _already_owned.)"""
    return [g for g in client.guilds if _can_manage(g, user_id)]


async def _eligible_unowned(client: discord.Client, user_id: int, payment_type: str, clone_id) -> list:
    out = []
    for g in eligible_guilds(client, user_id, payment_type, clone_id):
        if await _already_owned(payment_type, g.id, clone_id):
            continue
        out.append(g)
    out.sort(key=lambda g: g.name.lower())
    return out


class _CheckoutServerSelect(discord.ui.Select):
    def __init__(self, options, resume, buyer_id: int):
        super().__init__(placeholder="Choose the server to unlock…", min_values=1, max_values=1, options=options)
        self._resume = resume
        self._buyer_id = buyer_id

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self._buyer_id:
            await interaction.response.send_message("This menu isn't for you.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild_id = int(self.values[0])
        guild = interaction.client.get_guild(guild_id)
        if guild is None or not _can_manage(guild, interaction.user.id):
            await interaction.followup.send("You need **Manage Server** in that server to unlock it there.", ephemeral=True)
            return
        await self._resume(interaction, guild_id)


class _CheckoutServerView(discord.ui.View):
    def __init__(self, options, resume, buyer_id: int):
        super().__init__(timeout=300)
        self.add_item(_CheckoutServerSelect(options, resume, buyer_id))


async def prompt_server_for_checkout(interaction: discord.Interaction, payment_type: str, resume) -> None:
    """A server-scoped checkout was started with no server (typically from a DM
    copy of a wizard). Instead of logging a payment that can't unlock anything,
    work out which server it's for FIRST:
      - exactly one eligible server -> use it (auto-apply), and say which
      - several -> a dropdown; picking one continues the checkout
      - none -> explain, with the 'try it in the server' hint
    `resume(interaction, guild_id)` is an async callable that continues the
    original checkout for that server using the given (fresh, deferred) interaction.
    Call after interaction.response.defer(...)."""
    clone_id = getattr(interaction.client, "clone_id", None)
    eligible = await _eligible_unowned(interaction.client, interaction.user.id, payment_type, clone_id)
    label = _PRODUCT_LABELS.get(payment_type, payment_type)
    if not eligible:
        await interaction.followup.send(NO_SERVER_HINT, ephemeral=True)
        return
    if len(eligible) == 1:
        g = eligible[0]
        await interaction.followup.send(f"Setting up **{label}** for **{g.name}**…", ephemeral=True)
        await resume(interaction, g.id)
        return
    shown = eligible[:_MAX_OPTIONS]
    options = [
        discord.SelectOption(label=g.name[:100], value=str(g.id), description=f"{g.member_count or 0} members")
        for g in shown
    ]
    note = "" if len(eligible) <= _MAX_OPTIONS else f"\n_Showing the first {_MAX_OPTIONS} of {len(eligible)} servers. Not listed? Run this from inside the server._"
    await interaction.followup.send(
        f"Which server should **{label}** unlock for?{note}",
        view=_CheckoutServerView(options, resume, interaction.user.id), ephemeral=True,
    )



class _ServerSelect(discord.ui.Select):
    def __init__(self, reference: str, options):
        super().__init__(placeholder="Choose the server to unlock…", min_values=1, max_values=1, options=options)
        self.reference = reference

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        guild_id = int(self.values[0])
        guild = interaction.client.get_guild(guild_id)
        record = await _peek_claim(self.reference)
        if not record:
            await interaction.edit_original_response(content="This purchase was already applied.", view=None)
            return
        if int(record["user_id"]) != interaction.user.id:
            await interaction.edit_original_response(content="This purchase belongs to someone else.", view=None)
            return
        if guild is None or not _can_manage(guild, interaction.user.id):
            await interaction.edit_original_response(
                content="You need **Manage Server** in that server to unlock it there.", view=None)
            return

        record = await _take_claim(self.reference)
        if not record:  # lost a race with a double click
            await interaction.edit_original_response(content="This purchase was already applied.", view=None)
            return

        payment_type = record["payment_type"]
        clone_id = record.get("clone_id")
        from payments_manual import UNLOCK_HANDLERS
        handler = UNLOCK_HANDLERS.get(payment_type)
        try:
            if handler is None:
                raise RuntimeError(f"no unlock handler for {payment_type}")
            await handler(self.reference, int(record["user_id"]), guild_id, clone_id)
            pool = await get_pool()
            async with pool.acquire() as conn:
                await conn.execute(
                    "UPDATE payment_logs SET chat_id = $2 WHERE paystack_reference = $1",
                    self.reference, guild_id,
                )
            if payment_type == "premium" and record.get("subscription_id"):
                await db.set_premium_subscription_id(guild_id, clone_id, record["subscription_id"])
        except Exception:
            logger.exception(f"[gumroad-claim] unlock failed for {self.reference} in guild {guild_id}")
            await _put_claim_back(self.reference, record)
            await interaction.edit_original_response(
                content="Something went wrong unlocking that server. Nothing was lost — "
                        "tap **Choose server** on the DM again to retry.", view=None)
            return

        logger.info(f"[gumroad-claim] {self.reference} unlocked for guild {guild_id} by user {interaction.user.id}")
        label = _PRODUCT_LABELS.get(payment_type, payment_type)
        await interaction.edit_original_response(
            content=f"✅ **{label}** is now unlocked for **{guild.name}**. Enjoy!", view=None)


class _ServerSelectView(discord.ui.View):
    def __init__(self, reference: str, options):
        super().__init__(timeout=300)
        self.add_item(_ServerSelect(reference, options))


class ChooseServerButton(discord.ui.DynamicItem[discord.ui.Button], template=r"gumpick:(?P<ref>[A-Za-z0-9_\-]+)"):
    def __init__(self, reference: str):
        super().__init__(discord.ui.Button(
            label="Choose server", emoji="🏠", style=discord.ButtonStyle.success,
            custom_id=custom_id_for(reference),
        ))
        self.reference = reference

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["ref"])

    async def callback(self, interaction: discord.Interaction):
        record = await _peek_claim(self.reference)
        if not record:
            await interaction.response.send_message(
                "This purchase has already been applied to a server. 🎉", ephemeral=True)
            return
        if int(record["user_id"]) != interaction.user.id:
            await interaction.response.send_message("This button isn't for you.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        payment_type = record["payment_type"]
        clone_id = record.get("clone_id")
        eligible = []
        for g in interaction.client.guilds:
            if not _can_manage(g, interaction.user.id):
                continue
            if await _already_owned(payment_type, g.id, clone_id):
                continue
            eligible.append(g)
        eligible.sort(key=lambda g: g.name.lower())

        if not eligible:
            await interaction.followup.send(
                f"{NO_SERVER_HINT}\n\nAlready paid? Your reference is `{self.reference}` — send it to the "
                f"bot owner and they can apply this purchase to your server for you.",
                ephemeral=True)
            return

        shown = eligible[:_MAX_OPTIONS]
        options = [
            discord.SelectOption(label=g.name[:100], value=str(g.id),
                                 description=f"{g.member_count or 0} members")
            for g in shown
        ]
        note = "" if len(eligible) <= _MAX_OPTIONS else f"\n_Showing the first {_MAX_OPTIONS} of {len(eligible)} servers._"
        label = _PRODUCT_LABELS.get(payment_type, payment_type)
        await interaction.followup.send(
            f"Pick the server to unlock **{label}** on:{note}",
            view=_ServerSelectView(self.reference, options), ephemeral=True)


DYNAMIC_ITEMS = (ChooseServerButton,)
