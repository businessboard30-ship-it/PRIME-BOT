# path: discord_bot/cogs/_views_combined_join_offer.py

"""
Combines two separate join-time owner DMs into one message instead of
sending them back-to-back:

  1. The auto-listing offer (_views_auto_listing_offer.py) — offer to
     list the server on the public directory.
  2. The registry-invite consent ask (_views_registry_invite_consent.py)
     — offer to create an invite link for the bot owner's private
     /admin guilds registry, only relevant when _best_effort_invite
     found nothing usable.

Both used to fire as their own independent owner DM from bot.py's
_handle_new_guild, landing back-to-back in the owner's inbox (two
"shots" for what's really one join event). This module sends ONE DM
with whichever of the two questions actually applies — one, the other,
or both stacked in the same message with up to four buttons. If only
one applies, only that question is shown; the other's buttons are
never fabricated.

The button handlers reuse the same business logic as the originals
(listing build/upsert, invite creation) — the only real difference is
that answering one half must leave the OTHER half's buttons live on
the same message instead of wiping the whole view (the old per-offer
buttons always did `view=None` on click, which is correct when each
offer owns its own message but would eat the sibling question here).
Which half is still open is read straight off the message's current
button custom_ids rather than tracked separately, so it stays correct
no matter which half gets answered first.
"""

import logging

import discord

from database import db
from discord_bot.cogs._views_auto_listing_offer import (
    MAX_DESCRIPTION_LEN,
    MAX_LONG_DESCRIPTION_LEN,
    MAX_TAGS,
    _build_listing_from_guild,
    _clone_id_of_bot,
    _parse_tags,
)
from discord_bot.cogs._views_registry_invite_consent import _create_invite_for_registry
from config import DASHBOARD_BASE_URL

logger = logging.getLogger(__name__)

LISTING_PREFIX = "combinedjoin:listing:"
INVITE_PREFIX = "combinedjoin:invite:"


def _listing_text(guild: discord.Guild) -> str:
    return (
        f"📋 Want me to list **{guild.name}** on the public server directory right now? "
        "No form to fill in — I'll pull the name, icon, member count, and a permanent invite "
        "straight from Discord and it goes live immediately. You can add tags or edit anything "
        "later.\n\nThis is a one-time ask — tap **Deny** and I won't bring it up again "
        "(you can still list manually anytime with `/setup servers`)."
    )


def _invite_text(guild: discord.Guild) -> str:
    return (
        f"One more thing about **{guild.name}** — I keep a private admin registry of servers I'm in "
        "(only visible to my own bot owner, never shared publicly), and it's more useful with an invite "
        "link on file. Your server doesn't have a vanity URL or a readable existing invite, so I'd need "
        "to create a fresh one. Only do this if you're OK with that — otherwise just decline, everything "
        "else about the bot works exactly the same either way."
    )


def _build_content(guild: discord.Guild, *, show_listing: bool, show_invite: bool,
                    listing_note: str | None = None, invite_note: str | None = None) -> str:
    parts = []
    if show_listing:
        text = _listing_text(guild)
        if listing_note:
            text += f"\n> {listing_note}"
        parts.append(text)
    if show_invite:
        text = _invite_text(guild)
        if invite_note:
            text += f"\n> {invite_note}"
        parts.append(text)
    return "\n\n".join(parts)


def _build_view(guild_id: int, *, show_listing: bool, show_invite: bool) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    if show_listing:
        view.add_item(DynamicCombinedListingAgreeButton(guild_id))
        view.add_item(DynamicCombinedListingDenyButton(guild_id))
    if show_invite:
        view.add_item(DynamicCombinedInviteAllowButton(guild_id))
        view.add_item(DynamicCombinedInviteDeclineButton(guild_id))
    return view


def _pick_fallback_channel(guild: discord.Guild):
    candidates = [c for c in (guild.system_channel,) if c is not None]
    candidates += [c for c in guild.text_channels if c not in candidates]
    for channel in candidates:
        perms = channel.permissions_for(guild.me)
        if perms.send_messages and perms.view_channel:
            return channel
    return None


