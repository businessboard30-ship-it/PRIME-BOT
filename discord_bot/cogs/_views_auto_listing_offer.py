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
    db.upsert_server_listing directly — no site visit needed.
  - If the guild has no Community-mode description set, Agree can't fill
    that field from Discord data alone, so instead of bailing to the
    website form it opens AutoListingDescriptionModal right there in
    Discord — short description, long description, and tags — and
    upserts the listing from those answers plus the same live Discord
    data (icon/member count/invite) used in the has-description path.
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

# Kept in sync with app/servers/submit/page.tsx's MAX_DESCRIPTION_LEN /
# MAX_LONG_DESCRIPTION_LEN / MAX_TAGS — this modal is a second entry point
# into the same server_listings row, so the caps must match or a listing
# built here could look fine in Discord and still get silently truncated
# (or rejected) by whatever the site enforces on its own form.
MAX_DESCRIPTION_LEN = 20
MAX_LONG_DESCRIPTION_LEN = 1500
MAX_TAGS = 5


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


def _parse_tags(raw: str) -> list[str]:
    """Comma-separated -> deduped, capped list, same shape the site's own
    tag chips end up storing. Case-preserved (the site doesn't lowercase
    tags either), blanks dropped, capped at MAX_TAGS so a modal submit
    can't stash more tags than the website form would ever allow."""
    seen: list[str] = []
    for raw_tag in raw.split(","):
        tag = raw_tag.strip()
        if not tag or tag in seen:
            continue
        seen.append(tag)
        if len(seen) >= MAX_TAGS:
            break
    return seen


class AutoListingDescriptionModal(discord.ui.Modal, title="List this server"):
    """Shown from DynamicAutoListingAgreeButton when the guild has no
    Community-mode description to auto-fill from — collects exactly the
    three fields Discord data can't supply (short description, long
    description, tags) and upserts the listing from those plus the same
    live guild data (icon/member_count/invite) the has-description path
    already uses, so both paths land in the identical shape of row."""

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
        if guild is None:
            await interaction.edit_original_response(
                content="I'm not in that server anymore, so I can't list it.", view=None,
            )
            return

        clone_id = _clone_id_of_bot(interaction.client)
        try:
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
            logger.exception("[auto-listing-offer] modal listing build failed for guild %s", self.guild_id)
            await interaction.edit_original_response(
                content="Something went wrong creating the listing — try `/setup servers` instead.",
                view=None,
            )
            return

        # Verify the insert actually landed the fields this modal collected
        # rather than trusting the upsert call didn't raise — a silent
        # column/type mismatch would otherwise show a false "success" here.
        if (
            row.get("description") != str(self.short_description)
            or row.get("long_description") != (str(self.long_description) or None)
            or list(row.get("tags") or []) != _parse_tags(str(self.tags))
        ):
            logger.error(
                "[auto-listing-offer] upsert row mismatch for guild %s: got %r", self.guild_id, row,
            )
            await interaction.edit_original_response(
                content="The listing saved but something looks off — please check "
                        f"{DASHBOARD_BASE_URL}/servers/{guild.id} and fix it manually if needed.",
                view=None,
            )
            return

        await db.set_auto_listing_offer_status(self.guild_id, "agreed", clone_id)
        await interaction.edit_original_response(
            content=(
                f"✅ **{guild.name}** is live on the directory now: "
                f"{DASHBOARD_BASE_URL}/servers/{guild.id}\n"
                "Run `/setup servers` anytime to edit it."
            ),
            view=None,
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
        # Description check MUST happen before any response.defer()/send() —
        # discord.py requires send_modal() to be the interaction's first
        # response, and deferring first (as this used to do unconditionally)
        # makes the modal impossible to open afterward.
        guild = interaction.client.get_guild(self.guild_id)
        if guild is None:
            await interaction.response.send_message(
                "I'm not in that server anymore, so I can't list it.", ephemeral=True,
            )
            return

        if not guild.description:
            await interaction.response.send_modal(AutoListingDescriptionModal(self.guild_id))
            return

        await interaction.response.defer()
        clone_id = _clone_id_of_bot(interaction.client)

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
