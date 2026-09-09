# path: discord_bot/cogs/_views_auto_listing_offer.py

"""
One-time "list this server for me?" offer — skips the website form
entirely. Sent once per guild, ever (see claim_auto_listing_offer_send):

  - Tries a DM to the owner first; if DMs are closed, falls back to
    posting the same offer in a channel the bot can actually talk in
    (system_channel first, else the first text channel with send perms).
  - "Agree" builds the listing straight from live Discord data
    (guild.name/icon/member_count, an auto-generated permanent invite via
    server_listing.py's _auto_generate_invite) and calls
    db.upsert_server_listing directly — no site visit needed. Tags are
    left empty; they can be added later from the listing's edit link.
  - If the guild has no Community-mode description set, Agree can't fill
    that field from Discord data at all, so it skips auto-listing and
    points the admin at the manual /setup servers form instead.
  - "Deny" is permanent — status is recorded and this offer is never
    resent. /setup servers (server_listing.py) is always still there for
    a manual listing later; the decline message says so.

DynamicItem-based (not a plain View) so the buttons keep working across
restarts — same convention as _views_registry_invite_consent.py, which
this module is deliberately modeled on.
"""

import logging

import discord

from config import DASHBOARD_BASE_URL
from database import db
from discord_bot.cogs.server_listing import _auto_generate_invite

logger = logging.getLogger(__name__)


def _clone_id_of_bot(bot) -> int | None:
    return getattr(bot, "clone_id", None)


async def offer_auto_listing(bot, guild: discord.Guild) -> None:
    """Called from bot.py's _handle_new_guild. Best-effort throughout —
    never raises, since a failed offer shouldn't block anything else in
    the join flow."""
    clone_id = _clone_id_of_bot(bot)
    try:
        claimed = await db.claim_auto_listing_offer_send(guild.id, clone_id)
    except Exception:
        logger.exception("[auto-listing-offer] claim failed for guild %s", guild.id)
        return
    if not claimed:
        return  # already asked this guild once, ever — never re-send

    content = (
        f"📋 Want me to list **{guild.name}** on the public server directory right now? "
        "No form to fill in — I'll pull the name, icon, member count, and a permanent invite "
        "straight from Discord and it goes live immediately. You can add tags or edit anything "
        "later.\n\nThis is a one-time ask — tap **Deny** and I won't bring it up again "
        "(you can still list manually anytime with `/setup servers`)."
    )
    view = discord.ui.View(timeout=None)
    view.add_item(DynamicAutoListingAgreeButton(guild.id))
    view.add_item(DynamicAutoListingDenyButton(guild.id))

    try:
        owner = guild.owner or (await guild.fetch_owner() if guild.owner_id else None)
    except (discord.HTTPException, discord.Forbidden):
        owner = None

    if owner is not None and not owner.bot:
        try:
            await owner.send(content=content, view=view)
            return
        except (discord.HTTPException, discord.Forbidden):
            pass  # DMs closed — fall through to the channel fallback below

    channel = _pick_fallback_channel(guild)
    if channel is None:
        return  # nowhere to post — offer silently never reaches anyone
    try:
        await channel.send(content=content, view=view)
    except (discord.HTTPException, discord.Forbidden):
        logger.info("[auto-listing-offer] channel fallback also failed for guild %s", guild.id)


def _pick_fallback_channel(guild: discord.Guild) -> discord.TextChannel | None:
    candidates = [c for c in (guild.system_channel,) if c is not None]
    candidates += [c for c in guild.text_channels if c not in candidates]
    for channel in candidates:
        perms = channel.permissions_for(guild.me)
        if perms.send_messages and perms.view_channel:
            return channel
    return None


async def _build_listing_from_guild(guild: discord.Guild) -> dict:
    invite_url = await _auto_generate_invite(guild)
    return await db.upsert_server_listing(
        guild_id=guild.id,
        clone_id=None,
        guild_name=guild.name,
        guild_icon_url=guild.icon.url if guild.icon else None,
        member_count=guild.member_count or 0,
        invite_url=invite_url or "",
        description=guild.description,
        tags=[],
    )


class DynamicAutoListingAgreeButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"autolisting:agree:(?P<guild_id>\d+)",
):
    def __init__(self, guild_id: int):
        self.guild_id = guild_id
        super().__init__(
            discord.ui.Button(label="Agree", style=discord.ButtonStyle.success, emoji="✅",
                               custom_id=f"autolisting:agree:{guild_id}")
        )

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Item, match: "re.Match[str]", /):
        return cls(int(match["guild_id"]))

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        guild = interaction.client.get_guild(self.guild_id)
        if guild is None:
            await interaction.edit_original_response(
                content="I'm not in that server anymore, so I can't list it.", view=None,
            )
            return

        clone_id = _clone_id_of_bot(interaction.client)

        if not guild.description:
            await db.set_auto_listing_offer_status(self.guild_id, "agreed", clone_id)
            await interaction.edit_original_response(
                content=(
                    "Almost — your server doesn't have a Community-mode description set, so I don't "
                    "have anything to fill the listing's description with automatically. Run "
                    f"`/setup servers` and I'll hand you a link to `{DASHBOARD_BASE_URL}` with the "
                    "invite already filled in — just add a description and tags."
                ),
                view=None,
            )
            return

        try:
            await _build_listing_from_guild(guild)
        except Exception:
            logger.exception("[auto-listing-offer] listing build failed for guild %s", self.guild_id)
            await interaction.edit_original_response(
                content="Something went wrong creating the listing — try `/setup servers` instead.",
                view=None,
            )
            return

        await db.set_auto_listing_offer_status(self.guild_id, "agreed", clone_id)
        await interaction.edit_original_response(
            content=(
                f"✅ **{guild.name}** is live on the directory now: "
                f"{DASHBOARD_BASE_URL}/servers/{guild.id}\n"
                "Run `/setup servers` anytime to add tags or edit it."
            ),
            view=None,
        )


class DynamicAutoListingDenyButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"autolisting:deny:(?P<guild_id>\d+)",
):
    def __init__(self, guild_id: int):
        self.guild_id = guild_id
        super().__init__(
            discord.ui.Button(label="Deny", style=discord.ButtonStyle.secondary,
                               custom_id=f"autolisting:deny:{guild_id}")
        )

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Item, match: "re.Match[str]", /):
        return cls(int(match["guild_id"]))

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        clone_id = _clone_id_of_bot(interaction.client)
        await db.set_auto_listing_offer_status(self.guild_id, "declined", clone_id)
        await interaction.edit_original_response(
            content="No problem — I won't ask again. Run `/setup servers` anytime if you change your mind.",
            view=None,
        )


DYNAMIC_ITEMS = (DynamicAutoListingAgreeButton, DynamicAutoListingDenyButton)
