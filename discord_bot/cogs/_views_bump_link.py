# path: discord_bot/cogs/_views_bump_link.py

"""
Bump listings with no server invite link.

A server listing with no invite_url goes out as an ad card with no Join
button — other servers see it and can't actually join. This module:

  * explains WHY there's no link (missing Manage Server / Create Invite, or
    simply no invite to reuse) via explain_missing_link();
  * builds the flag (embed + buttons) shown to the server's admins —
    build_flag(); posted in the bump channel by post_flag_in_channel() (boot
    sweep, covers listings that were bumped before this existed) or shown
    ephemerally by BumpCog._do_bump when someone tries to bump;
  * lets an admin fix it in one press: "Get / create my link" (reuse a vanity
    URL / permanent invite, else create a never-expiring one — the press IS
    the consent, the bot still never mints invites on its own, see
    bot.py's _best_effort_invite) or "Paste my own link" (verified to be a live
    invite to THIS server before it's saved).

DynamicItems (custom_id carries the listing id) so the buttons keep working
on old messages after a restart. No schema change: "already flagged" is
detected by looking for the bot's own flag message in the bump channel.
"""

import logging
import re

import discord

from database import db, get_pool

logger = logging.getLogger(__name__)

_INVITE_RE = re.compile(
    r"^(?:https?://)?(?:www\.)?(?:discord\.gg/|discord(?:app)?\.com/invite/)([A-Za-z0-9-]+)/?$",
    re.IGNORECASE,
)
_MAX_CHANNELS_TO_TRY = 30


def has_link(listing: dict) -> bool:
    return bool((listing.get("invite_url") or "").strip())


# ── DB helpers (kept here so database.py doesn't need to change) ─────────────

async def set_listing_invite(listing_id: int, invite_url: str) -> bool:
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute("UPDATE bump_listings SET invite_url = $2 WHERE id = $1", listing_id, invite_url)
    return result.endswith("1")


async def list_server_listings_missing_link(clone_id) -> list:
    """Every server listing on this clone with no link whose guild has a bump
    channel set — regardless of whether it was ever bumped."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT l.id AS listing_id, l.guild_id, l.name, c.bump_channel_id, c.configured_by
            FROM bump_listings l
            JOIN bump_guild_config c
              ON c.guild_id = l.guild_id AND COALESCE(c.clone_id, -1) = COALESCE(l.clone_id, -1)
            WHERE l.listing_type = 'server'
              AND l.status = 'approved'
              AND COALESCE(l.clone_id, -1) = COALESCE($1, -1)
              AND (l.invite_url IS NULL OR btrim(l.invite_url) = '')
              AND c.bump_channel_id IS NOT NULL
            ORDER BY l.id
            """,
            clone_id,
        )
    return [dict(r) for r in rows]


# ── finding / creating a link ────────────────────────────────────────────────

def _channels_to_try(guild: discord.Guild, prefer_channel_id=None) -> list:
    channels = []
    if prefer_channel_id:
        preferred = guild.get_channel(int(prefer_channel_id))
        if isinstance(preferred, discord.TextChannel):
            channels.append(preferred)
    channels += [c for c in guild.text_channels if c not in channels]
    return channels[:_MAX_CHANNELS_TO_TRY]


async def find_existing_invite(guild: discord.Guild):
    """Vanity URL or a permanent invite the server already has. Creates
    nothing. Needs Manage Server."""
    if not guild.me.guild_permissions.manage_guild:
        return None
    try:
        vanity = await guild.vanity_invite()
        if vanity:
            return vanity.url
    except (discord.HTTPException, discord.Forbidden):
        pass
    try:
        for inv in await guild.invites():
            if inv.max_age == 0 and not inv.max_uses and not inv.temporary:
                return inv.url
    except (discord.HTTPException, discord.Forbidden):
        pass
    return None


async def find_or_create_invite(guild: discord.Guild, prefer_channel_id=None, reason: str = ""):
    """Returns (url, last_error). Reuses an existing link first; otherwise
    creates a never-expiring one. Only ever called after an admin pressed a
    button — never automatically."""
    url = await find_existing_invite(guild)
    if url:
        return url, None
    last_err = None
    for channel in _channels_to_try(guild, prefer_channel_id):
        if not channel.permissions_for(guild.me).create_instant_invite:
            continue
        try:
            invite = await channel.create_invite(max_age=0, max_uses=0, unique=False, reason=reason or "Bump listing link")
            return invite.url, None
        except (discord.HTTPException, discord.Forbidden) as e:
            last_err = str(e)
    return None, last_err