def _open_groups(interaction: discord.Interaction) -> set[str]:
    """Which question(s) still have live buttons on the message this
    interaction fired from — read off the actual components instead of
    tracked state, so it's correct regardless of answer order."""
    groups: set[str] = set()
    for row in interaction.message.components:
        for child in getattr(row, "children", []):
            cid = getattr(child, "custom_id", None) or ""
            if cid.startswith(LISTING_PREFIX):
                groups.add("listing")
            elif cid.startswith(INVITE_PREFIX):
                groups.add("invite")
    return groups


async def offer_combined_join_dm(bot, guild: discord.Guild, *, needs_invite_consent: bool) -> None:
    """Called from bot.py's _handle_new_guild. Best-effort throughout —
    never raises, since a failed offer shouldn't block anything else in
    the join flow."""
    clone_id = _clone_id_of_bot(bot)
    try:
        show_listing = await db.claim_auto_listing_offer_send(guild.id, clone_id)
    except Exception:
        logger.exception("[combined-join-offer] listing claim failed for guild %s", guild.id)
        show_listing = False

    show_invite = bool(needs_invite_consent)
    if not show_listing and not show_invite:
        logger.info("[combined-join-offer] nothing to offer guild=%s (listing already asked, invite not needed)", guild.id)
        return

    content = _build_content(guild, show_listing=show_listing, show_invite=show_invite)
    view = _build_view(guild.id, show_listing=show_listing, show_invite=show_invite)
    logger.info(
        "[combined-join-offer] sending guild=%s listing=%s invite=%s",
        guild.id, show_listing, show_invite,
    )

    try:
        owner = guild.owner or (await guild.fetch_owner() if guild.owner_id else None)
    except (discord.HTTPException, discord.Forbidden):
        owner = None
    if owner is None or owner.bot:
        logger.info("[combined-join-offer] no usable owner to DM guild=%s", guild.id)
        return

    try:
        await owner.send(content=content, view=view)
        logger.info("[combined-join-offer] sent owner DM guild=%s owner=%s", guild.id, owner.id)
        return
    except (discord.HTTPException, discord.Forbidden):
        logger.info("[combined-join-offer] owner DM failed (DMs closed?) guild=%s owner=%s", guild.id, owner.id)
        pass  # DMs closed — fall through to the channel fallback below

    if not show_listing:
        # Invite consent is an owner-only ask (same as the original
        # registry-invite-consent module) — no channel fallback for it.
        logger.info("[combined-join-offer] invite-only offer dropped, no channel fallback guild=%s", guild.id)
        return
    channel = _pick_fallback_channel(guild)
    if channel is None:
        logger.warning("[combined-join-offer] no fallback channel found guild=%s", guild.id)
        return
    try:
        await channel.send(content=content, view=view)
        logger.info("[combined-join-offer] sent channel fallback guild=%s channel=%s", guild.id, channel.id)
    except (discord.HTTPException, discord.Forbidden):
        logger.info("[combined-join-offer] channel fallback also failed for guild %s", guild.id)


async def _finish_listing(interaction: discord.Interaction, guild_id: int, note: str):
    """Answer the listing half, keeping the invite half's buttons (if
    they were on the message and aren't answered yet)."""
    still_open = _open_groups(interaction)
    still_open.discard("listing")
    invite_open = "invite" in still_open
    guild = interaction.client.get_guild(guild_id)
    content = _build_content(
        guild, show_listing=True, show_invite=invite_open, listing_note=note,
    )
    view = _build_view(guild_id, show_listing=False, show_invite=invite_open) if invite_open else None
    logger.info(
        "[combined-join-offer] listing answered guild=%s invite_still_open=%s note=%s",
        guild_id, invite_open, note,
    )
    await interaction.edit_original_response(content=content, view=view)


async def _finish_invite(interaction: discord.Interaction, guild_id: int, note: str):
    """Answer the invite half, keeping the listing half's buttons (if
    they were on the message and aren't answered yet)."""
    still_open = _open_groups(interaction)
    still_open.discard("invite")
    listing_open = "listing" in still_open
    guild = interaction.client.get_guild(guild_id)
    content = _build_content(
        guild, show_listing=listing_open, show_invite=True, invite_note=note,
    )
    view = _build_view(guild_id, show_listing=listing_open, show_invite=False) if listing_open else None
    logger.info(
        "[combined-join-offer] invite answered guild=%s listing_still_open=%s note=%s",
        guild_id, listing_open, note,
    )
    await interaction.edit_original_response(content=content, view=view)


class CombinedAutoListingDescriptionModal(discord.ui.Modal, title="List this server"):
    """Same fields/behavior as AutoListingDescriptionModal, but finishes
    through _finish_listing so an unanswered invite-consent half (if any)
    stays on the message instead of getting wiped by this modal's submit."""

    short_description = discord.ui.TextInput(
        label=f"Short description (max {MAX_DESCRIPTION_LEN} chars)",
        placeholder="Shows on the directory card",
        max_length=MAX_DESCRIPTION_LEN,
        required=True,
    )
    long_description = discord.ui.TextInput(
        label="Long description (optional)",
        style=discord.TextStyle.paragraph,
        placeholder="Shown on the full listing page — write as much as you like",
        max_length=MAX_LONG_DESCRIPTION_LEN,
        required=False,
    )
    tags = discord.ui.TextInput(
        label=f"Tags, comma-separated (up to {MAX_TAGS})",
        placeholder="gaming, anime, chill",
        required=False,
    )

    def __init__(self, guild_id: int):
        super().__init__()
        self.guild_id = guild_id

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        guild = interaction.client.get_guild(self.guild_id)
        logger.info("[combined-join-offer] listing modal submitted guild=%s user=%s", self.guild_id, interaction.user.id)
        if guild is None:
            await _finish_listing(interaction, self.guild_id, "I'm not in that server anymore, so I couldn't list it.")
            return

        clone_id = _clone_id_of_bot(interaction.client)
        try:
            from discord_bot.cogs.server_listing import _auto_generate_invite
            invite_url = await _auto_generate_invite(guild)
            row = await db.upsert_server_listing(
                guild_id=guild.id,
                clone_id=None,
                guild_name=guild.name,
                guild_icon_url=guild.icon.url if guild.icon else None,
                member_count=guild.member_count or 0,
                invite_url=invite_url or "",
                description=str(self.short_description),
                tags=_parse_tags(str(self.tags)),
                long_description=str(self.long_description) or None,
            )
        except Exception:
            logger.exception("[combined-join-offer] modal listing build failed for guild %s", self.guild_id)
            await _finish_listing(interaction, self.guild_id, "Something went wrong creating the listing — try `/setup servers` instead.")
            return

        if (
            row.get("description") != str(self.short_description)
            or row.get("long_description") != (str(self.long_description) or None)
            or list(row.get("tags") or []) != _parse_tags(str(self.tags))
        ):
            logger.error("[combined-join-offer] upsert row mismatch for guild %s: got %r", self.guild_id, row)
            await _finish_listing(
                interaction, self.guild_id,
                f"The listing saved but something looks off — please check {DASHBOARD_BASE_URL}/servers/{guild.id}.",
            )
            return

        await db.set_auto_listing_offer_status(self.guild_id, "agreed", clone_id)
        logger.info("[combined-join-offer] listing modal agreed and built guild=%s", self.guild_id)
        await _finish_listing(
            interaction, self.guild_id,
            f"✅ **{guild.name}** is live on the directory: {DASHBOARD_BASE_URL}/servers/{guild.id}",
        )


class DynamicCombinedListingAgreeButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=LISTING_PREFIX + r"agree:(?P<guild_id>\d+)",
):
    def __init__(self, guild_id: int):
        self.guild_id = guild_id
        super().__init__(
            discord.ui.Button(label="Agree", style=discord.ButtonStyle.success, emoji="✅",
                               custom_id=f"{LISTING_PREFIX}agree:{guild_id}")
        )

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Item, match: "re.Match[str]", /):
        return cls(int(match["guild_id"]))

    async def callback(self, interaction: discord.Interaction):
        # Description check MUST happen before any response.defer()/send() —
        # discord.py requires send_modal() to be the interaction's first
        # response.
        guild = interaction.client.get_guild(self.guild_id)
        logger.info("[combined-join-offer] listing agree clicked guild=%s user=%s", self.guild_id, interaction.user.id)
        if guild is None:
            logger.info("[combined-join-offer] listing agree: bot no longer in guild=%s", self.guild_id)
            await interaction.response.send_message(
                "I'm not in that server anymore, so I can't list it.", ephemeral=True,
            )
            return

        if not guild.description:
            logger.info("[combined-join-offer] listing agree: no description, opening modal guild=%s", self.guild_id)
            await interaction.response.send_modal(CombinedAutoListingDescriptionModal(self.guild_id))
            return

        await interaction.response.defer()
        clone_id = _clone_id_of_bot(interaction.client)
        try:
            await _build_listing_from_guild(guild)
        except Exception:
            logger.exception("[combined-join-offer] listing build failed for guild %s", self.guild_id)
            await _finish_listing(interaction, self.guild_id, "Something went wrong creating the listing — try `/setup servers` instead.")
            return

        await db.set_auto_listing_offer_status(self.guild_id, "agreed", clone_id)
        logger.info("[combined-join-offer] listing agreed and built guild=%s", self.guild_id)
        await _finish_listing(
            interaction, self.guild_id,
            f"✅ **{guild.name}** is live on the directory: {DASHBOARD_BASE_URL}/servers/{guild.id}",
        )


class DynamicCombinedListingDenyButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=LISTING_PREFIX + r"deny:(?P<guild_id>\d+)",
):
    def __init__(self, guild_id: int):
        self.guild_id = guild_id
        super().__init__(
            discord.ui.Button(label="Deny", style=discord.ButtonStyle.secondary,
                               custom_id=f"{LISTING_PREFIX}deny:{guild_id}")
        )

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Item, match: "re.Match[str]", /):
        return cls(int(match["guild_id"]))

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        clone_id = _clone_id_of_bot(interaction.client)
        await db.set_auto_listing_offer_status(self.guild_id, "declined", clone_id)
        logger.info("[combined-join-offer] listing declined guild=%s user=%s", self.guild_id, interaction.user.id)
        await _finish_listing(interaction, self.guild_id, "No problem — I won't ask again. `/setup servers` works anytime.")


class DynamicCombinedInviteAllowButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=INVITE_PREFIX + r"allow:(?P<guild_id>\d+)",
):
    def __init__(self, guild_id: int):
        self.guild_id = guild_id
        super().__init__(
            discord.ui.Button(label="Allow", style=discord.ButtonStyle.success, emoji="✅",
                               custom_id=f"{INVITE_PREFIX}allow:{guild_id}")
        )

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Item, match: "re.Match[str]", /):
        return cls(int(match["guild_id"]))

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        logger.info("[combined-join-offer] invite allow clicked guild=%s user=%s", self.guild_id, interaction.user.id)
        try:
            invite_url = await _create_invite_for_registry(interaction.client, self.guild_id)
        except Exception:
            logger.exception("[combined-join-offer] invite creation failed for guild %s", self.guild_id)
            await _finish_invite(interaction, self.guild_id, "Something went wrong creating the invite — check the bot logs.")
            return

        if not invite_url:
            logger.info("[combined-join-offer] invite allow: no permission to create invite guild=%s", self.guild_id)
            await _finish_invite(
                interaction, self.guild_id,
                "Thanks for saying yes — but I don't have permission to create an invite in any channel there, so I couldn't make one.",
            )
            return

        clone_id = getattr(interaction.client, "clone_id", None)
        await db.set_guild_invite_url(self.guild_id, invite_url, clone_id=clone_id)
        logger.info("[combined-join-offer] invite created and saved guild=%s", self.guild_id)
        await _finish_invite(interaction, self.guild_id, "✅ Thanks — invite link saved to the registry.")


class DynamicCombinedInviteDeclineButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=INVITE_PREFIX + r"decline:(?P<guild_id>\d+)",
):
    def __init__(self, guild_id: int):
        self.guild_id = guild_id
        super().__init__(
            discord.ui.Button(label="No thanks", style=discord.ButtonStyle.secondary,
                               custom_id=f"{INVITE_PREFIX}decline:{guild_id}")
        )

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Item, match: "re.Match[str]", /):
        return cls(int(match["guild_id"]))

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        logger.info("[combined-join-offer] invite declined guild=%s user=%s", self.guild_id, interaction.user.id)
        await _finish_invite(interaction, self.guild_id, "No problem — nothing was created, everything else works the same.")


DYNAMIC_ITEMS = (
    DynamicCombinedListingAgreeButton,
    DynamicCombinedListingDenyButton,
    DynamicCombinedInviteAllowButton,
    DynamicCombinedInviteDeclineButton,
)