def explain_missing_link(guild: discord.Guild, prefer_channel_id=None) -> tuple:
    """(reason lines, channel the bot could create an invite in or None)."""
    me = guild.me
    lines = []
    if not me.guild_permissions.manage_guild:
        lines.append("I don't have **Manage Server**, so I can't look up your vanity URL or existing invites to reuse.")
    creatable = next(
        (c for c in _channels_to_try(guild, prefer_channel_id) if c.permissions_for(me).create_instant_invite), None
    )
    if creatable is None:
        lines.append(
            "I don't have **Create Invite** in any channel I can see, so I can't make a new link either. "
            "Give my role **Create Invite** (in the bump channel is enough) — or paste your own link."
        )
    if not lines:
        lines.append(
            "Nothing is blocking me — your server just has no vanity URL or never-expiring invite to reuse, "
            "and I never create invite links on my own. Press the button and I'll make one."
        )
    return lines, creatable


async def validate_pasted_link(bot, guild_id: int, raw: str) -> tuple:
    """(canonical_url, error, note). The invite must be live AND belong to
    this guild — otherwise anyone could put another server's link on a listing."""
    match = _INVITE_RE.match((raw or "").strip())
    if not match:
        return None, "That doesn't look like a Discord invite (discord.gg/… or discord.com/invite/…).", None
    code = match.group(1)
    try:
        invite = await bot.fetch_invite(code)
    except discord.NotFound:
        return None, "Discord says that invite doesn't exist or has expired.", None
    except discord.HTTPException:
        return None, "Couldn't check that link right now — try again in a moment.", None
    if invite.guild is None or invite.guild.id != guild_id:
        return None, "That invite points to a different server.", None
    note = None
    if invite.expires_at or invite.max_age:
        note = "⚠️ This invite expires, so your Join button will stop working then. A never-expiring invite is safer."
    return f"https://discord.gg/{code}", None, note


# ── the flag ─────────────────────────────────────────────────────────────────

def build_flag(guild: discord.Guild, listing_id: int, name: str, bump_channel_id=None) -> tuple:
    lines, _ = explain_missing_link(guild, bump_channel_id)
    embed = discord.Embed(
        title="⚠️ Your bump has no server link",
        description=(
            f"**{name or guild.name}** is on the bump network, but its listing has no invite link — so the ad "
            "other servers see has no **Join** button and nobody can join from it. "
            "New bumps are paused until it has one."
        ),
        color=discord.Color.orange(),
    )
    embed.add_field(name="Why I couldn't add one myself", value="\n".join(f"• {l}" for l in lines)[:1024], inline=False)
    embed.add_field(
        name="How to fix it",
        value=(
            "Press **Get / create my link** and I'll reuse your vanity URL or a never-expiring invite, or make a "
            "new never-expiring one. Or press **Paste my own link**. You need **Manage Server** for either."
        ),
        inline=False,
    )
    view = discord.ui.View(timeout=None)
    view.add_item(DynamicBumpLinkFixButton(listing_id))
    view.add_item(DynamicBumpLinkPasteButton(listing_id))
    return embed, view


async def _flag_already_posted(channel: discord.TextChannel, bot_id: int, listing_id: int) -> bool:
    if not channel.permissions_for(channel.guild.me).read_message_history:
        return False
    fix_id = f"bumplink:fix:{listing_id}"
    try:
        async for msg in channel.history(limit=50):
            if msg.author.id != bot_id:
                continue
            ids = [getattr(c, "custom_id", None) for row in msg.components for c in getattr(row, "children", [])]
            if fix_id in ids:
                return True
    except discord.HTTPException:
        pass
    return False


async def post_flag_in_channel(bot, guild: discord.Guild, row: dict) -> str:
    """Posts the flag in the server's bump channel. row = a
    list_server_listings_missing_link() row. Returns 'posted', 'exists',
    or 'skipped' (channel gone / bot can't post there)."""
    from discord_bot.cogs.bump_setup import ensure_bot_can_post

    channel = guild.get_channel(int(row["bump_channel_id"]))
    if not isinstance(channel, discord.TextChannel) or not await ensure_bot_can_post(channel):
        return "skipped"
    if await _flag_already_posted(channel, bot.user.id, row["listing_id"]):
        return "exists"
    embed, view = build_flag(guild, row["listing_id"], row.get("name"), row["bump_channel_id"])
    configured_by = row.get("configured_by")
    try:
        await channel.send(
            content=f"<@{configured_by}> — your bump listing needs a link." if configured_by else None,
            embed=embed, view=view,
            allowed_mentions=discord.AllowedMentions(users=[discord.Object(id=configured_by)] if configured_by else False),
        )
    except (discord.Forbidden, discord.HTTPException):
        logger.info("[bumplink] couldn't post flag in channel %s", channel.id)
        return "skipped"
    return "posted"


# ── buttons ──────────────────────────────────────────────────────────────────

async def _can_manage(interaction: discord.Interaction, guild: discord.Guild) -> bool:
    """Manage Server in the LISTING's guild — the click can come from a copy
    of the message in another server, so don't trust interaction.permissions
    unless it's the same guild."""
    if interaction.guild_id == guild.id:
        return bool(interaction.permissions.manage_guild)
    member = guild.get_member(interaction.user.id)
    if member is None:
        try:
            member = await guild.fetch_member(interaction.user.id)
        except discord.HTTPException:
            return False
    return member.guild_permissions.manage_guild


class DynamicBumpLinkFixButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"bumplink:fix:(?P<listing_id>\d+)",
):
    def __init__(self, listing_id: int):
        self.listing_id = listing_id
        super().__init__(discord.ui.Button(
            label="Get / create my link", style=discord.ButtonStyle.success, emoji="🔗",
            custom_id=f"bumplink:fix:{listing_id}",
        ))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Item, match: "re.Match[str]", /):
        return cls(int(match["listing_id"]))

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        listing = await db.bump_get_listing(0, None, listing_id=self.listing_id)
        guild = interaction.client.get_guild(listing["guild_id"]) if listing else None
        if not listing or guild is None:
            await interaction.followup.send("That listing (or its server) isn't reachable right now.", ephemeral=True)
            return
        if not await _can_manage(interaction, guild):
            await interaction.followup.send("You need **Manage Server** in that server to do this.", ephemeral=True)
            return
        if has_link(listing):
            await interaction.edit_original_response(
                content=f"✅ **{listing.get('name') or guild.name}** already has a link — you can bump.", embed=None, view=None,
            )
            return
        config = await db.bump_get_guild_config(guild.id, listing.get("clone_id")) or {}
        prefer = config.get("bump_channel_id")
        url, err = await find_or_create_invite(
            guild, prefer, reason=f"Bump listing link — requested by {interaction.user} ({interaction.user.id})",
        )
        if not url:
            lines, _ = explain_missing_link(guild, prefer)
            await interaction.followup.send(
                "❌ I still couldn't get a link:\n" + "\n".join(f"• {l}" for l in lines)
                + (f"\nDiscord said: `{err}`" if err else "")
                + "\nFix that and press the button again, or use **Paste my own link**.",
                ephemeral=True,
            )
            return
        await set_listing_invite(self.listing_id, url)
        await interaction.edit_original_response(
            content=f"✅ Link saved for **{listing.get('name') or guild.name}**: {url}\nYou can bump now.",
            embed=None, view=None,
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
        logger.exception("Unhandled error in DynamicBumpLinkFixButton (listing %s): %s", self.listing_id, error)
        try:
            msg = "Something went wrong — check the bot logs."
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except discord.HTTPException:
            pass


class DynamicBumpLinkPasteButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"bumplink:paste:(?P<listing_id>\d+)",
):
    def __init__(self, listing_id: int):
        self.listing_id = listing_id
        super().__init__(discord.ui.Button(
            label="Paste my own link", style=discord.ButtonStyle.secondary, emoji="✏️",
            custom_id=f"bumplink:paste:{listing_id}",
        ))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Item, match: "re.Match[str]", /):
        return cls(int(match["listing_id"]))

    async def callback(self, interaction: discord.Interaction):
        listing = await db.bump_get_listing(0, None, listing_id=self.listing_id)
        guild = interaction.client.get_guild(listing["guild_id"]) if listing else None
        if not listing or guild is None:
            await interaction.response.send_message("That listing (or its server) isn't reachable right now.", ephemeral=True)
            return
        if not await _can_manage(interaction, guild):
            await interaction.response.send_message("You need **Manage Server** in that server to do this.", ephemeral=True)
            return
        await interaction.response.send_modal(BumpLinkPasteModal(self.listing_id))

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
        logger.exception("Unhandled error in DynamicBumpLinkPasteButton (listing %s): %s", self.listing_id, error)


class BumpLinkPasteModal(discord.ui.Modal, title="Your server's invite link"):
    def __init__(self, listing_id: int):
        super().__init__()
        self.listing_id = listing_id
        self.link = discord.ui.TextInput(placeholder="https://discord.gg/yourcode", max_length=100)
        self.add_item(discord.ui.Label(
            text="Invite link",
            description="Use a never-expiring invite so the Join button doesn't die",
            component=self.link,
        ))

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        listing = await db.bump_get_listing(0, None, listing_id=self.listing_id)
        guild = interaction.client.get_guild(listing["guild_id"]) if listing else None
        if not listing or guild is None:
            await interaction.followup.send("That listing (or its server) isn't reachable right now.", ephemeral=True)
            return
        if not await _can_manage(interaction, guild):
            await interaction.followup.send("You need **Manage Server** in that server to do this.", ephemeral=True)
            return
        url, error, note = await validate_pasted_link(interaction.client, guild.id, str(self.link.value))
        if error:
            await interaction.followup.send(f"❌ {error}", ephemeral=True)
            return
        await set_listing_invite(self.listing_id, url)
        done = f"✅ Link saved for **{listing.get('name') or guild.name}**: {url}\nYou can bump now."
        try:
            # Turns the flag message (public or ephemeral) into a "fixed" note.
            await interaction.edit_original_response(content=done, embed=None, view=None)
        except discord.HTTPException:
            await interaction.followup.send(done, ephemeral=True)
        if note:
            await interaction.followup.send(note, ephemeral=True)


DYNAMIC_ITEMS = (DynamicBumpLinkFixButton, DynamicBumpLinkPasteButton)
