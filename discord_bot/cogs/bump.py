# path: discord_bot/cogs/bump.py

"""
Bump network — lets a server advertise itself (or a bot it owns) to every
other opted-in server on this same clone, and receive their ads back, all
through one shared per-guild "bump channel" (see bump_guild_config /
bump_listings comments in database.py's _create_tables for the schema
reasoning).

Scoped per clone_id throughout, same as autopost.py — each clone is a
separate bot instance with its own guild set, so cross-clone distribution
would post into servers the sending clone was never invited to.

Receiving is mandatory, not a toggle: setting a bump channel via
`/bumpsetup` means this server accepts incoming bumps (server or bot) from
other opted-in servers as a condition of being able to send its own —
`receives_bumps` is always TRUE on setup and `_do_bump`/`bump_bot` both
hard-block if it's ever FALSE. There is no send-only mode.

Auto-suggest (the feature this file adds on top of the base plan): when a
server hasn't set a description/tags yet, `_suggest_description_and_tags()`
pulls a best-effort default from data the guild already has —
`guild.description` (Discord's own Community "Server Description" field)
and a keyword scan over channel/category names — instead of forcing the
admin to type both from scratch. It's a starting point they can edit, not a
hard default: `/bumpsetup` always shows what was picked before saving.

Drip-send: `/bump` doesn't post to every target instantly (rate-limit +
spam risk) — it fills bump_queue via db.bump_enqueue(), staggered
DRIP_SECONDS apart, and a background loop (bump_worker, same tasks.loop
shape as autopost.py's) drains a few due rows every tick.
"""

import asyncio
import logging
import random
import re
import time

import discord
from discord import app_commands
from discord.ext import commands, tasks

from config import DISCORD_CLONE_ADMIN_IDS
from database import db
from discord_bot.cogs._adaptive_skip import AdaptiveSkip
from discord_bot.cogs._views_bump_link import (
    DYNAMIC_ITEMS as BUMP_LINK_DYNAMIC_ITEMS, has_link, build_flag, find_existing_invite,
    list_server_listings_missing_link, post_flag_in_channel, set_listing_invite,
)

logger = logging.getLogger(__name__)

# Free tier: fixed cooldown/intensity, matching the "free = fixed default,
# paid = adjustable" spec agreed on earlier. DEFAULT_COOLDOWN_SECONDS is
# only the fallback — the live value is owner-editable via
# `/bumpadmin cooldown` and stored in admin_config (key: bump_cooldown_seconds),
# same generic key/value table admin.py already uses for other bot-wide
# settings. Per-clone: each clone reads its own admin_config row set by its
# own owner, since clones are separate bot instances with separate owners.
DEFAULT_COOLDOWN_SECONDS = 60 * 60  # 1 hour
STREAK_WINDOW_SECONDS = 48 * 60 * 60  # 48h, matches the reference screenshot
DRIP_SECONDS = 4 * 60  # ~1 ad / 4 min between servers
WORKER_TICK_SECONDS = 60
MAX_SENDS_PER_TICK = 10
# Reminder checks don't need per-minute precision like the send queue does
# — cooldowns run 60min+ by default, so a 5-minute poll is plenty prompt
# without hammering the DB every tick.
REMINDER_TICK_SECONDS = 5 * 60
# Kill switch — set to False to pause reminders (capped at MAX_REMINDERS_PER_TICK per tick).
ENABLE_BUMP_REMINDERS = True
# Caps how many reminders can fire in one 5-minute tick, so a backlog
# (e.g. after downtime) trickles out instead of bursting all at once
# like it did the first time this ran.
MAX_REMINDERS_PER_TICK = 3
# Boot-time sweep that flags server listings with no invite link (see
# _flag_missing_links). Capped so a big backlog trickles out over a few minutes.
MAX_LINK_FLAGS_PER_BOOT = 100

TAG_KEYWORDS = {
    "gaming": ["game", "gaming", "valorant", "minecraft", "fortnite", "esports"],
    "anime": ["anime", "manga", "weeb", "otaku"],
    "art": ["art", "design", "artist", "drawing"],
    "music": ["music", "beats", "producer", "audio"],
    "coding": ["code", "coding", "dev", "programming", "developer"],
    "crypto": ["crypto", "nft", "trading", "web3"],
    "community": ["community", "hangout", "chill", "lounge"],
    "study": ["study", "school", "homework", "students"],
}


def _clone_id_of(bot: commands.Bot):
    return getattr(bot, "clone_id", None)


def _require_manage_guild(interaction: discord.Interaction) -> bool:
    if interaction.guild is None:
        return False
    return bool(interaction.permissions.manage_guild)


def _require_bot_owner(user_id: int) -> bool:
    return user_id in DISCORD_CLONE_ADMIN_IDS


def _suggest_description_and_tags(guild: discord.Guild) -> tuple[str, list[str]]:
    """Best-effort auto-fill, not a guess dressed up as certain — the
    caller always shows this to the admin before saving (see
    BumpSetupView)."""
    description = (getattr(guild, "description", None) or "").strip()
    if not description:
        description = f"{guild.name} — a Discord community with {guild.member_count or '?'} members."

    names = " ".join(c.name.lower() for c in guild.channels) if guild.channels else ""
    names += " " + " ".join((f or "").lower() for f in getattr(guild, "features", []))
    tags = [tag for tag, keywords in TAG_KEYWORDS.items() if any(kw in names for kw in keywords)]
    if not tags:
        tags = ["community"]
    return description, tags[:5]


def _suggest_perks(guild: discord.Guild) -> list[str]:
    """Same spirit as _suggest_description_and_tags — a starting-point
    perks list read off signals the guild already has, not a hard
    default (BumpSetupView/BumpEditModal always show it before saving)."""
    perks: list[str] = []
    if any(c.type == discord.ChannelType.voice for c in guild.channels):
        perks.append("Custom voice channels on demand")
    if any("event" in c.name.lower() or "tournament" in c.name.lower() for c in guild.channels):
        perks.append("Weekly tournaments and events")
    if any("bot" in c.name.lower() or "economy" in c.name.lower() for c in guild.channels):
        perks.append("Custom bots for economy, music, and moderation")
    if getattr(guild, "premium_subscription_count", 0):
        perks.append(f"{guild.premium_subscription_count} server boosts and counting")
    if not perks:
        perks.append("Active, friendly community")
    return perks[:4]


async def _live_counts(bot: commands.Bot, listing: dict) -> tuple[int | None, int | None, str | None]:
    """Best-effort (online, members, icon_url) for a SERVER listing.

    Tries the listing's own invite first via with_counts=True — this
    works even for target guilds the bot isn't a member of and needs no
    privileged Presence Intent. The invite's partial guild object carries
    the server icon too, so this is also where the live icon comes from
    for guilds the bot has never joined. Falls back to the live Guild
    object (member_count and icon both accurate, but online is unknown
    without presences) for guilds the bot IS currently in. Never raises —
    a dead/expired invite just means stats/icon fall back or are omitted,
    not a failed send.
    """
    invite_url = (listing.get("invite_url") or "").strip()
    if invite_url:
        code = invite_url.rstrip("/").split("/")[-1]
        try:
            invite = await bot.fetch_invite(code, with_counts=True)
            icon_url = invite.guild.icon.url if invite.guild and invite.guild.icon else None
            return invite.approximate_presence_count, invite.approximate_member_count, icon_url
        except (discord.NotFound, discord.HTTPException):
            pass
    guild = bot.get_guild(listing.get("guild_id") or listing.get("listing_guild_id"))
    if guild:
        icon_url = guild.icon.url if guild.icon else None
        return None, guild.member_count, icon_url
    return None, None, None


def _extract_client_id(invite_url: str) -> int | None:
    """Pulls client_id out of a bot OAuth invite
    (…?client_id=123&scope=bot…) so we can look the bot up as a live
    Discord user. Deliberately narrow — only matches the OAuth authorize
    link shape /bump bot actually asks for, not arbitrary URLs."""
    match = re.search(r"[?&]client_id=(\d+)", invite_url or "")
    return int(match.group(1)) if match else None


async def _live_bot_info(bot: commands.Bot, invite_url: str) -> dict:
    """Best-effort live data for a BOT listing's stats line.

    There's no legitimate public endpoint for "how many servers/users
    does this arbitrary third-party bot have" — that data belongs to the
    bot's own owner, not to anyone who can see its invite link, so we
    don't fabricate an online/members-style stat here the way we can for
    servers. What IS genuinely fetchable and live: the bot's Discord user
    record — avatar, Discord's own "Verified Bot" badge, and account
    creation date. Never raises — a bad client_id just means the stats
    line/thumbnail falls back or is omitted, not a failed send.
    """
    client_id = _extract_client_id(invite_url)
    if client_id is None:
        return {}
    try:
        user = await bot.fetch_user(client_id)
    except (discord.NotFound, discord.HTTPException):
        return {}
    return {
        "avatar_url": user.display_avatar.url if user.display_avatar else None,
        "verified": bool(user.public_flags.verified_bot),
        "created_at": user.created_at,
    }


def _bump_embed(listing: dict, streak: int, online: int | None = None, members: int | None = None,
                 bot_info: dict | None = None, server_icon_url: str | None = None) -> discord.Embed:
    kind = listing.get("listing_type", "server")
    name = listing.get("name") or "Unnamed"
    bot_info = bot_info or {}
    # Verified bots get the checkmark folded into the title itself (next to
    # the name), matching the reference layout, instead of a separate stats
    # line — Discord embeds have no true inline-icon slot, so the title is
    # the closest equivalent to "badge beside the name".
    verified_badge = " ✅" if (kind == "bot" and bot_info.get("verified")) else ""
    embed = discord.Embed(
        title=f"{'🤖' if kind == 'bot' else '📣'} {name}{verified_badge}",
        description=(listing.get("description") or "No description set.")[:500],
        color=discord.Color.from_str("#22b3a4"),
    )

    if kind == "server":
        if server_icon_url:
            embed.set_thumbnail(url=server_icon_url)
        stats = []
        if members is not None:
            online_part = f"🟢 {online:,} Online  •  " if online is not None else ""
            stats.append(f"{online_part}⚪ {members:,} Members")
        created = listing.get("created_at") or listing.get("listing_created_at")
        if created:
            stats.append(f"📅 Est. {created.strftime('%b %Y')}")
        if stats:
            embed.add_field(name="Stats", value="\n".join(stats), inline=False)
    else:
        stats = []
        if bot_info.get("avatar_url"):
            embed.set_thumbnail(url=bot_info["avatar_url"])
        if "verified" in bot_info and not bot_info["verified"]:
            stats.append("🤖 Unverified bot")
        if bot_info.get("created_at"):
            stats.append(f"📅 Joined Discord {bot_info['created_at'].strftime('%b %Y')}")
        if stats:
            embed.add_field(name="Stats", value="\n".join(stats), inline=False)

    tags = listing.get("tags") or []
    if tags:
        embed.add_field(name="Tags", value=" · ".join(tags), inline=False)

    perks = listing.get("perks") or []
    if perks:
        embed.add_field(name="What we offer", value="\n".join(f"• {p}" for p in perks[:6]), inline=False)

    if listing.get("invite_url"):
        embed.add_field(name="Link", value=listing["invite_url"], inline=False)

    if listing.get("support_url"):
        embed.add_field(name="Support", value=listing["support_url"], inline=False)

    rating_count = listing.get("rating_count") or 0
    rating_sum = listing.get("rating_sum") or 0
    if rating_count:
        avg = rating_sum / rating_count
        rounded = round(avg)
        stars = "★" * rounded + "☆" * (5 - rounded)
        rating_text = f"{stars} ({avg:.1f}, {rating_count})"
    else:
        rating_text = "☆☆☆☆☆ (not yet rated)"
    total_bumps = listing.get("total_bumps") or 0
    embed.set_footer(text=f"{rating_text}  •  Total bumps: {total_bumps}  •  Streak: {streak} 🔥")
    return embed


class DynamicRateOpenButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"bump:rateopen:(?P<listing_id>\d+)",
):
    """★ Rate button on a posted ad card. Dynamic items are matched by
    regex against the custom_id and registered ONCE (bot.add_dynamic_items
    in BumpCog.__init__) rather than per-message, so — unlike a plain
    discord.ui.View — this keeps responding to old messages after a bot
    restart with no per-listing state kept in memory."""

    def __init__(self, listing_id: int):
        self.listing_id = listing_id
        super().__init__(
            discord.ui.Button(label="★ Rate", style=discord.ButtonStyle.secondary, custom_id=f"bump:rateopen:{listing_id}")
        )

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Item, match: "re.Match[str]", /):
        return cls(int(match["listing_id"]))

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            "Rate this listing:",
            view=_rating_view(self.listing_id),
            ephemeral=True,
        )


class DynamicRateStarButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"bump:rate:(?P<listing_id>\d+):(?P<stars>[1-5])",
):
    """One of the 1-5 star buttons on the ephemeral rating picker. Same
    dynamic/persistent mechanism as DynamicRateOpenButton — listing_id
    and the star count both live in the custom_id, not in memory."""

    def __init__(self, listing_id: int, stars: int):
        self.listing_id = listing_id
        self.stars = stars
        super().__init__(
            discord.ui.Button(label=f"{stars}★", style=discord.ButtonStyle.secondary, custom_id=f"bump:rate:{listing_id}:{stars}")
        )

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Item, match: "re.Match[str]", /):
        return cls(int(match["listing_id"]), int(match["stars"]))

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        avg, count = await db.bump_rate_listing(self.listing_id, interaction.user.id, self.stars)
        await interaction.edit_original_response(
            content=f"✅ Thanks for rating {self.stars}★! This listing is now **{avg:.1f}★** across {count} rating{'s' if count != 1 else ''}.",
            view=None,
        )


class DynamicAddMineButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"bump:addmine",
):
    """+ Add mine button on a posted ad card. No per-listing state at
    all, so the template is a fixed string rather than a capture group."""

    def __init__(self):
        super().__init__(discord.ui.Button(label="+ Add mine", style=discord.ButtonStyle.secondary, custom_id="bump:addmine"))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Item, match: "re.Match[str]", /):
        return cls()

    async def callback(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message("This only works inside a server.", ephemeral=True)
            return
        if not _require_manage_guild(interaction):
            await interaction.response.send_message("You need **Manage Server** to set up a listing here.", ephemeral=True)
            return
        # BumpChannelSelectView needs the real cog instance — its two
        # handlers call self.cog._finish_channel_setup(...). This is a
        # DynamicItem with no reference to any live cog stored anywhere
        # (by design, so it survives restarts), so None was being passed
        # here instead, which crashed with AttributeError the moment
        # someone picked a channel or clicked "Create a #bump channel for
        # me" — the primary growth-loop button on every posted ad card.
        # interaction.client.get_cog(...) fetches the actual live cog
        # fresh at click time, which is always safe here since
        # BumpChannelSelectView itself is a plain (non-dynamic) view shown
        # only as an immediate reply within this interaction's short
        # response window, never something clicked days later.
        await interaction.response.send_message(
            "Which channel should receive bumps (and send yours)?",
            view=BumpChannelSelectView(interaction.client.get_cog("BumpCog")),
            ephemeral=True,
        )


class DynamicBumpPromptButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"bump:prompt:(?P<listing_id>\d+)",
):
    """The standalone 🔁 Bump button. It is NOT part of the ad card: after
    every successful bump the bot posts a separate message with this button
    in the bumping server's own bump channel (see BumpCog._post_bump_prompt),
    so it is visible to everyone there and ANYONE in that server can press it
    — no Manage Server needed. The cooldown is what gates it: pressing it
    early just tells the clicker when the next bump unlocks. A successful
    press bumps and posts a fresh prompt, so the button always reappears
    after every bump. DynamicItem (custom_id carries the listing id) so it
    keeps working on old messages after a restart."""

    def __init__(self, listing_id: int):
        self.listing_id = listing_id
        super().__init__(
            discord.ui.Button(label="Bump", style=discord.ButtonStyle.success, emoji="🔁",
                              custom_id=f"bump:prompt:{listing_id}")
        )

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Item, match: "re.Match[str]", /):
        return cls(int(match["listing_id"]))

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        cog = interaction.client.get_cog("BumpCog")
        if cog is None:
            await interaction.followup.send("Bump isn't available right now — try again shortly.", ephemeral=True)
            return
        listing = await db.bump_get_listing(0, None, listing_id=self.listing_id)
        if not listing:
            await interaction.followup.send("This listing no longer exists.", ephemeral=True)
            return
        owner_guild_id = listing["guild_id"]
        if interaction.guild_id != owner_guild_id:
            await interaction.followup.send("This bump button belongs to a different server.", ephemeral=True)
            return
        clone_id = listing.get("clone_id")
        config = await db.bump_get_guild_config(owner_guild_id, clone_id)
        if not config or not config.get("bump_channel_id"):
            await interaction.followup.send("This server hasn't finished `/bumpsetup` yet.", ephemeral=True)
            return
        if not config.get("receives_bumps", True):
            await interaction.followup.send(
                "This server's bump channel isn't accepting bumps right now — an admin needs to run `/bumpsetup` again.",
                ephemeral=True,
            )
            return
        cooldown_seconds = await cog._cooldown_seconds()
        can_bump, remaining = await db.bump_check_cooldown(listing["id"], cooldown_seconds)
        if not can_bump:
            await interaction.followup.send(
                f"⏳ Already bumped recently — you can bump again <t:{int(time.time()) + remaining}:R>.",
                ephemeral=True,
            )
            return
        await cog._do_bump(interaction, config, listing, clone_id, owner_guild_id=owner_guild_id)

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
        logger.exception("Unhandled error in DynamicBumpPromptButton (listing %s): %s", self.listing_id, error)
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message("Something went wrong — check the bot logs.", ephemeral=True)
            else:
                await interaction.followup.send("Something went wrong — check the bot logs.", ephemeral=True)
        except discord.HTTPException:
            pass


class DynamicBumpAgainButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"bump:again:(?P<listing_id>\d+)",
):
    """🔁 Bump again button on a posted ad card — lets staff re-bump the
    instant the cooldown clears without going to find `/bump now`. Same
    dynamic/persistent mechanism as the other buttons on this card (see
    DynamicRateOpenButton docstring), which matters extra here: this
    card gets drip-posted into every OTHER opted-in server too, so most
    clicks land on a copy of the card sitting in a server that isn't the
    listing's own. That's why this looks the listing's owning guild up
    fresh from listing_id and permission-checks against THAT guild's
    membership, instead of trusting interaction.guild_id / the clicking
    user's permissions in whatever server the card happens to be in —
    otherwise anyone with Manage Server in a server that merely
    *received* the ad could bump someone else's listing."""

    def __init__(self, listing_id: int):
        self.listing_id = listing_id
        super().__init__(
            discord.ui.Button(label="🔁 Bump again", style=discord.ButtonStyle.primary, custom_id=f"bump:again:{listing_id}")
        )

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Item, match: "re.Match[str]", /):
        return cls(int(match["listing_id"]))

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        listing = await db.bump_get_listing(0, None, listing_id=self.listing_id)
        if not listing:
            await interaction.followup.send("This listing no longer exists.", ephemeral=True)
            return

        owner_guild_id = listing["guild_id"]
        owner_guild = interaction.client.get_guild(owner_guild_id)
        if owner_guild is None:
            await interaction.followup.send("The server that owns this listing isn't reachable right now.", ephemeral=True)
            return

        member = owner_guild.get_member(interaction.user.id)
        if member is None:
            try:
                member = await owner_guild.fetch_member(interaction.user.id)
            except discord.HTTPException:
                member = None
        if member is None or not member.guild_permissions.manage_guild:
            await interaction.followup.send(
                "Only someone with **Manage Server** in the listing's own server can bump it again.",
                ephemeral=True,
            )
            return

        clone_id = listing.get("clone_id")
        config = await db.bump_get_guild_config(owner_guild_id, clone_id)
        if not config or not config.get("bump_channel_id"):
            await interaction.followup.send("That server hasn't finished `/bumpsetup` yet.", ephemeral=True)
            return
        if not config.get("receives_bumps", True):
            await interaction.followup.send(
                "That server's bump channel isn't accepting bumps right now — run `/bumpsetup` there again to re-confirm.",
                ephemeral=True,
            )
            return

        cog = interaction.client.get_cog("BumpCog")
        if cog is None:
            await interaction.followup.send("Bump isn't available right now — try again shortly.", ephemeral=True)
            return
        await cog._do_bump(interaction, config, listing, clone_id, owner_guild_id=owner_guild_id)

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
        logger.exception("Unhandled error in DynamicBumpAgainButton (listing %s): %s", self.listing_id, error)
        if not interaction.response.is_done():
            try:
                await interaction.response.send_message("Something went wrong — check the bot logs.", ephemeral=True)
            except discord.HTTPException:
                pass
        else:
            try:
                await interaction.followup.send("Something went wrong — check the bot logs.", ephemeral=True)
            except discord.HTTPException:
                pass


class DynamicBumpApproveButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"bump:approve:(?P<listing_id>\d+)",
):
    """Approve button on the bot-owner DM sent by api/bump_oauth.py's
    _notify_admins_of_pending when a bot listing is submitted for
    review. That DM is built and sent over raw REST from api_server.py
    — a separate process with no live gateway session — so it can't
    carry a normal discord.ui.View tied to a live message/interaction.
    A DynamicItem with a static custom_id works anyway: Discord routes
    the click to whichever process holds the gateway connection (this
    one, bot.py) regardless of which process sent the original message,
    and — same as DynamicRateOpenButton — it keeps responding after a
    restart since there's no per-listing state kept in memory."""

    def __init__(self, listing_id: int):
        self.listing_id = listing_id
        super().__init__(
            discord.ui.Button(label="Approve", style=discord.ButtonStyle.success, emoji="✅",
                               custom_id=f"bump:approve:{listing_id}")
        )

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Item, match: "re.Match[str]", /):
        return cls(int(match["listing_id"]))

    async def callback(self, interaction: discord.Interaction):
        # The DM is only ever sent to DISCORD_CLONE_ADMIN_IDS, but the
        # custom_id itself carries no identity check — re-verify here
        # rather than relying solely on "only the recipient can see
        # their own DM" to gate an approve/reject action.
        if not _require_bot_owner(interaction.user.id):
            await interaction.response.send_message("This is owner-only.", ephemeral=True)
            return
        await interaction.response.defer()
        try:
            listing = await db.bump_review_listing(self.listing_id, approve=True)
        except Exception:
            logger.exception("bump_review_listing(approve) failed for listing %s", self.listing_id)
            await interaction.followup.send("Something went wrong approving this — check the bot logs.", ephemeral=True)
            return
        label = f"✅ Approved **{listing['name']}**." if listing else "Already handled (listing not found)."
        await interaction.edit_original_response(content=label, embed=None, view=None)
        if listing and listing.get("verified_owner_id"):
            from discord_bot.cogs._views_bump import _notify_submitter
            await _notify_submitter(interaction.client, listing["verified_owner_id"], listing["name"], approved=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
        logger.exception("Unhandled error in DynamicBumpApproveButton (listing %s): %s", self.listing_id, error)
        if not interaction.response.is_done():
            try:
                await interaction.response.send_message("Something went wrong — check the bot logs.", ephemeral=True)
            except discord.HTTPException:
                pass


class DynamicBumpRejectButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"bump:reject:(?P<listing_id>\d+)",
):
    """Reject counterpart to DynamicBumpApproveButton — see its
    docstring for why this has to be a DynamicItem rather than a plain
    View."""

    def __init__(self, listing_id: int):
        self.listing_id = listing_id
        super().__init__(
            discord.ui.Button(label="Reject", style=discord.ButtonStyle.danger, emoji="🚫",
                               custom_id=f"bump:reject:{listing_id}")
        )

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Item, match: "re.Match[str]", /):
        return cls(int(match["listing_id"]))

    async def callback(self, interaction: discord.Interaction):
        if not _require_bot_owner(interaction.user.id):
            await interaction.response.send_message("This is owner-only.", ephemeral=True)
            return
        await interaction.response.defer()
        try:
            listing = await db.bump_review_listing(self.listing_id, approve=False)
        except Exception:
            logger.exception("bump_review_listing(reject) failed for listing %s", self.listing_id)
            await interaction.followup.send("Something went wrong rejecting this — check the bot logs.", ephemeral=True)
            return
        await interaction.edit_original_response(content="🚫 Rejected and removed.", embed=None, view=None)
        if listing and listing.get("verified_owner_id"):
            from discord_bot.cogs._views_bump import _notify_submitter
            await _notify_submitter(interaction.client, listing["verified_owner_id"], listing["name"], approved=False)

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
        logger.exception("Unhandled error in DynamicBumpRejectButton (listing %s): %s", self.listing_id, error)
        if not interaction.response.is_done():
            try:
                await interaction.response.send_message("Something went wrong — check the bot logs.", ephemeral=True)
            except discord.HTTPException:
                pass


def _rating_view(listing_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    for n in range(1, 6):
        view.add_item(DynamicRateStarButton(listing_id, n))
    return view


def _bump_post_view(listing_id: int, invite_url: str | None, support_url: str | None = None) -> discord.ui.View:
    """The button row under a posted ad card — matches the reference
    design's Join / + Add mine / ★ Rate row, plus an optional Support
    link when the listing owner has set one, plus 🔁 Bump again so staff
    can re-bump the moment the cooldown clears without going to find
    `/bump now`. Built from DynamicItems so it keeps working on old
    messages after a bot restart (see DynamicRateOpenButton docstring)."""
    view = discord.ui.View(timeout=None)
    if invite_url:
        view.add_item(discord.ui.Button(label="Join server", style=discord.ButtonStyle.link, url=invite_url, emoji="🔗"))
    if support_url:
        view.add_item(discord.ui.Button(label="Support", style=discord.ButtonStyle.link, url=support_url, emoji="🛟"))
    view.add_item(DynamicAddMineButton())
    view.add_item(DynamicRateOpenButton(listing_id))
    # No 🔁 Bump again here anymore: bumping is a standalone message posted
    # after every bump (DynamicBumpPromptButton). DynamicBumpAgainButton stays
    # registered below only so buttons on already-posted cards keep working.
    return view



class BumpEditModal(discord.ui.Modal, title="Edit bump listing"):
    def __init__(self, cog: "BumpCog", listing_type: str, name: str, description: str, tags: list[str],
                 invite_url: str, perks: list[str] | None = None, support_url: str = "",
                 listing_id: int | None = None):
        super().__init__()
        self.cog = cog
        self.listing_type = listing_type
        self.listing_id = listing_id
        self.name_input = discord.ui.TextInput(label="Name", default=name[:100], max_length=100)
        self.description_input = discord.ui.TextInput(
            label="Description", style=discord.TextStyle.paragraph, default=description[:1000], max_length=1000
        )
        self.tags_input = discord.ui.TextInput(
            label="Tags (comma separated)", default=", ".join(tags)[:100], max_length=100, required=False
        )
        self.invite_input = discord.ui.TextInput(
            label="Invite link (needed for the Join button)", default=invite_url[:200], required=False, max_length=200
        )
        self.perks_input = discord.ui.TextInput(
            label="Perks (also 'support: <link>')",
            style=discord.TextStyle.paragraph,
            default="\n".join(perks or [])[:400],
            required=False,
            max_length=400,
        )
        # Discord modals cap out at 5 components, and this listing already
        # needs name/description/tags/invite/perks — so the support link
        # rides in the perks field as an optional trailing line rather than
        # getting its own TextInput. Free text, not a channel/server
        # picker: the support server is very often a *different* server
        # than the one this listing lives in, so there's no local
        # guild/channel object to pick from anyway.
        self.perks_input.default = (self.perks_input.default or "")
        if support_url:
            trailer = f"support: {support_url}"
            self.perks_input.default = (self.perks_input.default + ("\n" if self.perks_input.default else "") + trailer)[:400]
        self.add_item(self.name_input)
        self.add_item(self.description_input)
        self.add_item(self.tags_input)
        self.add_item(self.invite_input)
        self.add_item(self.perks_input)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        tags = [t.strip() for t in self.tags_input.value.split(",") if t.strip()][:5]
        raw_lines = [p.strip() for p in self.perks_input.value.splitlines() if p.strip()]
        support_url = None
        perks = []
        for line in raw_lines:
            if line.lower().startswith("support:"):
                support_url = line.split(":", 1)[1].strip() or None
            else:
                perks.append(line)
        perks = perks[:4]
        clone_id = _clone_id_of(interaction.client)
        saved = await db.bump_upsert_listing(
            guild_id=interaction.guild_id,
            clone_id=clone_id,
            created_by=interaction.user.id,
            listing_type=self.listing_type,
            name=self.name_input.value.strip(),
            description=self.description_input.value.strip(),
            invite_url=self.invite_input.value.strip() or None,
            tags=tags,
            perks=perks,
            support_url=support_url,
            listing_id=self.listing_id,
        )
        await interaction.followup.send("✅ Listing saved.", ephemeral=True)
        if self.listing_type == "server" and not has_link(saved):
            url = await find_existing_invite(interaction.guild)
            if url:
                await set_listing_invite(saved["id"], url)
                await interaction.followup.send(f"🔗 No link was set, so I reused your existing invite: {url}", ephemeral=True)
            else:
                config = await db.bump_get_guild_config(interaction.guild_id, clone_id) or {}
                embed, view = build_flag(interaction.guild, saved["id"], saved.get("name"), config.get("bump_channel_id"))
                await interaction.followup.send(embed=embed, view=view, ephemeral=True)


class BumpSetupView(discord.ui.View):
    """Shown after `/bumpsetup` picks a channel — displays the
    auto-suggested description/tags and lets the admin accept or edit
    before anything is saved."""

    def __init__(self, cog: "BumpCog", suggested_desc: str, suggested_tags: list[str], suggested_perks: list[str] | None = None):
        super().__init__(timeout=300)
        self.cog = cog
        self.suggested_desc = suggested_desc
        self.suggested_tags = suggested_tags
        self.suggested_perks = suggested_perks or []

    @discord.ui.button(label="Use suggested description & tags", style=discord.ButtonStyle.success, emoji="✨")
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        clone_id = _clone_id_of(interaction.client)
        # Server listings never got an invite_url before — it was left
        # None here and only ever set manually via /bump edit. The bot
        # already knows how to find/create one (same helper on_guild_join
        # uses), so auto-populate it here instead of shipping every new
        # server listing without a "Join server" button.
        invite_url = await interaction.client._best_effort_invite(interaction.guild)
        saved = await db.bump_upsert_listing(
            guild_id=interaction.guild_id,
            clone_id=clone_id,
            created_by=interaction.user.id,
            listing_type="server",
            name=interaction.guild.name,
            description=self.suggested_desc,
            invite_url=invite_url,
            tags=self.suggested_tags,
            perks=self.suggested_perks,
        )
        await interaction.edit_original_response(
            content=f"✅ Saved.\n**Description:** {self.suggested_desc}\n**Tags:** {', '.join(self.suggested_tags)}",
            view=None,
        )
        if not has_link(saved):
            config = await db.bump_get_guild_config(interaction.guild_id, clone_id) or {}
            embed, view = build_flag(interaction.guild, saved["id"], saved.get("name"), config.get("bump_channel_id"))
            await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    @discord.ui.button(label="Edit before saving", style=discord.ButtonStyle.secondary, emoji="✏️")
    async def edit(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(
            BumpEditModal(self.cog, "server", interaction.guild.name, self.suggested_desc, self.suggested_tags, "", self.suggested_perks, "")
        )


class BumpBotMenuView(discord.ui.View):
    """Shown by /bump bot when the guild already has approved bot
    listings — lets the admin bump one directly instead of only ever
    being able to add a new one. cls kept in bump.py (not
    _views_bump.py) since it calls back into the cog's _do_bump."""

    def __init__(self, cog: "BumpCog", guild_id: int, clone_id: int | None, approved: list):
        super().__init__(timeout=180)
        self.cog = cog
        self.guild_id = guild_id
        self.clone_id = clone_id
        self.select: discord.ui.Select | None = None
        if approved:
            options = [discord.SelectOption(label=l["name"][:100], value=str(l["id"])) for l in approved[:25]]
            self.select = discord.ui.Select(placeholder="Bump an existing bot listing", options=options)
            self.select.callback = self._bump_selected
            self.add_item(self.select)

    async def _bump_selected(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        listing_id = int(self.select.values[0])
        clone_id = _clone_id_of(interaction.client)
        config = await db.bump_get_guild_config(self.guild_id, clone_id)
        listing = await db.bump_get_listing(self.guild_id, clone_id, listing_id=listing_id)
        if not listing:
            await interaction.followup.send("That listing no longer exists.", ephemeral=True)
            return
        await self.cog._do_bump(interaction, config, listing, clone_id)

    @discord.ui.button(label="Add a new bot", style=discord.ButtonStyle.primary, emoji="🤖")
    async def add_new(self, interaction: discord.Interaction, button: discord.ui.Button):
        from discord_bot.cogs._views_bump import BumpBotIdModal
        await interaction.response.send_modal(BumpBotIdModal(self.guild_id, self.clone_id))

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
        logger.exception("Unhandled error in BumpBotMenuView (guild %s): %s", self.guild_id, error)
        if not interaction.response.is_done():
            try:
                await interaction.response.send_message("Something went wrong — check the bot logs.", ephemeral=True)
            except discord.HTTPException:
                pass


class BumpChannelSelectView(discord.ui.View):
    def __init__(self, cog: "BumpCog"):
        super().__init__(timeout=180)
        self.cog = cog

    @discord.ui.select(cls=discord.ui.ChannelSelect, channel_types=[discord.ChannelType.text], placeholder="Pick an existing channel")
    async def pick(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        channel = select.values[0]
        resolved_channel = interaction.guild.get_channel(channel.id) or await interaction.guild.fetch_channel(channel.id)
        perms = resolved_channel.permissions_for(interaction.guild.me)
        if not (perms.send_messages and perms.embed_links):
            await interaction.response.edit_message(
                content=f"I need **Send Messages** and **Embed Links** in {channel.mention} — pick a different channel or fix permissions.",
                view=self,
            )
            return
        await self.cog._finish_channel_setup(interaction, resolved_channel, respond_via="edit_message")

    @discord.ui.button(label="Create a #bump channel for me", style=discord.ButtonStyle.success, emoji="➕")
    async def create_channel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild.me.guild_permissions.manage_channels:
            await interaction.response.edit_message(
                content="I need the **Manage Channels** permission to create one — grant that, or pick an existing channel above instead.",
                view=self,
            )
            return
        try:
            overwrites = {
                interaction.guild.default_role: discord.PermissionOverwrite(send_messages=False),
                interaction.guild.me: discord.PermissionOverwrite(send_messages=True, embed_links=True, manage_messages=True),
            }
            new_channel = await interaction.guild.create_text_channel(
                "bump",
                overwrites=overwrites,
                reason=f"Auto-created by /bumpsetup for {interaction.user}",
            )
        except discord.Forbidden:
            await interaction.response.edit_message(
                content="Couldn't create the channel — missing permissions. Pick an existing channel above instead.",
                view=self,
            )
            return
        except discord.HTTPException as e:
            logger.error(f"[v0] bump auto-create channel failed for guild {interaction.guild_id}: {e}")
            await interaction.response.edit_message(
                content="Something went wrong creating the channel. Pick an existing channel above instead.",
                view=self,
            )
            return
        # Locked to bot-only posting by default (default_role send_messages=False)
        # since bump content is meant to be automated announcements, not a
        # general chat channel — staff with Manage Channels can loosen this
        # afterward if they'd rather members see/post there freely.
        await self.cog._finish_channel_setup(interaction, new_channel, respond_via="edit_message",
                                              extra_note=f"Created {new_channel.mention} (posting there is bot-only by default — adjust its permissions anytime).")


class BumpCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # A clone with an empty bump queue / no reminders due shouldn't
        # keep hitting the DB every tick forever — see _adaptive_skip.py.
        self._worker_gate = AdaptiveSkip(idle_threshold=5, idle_skip=5)
        self._reminder_gate = AdaptiveSkip(idle_threshold=5, idle_skip=8)
        # Registers the custom_id regex patterns once, globally — this is
        # what makes the ad-card buttons keep responding on old messages
        # after a restart, with no per-listing state kept in memory (see
        # DynamicRateOpenButton docstring above). Cheap/idempotent to call
        # again on a cog reload; discord.py just overwrites the same keys.
        bot.add_dynamic_items(
            DynamicRateOpenButton, DynamicRateStarButton, DynamicAddMineButton,
            DynamicBumpAgainButton, DynamicBumpPromptButton, DynamicBumpApproveButton, DynamicBumpRejectButton,
            *BUMP_LINK_DYNAMIC_ITEMS,
        )
        self.bump_worker.start()
        self._restore_task = None
        self._link_task = None
        self._general_task = None
        # Reminder worker is temporarily disabled — a startup crash-loop
        # let cooldowns pile up across many listings, so the first tick
        # after the bot finally came back up fired reminders for all of
        # them at once. Flip ENABLE_BUMP_REMINDERS back to True to
        # re-enable once satisfied it won't burst like that again (e.g.
        # add a per-tick send cap, same as bump_worker's MAX_SENDS_PER_TICK).
        if ENABLE_BUMP_REMINDERS:
            self.bump_reminder_worker.start()

    async def cog_load(self):
        # One background pass per boot; see _auto_restore_bump_channels.
        self._restore_task = asyncio.create_task(self._auto_restore_bump_channels())
        self._general_task = asyncio.create_task(self._move_bump_out_of_general())
        self._link_task = asyncio.create_task(self._flag_missing_links())

    def cog_unload(self):
        if self._restore_task is not None:
            self._restore_task.cancel()
        if self._link_task is not None:
            self._link_task.cancel()
        if self._general_task is not None:
            self._general_task.cancel()
        self.bump_worker.cancel()
        if ENABLE_BUMP_REMINDERS:
            self.bump_reminder_worker.cancel()

    async def _finish_channel_setup(self, interaction: discord.Interaction, channel: discord.TextChannel,
                                     respond_via: str, extra_note: str = ""):
        """Shared tail of both bump-channel setup paths (pick existing /
        create new): saves the config, builds the suggested listing (if
        there isn't one already), and replies. Kept in one place so the
        two entry points in BumpChannelSelectView can't drift out of sync."""
        clone_id = _clone_id_of(interaction.client)
        await db.bump_set_guild_config(
            guild_id=interaction.guild_id, clone_id=clone_id, configured_by=interaction.user.id,
            bump_channel_id=channel.id, receives_bumps=True,
        )

        prefix = f"{extra_note}\n\n" if extra_note else ""
        desc, tags = _suggest_description_and_tags(interaction.guild)
        perks = _suggest_perks(interaction.guild)
        existing = await db.bump_get_listing(interaction.guild_id, clone_id, "server")
        respond = interaction.response.edit_message if respond_via == "edit_message" else interaction.response.send_message

        if existing:
            await respond(
                content=(
                    f"{prefix}✅ Bump channel set to {channel.mention}. Note: this server will **receive** other "
                    f"servers' and bots' bumps here too — that's required to use `/bump` yourself.\n"
                    f"Your listing is already configured — use `/bump edit` to change it."
                ),
                view=None,
            )
            return

        await respond(
            content=(
                f"{prefix}✅ Bump channel set to {channel.mention}. Note: this server will **receive** other "
                f"servers' and bots' bumps here too — that's required to use `/bump` yourself.\n\n"
                f"Here's a suggested listing pulled from your server:\n"
                f"**Description:** {desc}\n**Tags:** {', '.join(tags)}\n"
                f"**What we offer:** {', '.join(perks)}"
            ),
            view=BumpSetupView(self, desc, tags, perks),
        )

    async def _auto_restore_bump_channels(self):
        """Automatic, silent restore of #bump channels wiped by the old
        vote_bump_cleanup pass (channel deleted + bump_channel_id cleared +
        receives_bumps FALSE). Runs once per boot per bot, in the background.

        Every clone is a separate bot with its own bump_guild_config row for
        the same guild, so several bots can be restoring the same guild at
        once. To make sure that never yields two #bump channels:
          - a per-guild Postgres advisory lock (transaction-scoped, so it
            works through the pooler) serializes check-then-create across ALL
            bots/processes, and
          - inside the lock the guild's channels are re-fetched over REST, so
            a #bump another clone created seconds ago is always seen, not just
            whatever this bot has cached.
        If a #bump already exists (one OR several) it is reused and NEVER
        added to; a second one is only ever created when none exists. Nothing
        is posted anywhere — no channel messages, no DMs; only log lines."""
        try:
            await self.bot.wait_until_ready()
            # Spread clones out so they don't all hit the same guilds at once.
            await asyncio.sleep(random.uniform(20, 90))
            clone_id = _clone_id_of(self.bot)
            rows = await db.bump_list_cleared_guilds(clone_id)
            if not rows:
                return
            restored = reused = skipped = 0
            for row in rows:
                guild = self.bot.get_guild(row["guild_id"])
                if guild is None:
                    continue
                try:
                    result = await self._restore_one_guild(guild, clone_id, row.get("configured_by"))
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("[bump] auto-restore failed for guild %s", guild.id)
                    result = "skipped"
                if result == "created":
                    restored += 1
                    await asyncio.sleep(2)  # stay well under channel-create rate limits
                elif result == "reused":
                    reused += 1
                else:
                    skipped += 1
            logger.info("[bump] auto-restore (clone_id=%s): created %d, re-linked %d, skipped %d",
                        clone_id, restored, reused, skipped)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[bump] auto-restore pass crashed")

    @staticmethod
    def _is_network_message(msg: discord.Message, bot_id: int) -> bool:
        """True only for messages THIS bot posted as part of the bump network:
        bump/listing cards, sponsored ad cards, the 🔁 Bump prompt, cooldown
        reminders, and the link-flag / restore notes. Nothing else."""
        if msg.author.id != bot_id:
            return False
        for embed in msg.embeds:
            footer = (embed.footer.text or "") if embed.footer else ""
            if "Total bumps:" in footer or footer.startswith("📢 Sponsored"):
                return True
        for row in msg.components:
            for comp in getattr(row, "children", []):
                cid = getattr(comp, "custom_id", None) or ""
                if cid.startswith(("bump:", "bumplink:")):
                    return True
        content = msg.content or ""
        return content.startswith(("⏱️ Bump cooldown's reset for", "📣 **Bump channel restored.**"))

    async def _delete_network_messages(self, channel: discord.TextChannel, limit: int = 300) -> int:
        """Deletes this bot's own bump/ad messages from a channel they should
        never have been posted in (see _is_network_message). Own messages can
        be deleted without any extra permission; needs Read Message History
        to find them. Best-effort — never raises."""
        deleted = 0
        try:
            if not channel.permissions_for(channel.guild.me).read_message_history:
                return 0
            targets = [m async for m in channel.history(limit=limit) if self._is_network_message(m, self.bot.user.id)]
            for msg in targets:
                try:
                    await msg.delete()
                    deleted += 1
                except discord.NotFound:
                    pass
                except discord.HTTPException:
                    logger.info("[bump] couldn't delete message %s in %s", msg.id, channel.id)
                await asyncio.sleep(0.5)  # stay under delete rate limits
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[bump] cleanup of old bump messages failed in channel %s", getattr(channel, "id", None))
        return deleted

    async def _move_bump_out_of_general(self):
        """One pass per boot: servers whose bump channel is their system
        channel / #general get moved to a dedicated #bump (reused if one
        exists, else created bot-posting-only) and this bot's own bump/ad
        messages already sitting in the old channel are deleted. An older version of the
        "Enable Bump Network" button defaulted to the system channel, which put
        other servers' bumps and sponsored ads into the main chat. Only touches
        channels the bot did NOT create itself; if the bot can't create/reuse a
        #bump (no Manage Channels) the server is left as-is and logged."""
        try:
            await self.bot.wait_until_ready()
            await asyncio.sleep(random.uniform(60, 120))
            clone_id = _clone_id_of(self.bot)
            from database import get_pool
            pool = await get_pool()
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT guild_id, bump_channel_id, configured_by FROM bump_guild_config
                    WHERE COALESCE(clone_id, -1) = COALESCE($1, -1) AND bump_channel_id IS NOT NULL
                      AND COALESCE(channel_auto_created, FALSE) = FALSE
                    """,
                    clone_id,
                )
            moved = skipped = 0
            for row in rows:
                guild = self.bot.get_guild(row["guild_id"])
                if guild is None:
                    continue
                channel = guild.get_channel(int(row["bump_channel_id"]))
                if channel is None or "bump" in channel.name.lower():
                    continue
                from discord_bot.cogs._views_join_dm import _default_text_channel
                if not (channel == _default_text_channel(guild) or "general" in channel.name.lower()):
                    continue
                try:
                    result = await self._restore_one_guild(guild, clone_id, row.get("configured_by"))
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("[bump] couldn't move guild %s out of #%s", guild.id, channel.name)
                    result = "skipped"
                if result in ("created", "reused"):
                    moved += 1
                    removed = await self._delete_network_messages(channel)
                    logger.info("[bump] guild %s: moved out of #%s, deleted %d bump/ad message(s) from it",
                                guild.id, channel.name, removed)
                    fresh = await db.bump_get_guild_config(guild.id, clone_id) or {}
                    new_channel = guild.get_channel(int(fresh["bump_channel_id"])) if fresh.get("bump_channel_id") else None
                    if new_channel is not None and result == "created":
                        try:
                            await new_channel.send(
                                f"📣 Bumps and sponsored ads used to land in {channel.mention}. "
                                "I've moved them here so the main chat stays clean. "
                                "Run `/bumpsetup` if you'd like a different channel."
                            )
                        except discord.HTTPException:
                            pass
                    await asyncio.sleep(3)
                else:
                    skipped += 1
            if moved or skipped:
                logger.info("[bump] moved %d server(s) out of #general (clone_id=%s); %d couldn't be moved (no Manage Channels?)",
                            moved, clone_id, skipped)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[bump] general-channel cleanup pass crashed")

    async def _flag_missing_links(self):
        """One pass per boot: every server listing with no invite link —
        whether or not it was ever bumped, and including ones created before
        this check existed — gets either a silent fix (the server already has
        a vanity URL / permanent invite) or a flag in its bump channel that
        explains why no link exists and gives its admins buttons to fix it.
        A flag that's already sitting in the channel isn't posted twice."""
        try:
            await self.bot.wait_until_ready()
            # After _auto_restore_bump_channels has had time to settle channels.
            await asyncio.sleep(random.uniform(150, 300))
            clone_id = _clone_id_of(self.bot)
            rows = await list_server_listings_missing_link(clone_id)
            if not rows:
                return
            auto = flagged = existing = skipped = 0
            for row in rows[:MAX_LINK_FLAGS_PER_BOOT]:
                guild = self.bot.get_guild(row["guild_id"])
                if guild is None:
                    continue
                try:
                    url = await find_existing_invite(guild)
                    if url:
                        await set_listing_invite(row["listing_id"], url)
                        auto += 1
                        continue
                    result = await post_flag_in_channel(self.bot, guild, row)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("[bump] link check failed for listing %s", row.get("listing_id"))
                    continue
                if result == "posted":
                    flagged += 1
                    await asyncio.sleep(3)  # stay well under send rate limits
                elif result == "exists":
                    existing += 1
                else:
                    skipped += 1
            logger.info(
                "[bump] missing-link sweep (clone_id=%s): %d listing(s) without a link — auto-filled %d, flagged %d, "
                "already flagged %d, couldn't post %d%s",
                clone_id, len(rows), auto, flagged, existing, skipped,
                f", {len(rows) - MAX_LINK_FLAGS_PER_BOOT} left for next boot" if len(rows) > MAX_LINK_FLAGS_PER_BOOT else "",
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[bump] missing-link sweep crashed")

    async def _restore_one_guild(self, guild: discord.Guild, clone_id, configured_by) -> str:
        from database import get_pool
        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                # Held until this transaction ends — i.e. until after the
                # channel exists and the config is saved.
                await conn.execute("SELECT pg_advisory_xact_lock($1)", 7_310_000_000_000 + guild.id % 1_000_000_000_000)
                try:
                    channels = await guild.fetch_channels()
                except discord.HTTPException:
                    return "skipped"
                existing = [c for c in channels if isinstance(c, discord.TextChannel) and c.name == "bump"]
                me = guild.me
                channel, created = None, False
                if existing:
                    # Reuse — prefer one this bot can already post in.
                    def _can_post(c):
                        p = c.permissions_for(me)
                        return p.view_channel and p.send_messages and p.embed_links
                    channel = next((c for c in existing if _can_post(c)), existing[0])
                    if not _can_post(channel):
                        # Another clone's #bump is bot-only for that clone. Give this
                        # bot access if we're allowed to; otherwise stay quiet and
                        # leave it — never create a second channel.
                        try:
                            await channel.set_permissions(
                                me, send_messages=True, embed_links=True, view_channel=True,
                                reason="Bump channel restore: let this bot post in the existing #bump",
                            )
                        except (discord.Forbidden, discord.HTTPException):
                            return "skipped"
                else:
                    if not me.guild_permissions.manage_channels:
                        return "skipped"
                    try:
                        channel = await guild.create_text_channel(
                            "bump",
                            overwrites={
                                guild.default_role: discord.PermissionOverwrite(send_messages=False),
                                me: discord.PermissionOverwrite(send_messages=True, embed_links=True, manage_messages=True),
                            },
                            reason="Restoring bump channel removed by the old auto-cleanup",
                        )
                    except (discord.Forbidden, discord.HTTPException):
                        return "skipped"
                    created = True
                await db.bump_set_guild_config(
                    guild_id=guild.id, clone_id=clone_id, configured_by=configured_by or self.bot.user.id,
                    bump_channel_id=channel.id, receives_bumps=True,
                )
                if created:
                    await conn.execute(
                        "UPDATE bump_guild_config SET channel_auto_created = TRUE "
                        "WHERE guild_id = $1 AND COALESCE(clone_id, -1) = COALESCE($2, -1)",
                        guild.id, clone_id,
                    )
        return "created" if created else "reused"

    async def _cooldown_seconds(self) -> int:
        """Owner-editable via /bumpadmin cooldown — falls back to
        DEFAULT_COOLDOWN_SECONDS if never set."""
        raw = await db.get_config("bump_cooldown_seconds")
        if raw is None:
            return DEFAULT_COOLDOWN_SECONDS
        try:
            return max(60, int(raw))  # 60s floor so a typo can't create a spam loop
        except (TypeError, ValueError):
            return DEFAULT_COOLDOWN_SECONDS

    async def on_guild_remove(self, guild: discord.Guild):
        # Belt-and-suspenders alongside the live-membership filter in
        # _do_bump: once the bot is kicked/removed, drop its bump config
        # outright so a stale row can never resurface as a target even if
        # the membership filter is ever bypassed by a direct DB read.
        clone_id = _clone_id_of(self.bot)
        try:
            await db.bump_clear_guild_config(guild.id, clone_id)
        except Exception:
            logger.exception("[bump] failed to clear config for departed guild %s", guild.id)

    # --- /bumpadmin (bot owner only) --------------------------------

    bumpadmin = app_commands.Group(name="bumpadmin", description="Bot-owner controls for the bump network")

    @bumpadmin.command(name="cooldown", description="View or set the global bump cooldown (owner only)")
    @app_commands.describe(minutes="New cooldown in minutes. Leave blank to just view the current value.")
    async def bumpadmin_cooldown(self, interaction: discord.Interaction, minutes: int = None):
        await interaction.response.defer(ephemeral=True)
        if not _require_bot_owner(interaction.user.id):
            await interaction.followup.send("This is owner-only.", ephemeral=True)
            return
        if minutes is None:
            current = await self._cooldown_seconds()
            await interaction.followup.send(f"Current bump cooldown: **{current // 60} min**.", ephemeral=True)
            return
        if minutes < 1:
            await interaction.followup.send("Cooldown must be at least 1 minute.", ephemeral=True)
            return
        await db.update_config("bump_cooldown_seconds", minutes * 60)
        await interaction.followup.send(f"✅ Bump cooldown set to **{minutes} min**.", ephemeral=True)

    @bumpadmin.command(name="cleanup_reminders", description="One-off: delete the reminder burst sent before the fix (owner only)")
    async def bumpadmin_cleanup_reminders(self, interaction: discord.Interaction):
        """Not something this feature will need long-term — the reminder
        worker fired for every listing whose cooldown had piled up during
        the crash-loop, all in the same first tick. This walks every
        configured bump channel's recent history and deletes whatever the
        bot itself just sent that starts with the reminder's own marker
        text, so there's no need to track message ids for a one-time
        cleanup. Safe to run more than once (dupes/none-found are both
        harmless) and only ever touches the bot's own messages."""
        if not _require_bot_owner(interaction.user.id):
            await interaction.response.send_message("This is owner-only.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)

        clone_id = _clone_id_of(self.bot)
        rows = await db.bump_list_configured_guilds(clone_id)
        deleted = 0
        checked_channels = 0
        for row in rows:
            guild = self.bot.get_guild(row["guild_id"])
            if guild is None:
                continue
            channel = guild.get_channel(int(row["bump_channel_id"]))
            if channel is None:
                continue
            checked_channels += 1
            try:
                def _is_reminder(m: discord.Message) -> bool:
                    return m.author.id == self.bot.user.id and m.content.startswith("⏱️ Bump cooldown's reset for")
                deleted_msgs = await channel.purge(limit=100, check=_is_reminder)
                deleted += len(deleted_msgs)
            except (discord.Forbidden, discord.HTTPException):
                continue

        await interaction.followup.send(
            f"🧹 Deleted **{deleted}** reminder message(s) across **{checked_channels}** channel(s).",
            ephemeral=True,
        )

    @bumpadmin.command(name="list", description="List servers that have set up bump (owner only)")
    async def bumpadmin_list(self, interaction: discord.Interaction):
        if not _require_bot_owner(interaction.user.id):
            await interaction.response.send_message("This is owner-only.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        clone_id = _clone_id_of(self.bot)
        rows = await db.bump_list_configured_guilds(clone_id)
        if not rows:
            await interaction.followup.send("No servers have set up bump yet.", ephemeral=True)
            return

        lines = []
        for row in rows:
            guild = self.bot.get_guild(row["guild_id"])
            name = guild.name if guild else f"Unknown guild ({row['guild_id']})"
            member_count = guild.member_count if guild else "?"
            receiving = "✅" if row.get("receives_bumps") else "🚫"
            premium = " · 💎 premium" if row.get("is_premium") else ""
            channel = f"<#{row['bump_channel_id']}>" if guild else f"channel {row['bump_channel_id']}"
            lines.append(f"{receiving} **{name}** ({member_count} members) — {channel}{premium}")

        embed = discord.Embed(
            title=f"📡 Bump-configured servers ({len(rows)})",
            description="\n".join(lines)[:4000],
            color=discord.Color.blurple(),
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

        # Also DM the owner directly, same "so you don't have to go dig
        # for it" convenience as /viewfeedback — best-effort, doesn't
        # fail the command if DMs are closed.
        try:
            await interaction.user.send(embed=embed)
        except discord.Forbidden:
            pass

    @bumpadmin.command(name="review", description="Review bot listings pending approval (owner only)")
    async def bumpadmin_review(self, interaction: discord.Interaction):
        if not _require_bot_owner(interaction.user.id):
            await interaction.response.send_message("This is owner-only.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        clone_id = _clone_id_of(self.bot)
        pending = await db.bump_list_pending_listings(clone_id)
        if not pending:
            await interaction.followup.send("Nothing waiting for review.", ephemeral=True)
            return

        from discord_bot.cogs._views_bump import BumpReviewView
        for listing in pending[:10]:
            guild = self.bot.get_guild(listing["guild_id"])
            embed = discord.Embed(
                title=f"🤖 {listing['name']}",
                description=listing.get("description") or "*no description*",
                color=discord.Color.gold(),
            )
            embed.add_field(name="Submitted by", value=f"<@{listing['verified_owner_id']}> (OAuth-verified)", inline=False)
            embed.add_field(name="Owning server", value=guild.name if guild else f"Unknown ({listing['guild_id']})", inline=True)
            embed.add_field(name="Application ID", value=str(listing["application_id"]), inline=True)
            embed.add_field(name="Invite", value=listing["invite_url"], inline=False)
            await interaction.followup.send(embed=embed, view=BumpReviewView(listing["id"]), ephemeral=True)
        if len(pending) > 10:
            await interaction.followup.send(f"…and {len(pending) - 10} more. Run this again after clearing some.", ephemeral=True)

    bump = app_commands.Group(name="bump", description="Bump your server or a bot to the network")

    @bump.command(name="now", description="Bump this server to other opted-in servers")
    async def bump_now(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        clone_id = _clone_id_of(interaction.client)
        config = await db.bump_get_guild_config(interaction.guild_id, clone_id)
        if not config or not config.get("bump_channel_id"):
            await interaction.followup.send("Run `/bumpsetup` first to pick a bump channel.", ephemeral=True)
            return
        if not config.get("receives_bumps", True):
            await interaction.followup.send(
                "Receiving is required to bump — your server's bump channel accepts other servers' and bots' "
                "bumps as a condition of sending your own. Run `/bumpsetup` again to re-confirm.",
                ephemeral=True,
            )
            return

        listing = await db.bump_get_listing(interaction.guild_id, clone_id, "server")
        if not listing:
            await interaction.followup.send(
                "No listing found — run `/bumpsetup` again to create one (it'll suggest a description and tags for you).",
                ephemeral=True,
            )
            return

        await self._do_bump(interaction, config, listing, clone_id)

    @bump.command(name="edit", description="Edit this server's bump listing, including the support server/channel link")
    async def bump_edit(self, interaction: discord.Interaction):
        # No defer here — send_modal must be the FIRST response to an
        # interaction; Discord rejects a modal opened after a defer.
        # Deferring, then calling send_modal below, used to raise
        # discord.InteractionResponded and silently kill the entire /bump
        # edit flow (including the support-link field the command's own
        # description advertises) every single time.
        if not _require_manage_guild(interaction):
            await interaction.response.send_message("You need **Manage Server** to run this.", ephemeral=True)
            return
        clone_id = _clone_id_of(interaction.client)
        listing = await db.bump_get_listing(interaction.guild_id, clone_id, "server")
        if not listing:
            await interaction.response.send_message("Run `/bumpsetup` first — there's no listing to edit yet.", ephemeral=True)
            return
        await interaction.response.send_modal(
            BumpEditModal(
                self, "server", listing.get("name") or interaction.guild.name,
                listing.get("description") or "", listing.get("tags") or [],
                listing.get("invite_url") or "", listing.get("perks") or [],
                listing.get("support_url") or "", listing_id=listing["id"],
            )
        )

    async def _post_bump_prompt(self, guild_id: int, config: dict, listing: dict, bumped_by, cooldown_seconds: int):
        """Posts the standalone 🔁 Bump message in the bumping server's own
        bump channel — after EVERY successful bump, whichever path triggered
        it (/bump now, the setup flow, or the button itself). Any earlier
        prompt for this listing is deleted first so exactly one live button
        sits in the channel. Best-effort: a failure here never affects the bump."""
        try:
            channel = self.bot.get_channel(int(config["bump_channel_id"]))
            if channel is None:
                return
            me = channel.guild.me
            perms = channel.permissions_for(me)
            if not (perms.send_messages and perms.embed_links):
                return
            button_id = f"bump:prompt:{listing['id']}"
            if perms.read_message_history:
                try:
                    async for old in channel.history(limit=30):
                        if old.author.id != self.bot.user.id:
                            continue
                        ids = [getattr(c, "custom_id", None) for row in old.components
                               for c in getattr(row, "children", [])]
                        if button_id in ids:
                            try:
                                await old.delete()
                            except discord.HTTPException:
                                pass
                except discord.HTTPException:
                    pass
            next_ts = int(time.time()) + cooldown_seconds
            name = listing.get("name") or channel.guild.name
            view = discord.ui.View(timeout=None)
            view.add_item(DynamicBumpPromptButton(listing["id"]))
            await channel.send(
                f"✅ **{name}** was bumped by {bumped_by.mention}!\n"
                f"Anyone can bump it again <t:{next_ts}:R> — tap the button below once the timer's up.",
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception:
            logger.exception("[bump] couldn't post bump prompt for listing %s", listing.get("id"))

    async def _do_bump(self, interaction: discord.Interaction, config: dict, listing: dict, clone_id,
                        owner_guild_id: int | None = None):
        # No defer here — every caller (bump_now, _bump_selected,
        # DynamicBumpAgainButton) already deferred before calling this. A
        # second defer() on the same interaction raises
        # discord.InteractionResponded, and since it used to run as this
        # function's very first statement — outside the try block below —
        # nothing caught it: /bump now died right after loading the
        # config/listing, before bump_record or bump_enqueue ever ran, so
        # no bump was ever actually recorded or posted.
        #
        # owner_guild_id: the listing's own guild, used to exclude it from
        # its own target list. Defaults to interaction.guild_id (true for
        # the /bump now and select-menu paths, always run inside the
        # listing's own server), but the 🔁 Bump again button on a posted
        # ad card can be clicked from a copy of that card sitting in a
        # totally different server, so that caller passes the listing's
        # real owning guild explicitly rather than letting this default
        # silently exclude the wrong guild.
        owner_guild_id = interaction.guild_id if owner_guild_id is None else owner_guild_id
        try:
            # A server listing with no invite link goes out with no Join
            # button. Reuse the server's own vanity/permanent invite if it has
            # one; otherwise hold the bump and tell them why + how to fix it.
            if listing.get("listing_type", "server") == "server" and not has_link(listing):
                owner_guild = self.bot.get_guild(owner_guild_id)
                if owner_guild is not None:
                    url = await find_existing_invite(owner_guild)
                    if url:
                        await set_listing_invite(listing["id"], url)
                        listing = {**listing, "invite_url": url}
                    else:
                        embed, view = build_flag(owner_guild, listing["id"], listing.get("name"), config.get("bump_channel_id"))
                        await interaction.followup.send(
                            content="⛔ I've held this bump — your listing has no server link, so the ad would go out with no Join button.",
                            embed=embed, view=view, ephemeral=True,
                        )
                        return
            cooldown_seconds = await self._cooldown_seconds()
            can_bump, remaining = await db.bump_check_cooldown(listing["id"], cooldown_seconds)
            if not can_bump:
                minutes = remaining // 60
                await interaction.followup.send(
                    f"⏳ On cooldown — try again in {minutes}m {remaining % 60}s.", ephemeral=True
                )
                return

            from config import BUMP_SHARED_NETWORK
            candidates = await db.bump_find_targets(
                exclude_guild_id=owner_guild_id, clone_id=clone_id,
                language=config.get("language", "any"), include_nsfw=bool(config.get("nsfw_opt_in")),
                shared=BUMP_SHARED_NETWORK,
            )
            if BUMP_SHARED_NETWORK:
                # Shared network: each target is queued under the clone that is
                # actually in that server, and THAT bot's worker delivers it
                # (and drops it if it has since left) — so this bot doesn't need
                # to be in the target guild.
                targets = candidates
            else:
                # Only bump into servers the bot is CURRENTLY in — a config row can
                # go stale if the bot was kicked and on_guild_remove hasn't caught
                # it yet (or for rows written before that listener existed), and we
                # never want to post into a server the bot no longer belongs to.
                targets = [t for t in candidates if self.bot.get_guild(t["guild_id"]) is not None]

            streak = await db.bump_record(listing["id"], STREAK_WINDOW_SECONDS)
            queued = await db.bump_enqueue(listing["id"], clone_id, targets, DRIP_SECONDS) if targets else 0

            # Re-fetch: bump_record just incremented total_bumps/streak in the
            # DB, and the listing dict we're holding predates that write.
            refreshed = await db.bump_get_listing(owner_guild_id, clone_id, listing["listing_type"], listing["id"]) or listing
            server_icon_url = None
            if refreshed["listing_type"] == "bot":
                online, members = None, None
                bot_info = await _live_bot_info(interaction.client, refreshed.get("invite_url") or "")
            else:
                online, members, server_icon_url = await _live_counts(interaction.client, refreshed)
                bot_info = None

            await interaction.followup.send(
                embed=_bump_embed(refreshed, streak, online, members, bot_info, server_icon_url),
                view=_bump_post_view(refreshed["id"], refreshed.get("invite_url"), refreshed.get("support_url")),
                content=(
                    f"✅ Added to the queue — will reach **{queued}** server{'s' if queued != 1 else ''} "
                    f"over the next ~{(queued * DRIP_SECONDS) // 60 or 1} min."
                    if queued else "✅ Bumped — no other opted-in servers match your filters yet."
                ),
            )
            await self._post_bump_prompt(owner_guild_id, config, refreshed, interaction.user, cooldown_seconds)
        except Exception:
            logger.exception("_do_bump failed for listing %s", listing.get("id"))
            # interaction.response was already used (deferred) by the
            # caller before _do_bump ever ran, so followup.send is always
            # correct here — the old is_done()/else split had identical
            # code in both arms and did nothing.
            try:
                await interaction.followup.send("Something went wrong sending that bump — check the bot logs.", ephemeral=True)
            except discord.HTTPException:
                pass

    async def _recover_target_channel(self, row: dict, clone_id):
        """Returns a postable bump channel for this queue row's target guild,
        repairing it if needed; None if it can't be recovered.

        1. If the configured channel still exists (just uncached), use it —
           granting this bot posting access if a different bot made it.
        2. If it was deleted, re-link an existing #bump or recreate one (same
           locked, never-duplicate path as the startup auto-restore), save the
           new channel on that server's config, and — only when a brand-new
           channel was made — post a short "here's what this is" note so the
           server knows what happened and how to configure it."""
        try:
            guild = self.bot.get_guild(row["target_guild_id"])
            if guild is None:
                return None
            from discord_bot.cogs.bump_setup import ensure_bot_can_post
            try:
                found = await guild.fetch_channel(row["target_channel_id"])
                if isinstance(found, discord.TextChannel) and await ensure_bot_can_post(found):
                    return found
                return None
            except discord.NotFound:
                pass  # deleted — fall through to restore
            except discord.HTTPException:
                return None
            config = await db.bump_get_guild_config(guild.id, clone_id) or {}
            result = await self._restore_one_guild(guild, clone_id, config.get("configured_by"))
            if result not in ("created", "reused"):
                return None
            fresh = await db.bump_get_guild_config(guild.id, clone_id) or {}
            new_id = fresh.get("bump_channel_id")
            channel = self.bot.get_channel(new_id) if new_id else None
            if channel is None and new_id:
                try:
                    channel = await guild.fetch_channel(new_id)
                except discord.HTTPException:
                    return None
            if channel is not None and result == "created":
                try:
                    await channel.send(
                        "📣 **Bump channel restored.** The previous bump channel was missing, so I recreated it. "
                        "Other servers' and bots' ads land here — that's how the free partnership network works. "
                        "Run `/bumpsetup` to change settings, or `/bump now` to send your own server out."
                    )
                except discord.HTTPException:
                    pass
            return channel
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[bump] couldn't recover bump channel for guild %s", row.get("target_guild_id"))
            return None

    # --- worker: drains bump_queue -----------------------------------

    @tasks.loop(seconds=WORKER_TICK_SECONDS)
    async def bump_worker(self):
        if not self._worker_gate.should_run():
            return
        clone_id = _clone_id_of(self.bot)
        try:
            due = await db.bump_get_due_queue(clone_id, limit=MAX_SENDS_PER_TICK)
        except Exception:
            logger.exception("[bump] failed to fetch due queue")
            return
        self._worker_gate.record(found_something=bool(due))

        for row in due:
            if self.bot.get_guild(row["target_guild_id"]) is None:
                # Bot left this guild after the bump was queued — skip, don't send.
                await db.bump_mark_sent(row["id"])
                continue
            channel = self.bot.get_channel(row["target_channel_id"])
            if channel is None:
                # Not cached — either just uncached, or the server's bump channel
                # was deleted. Self-heal: re-link/recreate #bump for that server so
                # it still receives this ad instead of silently losing it.
                channel = await self._recover_target_channel(row, clone_id)
            if channel is None:
                await db.bump_mark_sent(row["id"])  # genuinely unrecoverable — drop it, don't retry forever
                continue
            try:
                server_icon_url = None
                if row.get("listing_type") == "bot":
                    online, members = None, None
                    bot_info = await _live_bot_info(self.bot, row.get("invite_url") or "")
                else:
                    online, members, server_icon_url = await _live_counts(self.bot, row)
                    bot_info = None
                embed = _bump_embed(row, row.get("streak_count") or 1, online, members, bot_info, server_icon_url)
                await channel.send(embed=embed, view=_bump_post_view(row["listing_id"], row.get("invite_url"), row.get("support_url")))
            except discord.Forbidden:
                logger.info("[bump] missing perms in channel %s, dropping", row["target_channel_id"])
            except Exception:
                logger.exception("[bump] send failed for queue row %s", row["id"])
            await db.bump_mark_sent(row["id"])

    @bump_worker.before_loop
    async def _before_worker(self):
        await self.bot.wait_until_ready()

    # --- reminder worker: nudges staff once cooldown resets -----------

    @tasks.loop(seconds=REMINDER_TICK_SECONDS)
    async def bump_reminder_worker(self):
        if not self._reminder_gate.should_run():
            return
        clone_id = _clone_id_of(self.bot)
        cooldown_seconds = await self._cooldown_seconds()
        try:
            due = await db.bump_get_listings_needing_reminder(clone_id, cooldown_seconds, limit=MAX_REMINDERS_PER_TICK)
        except Exception:
            logger.exception("[bump] failed to fetch listings needing a reminder")
            return
        self._reminder_gate.record(found_something=bool(due))

        for listing in due:
            guild = self.bot.get_guild(listing["guild_id"])
            if guild is None:
                # Bot left this guild — nothing to remind, and nothing to
                # retry either; stamp it so it doesn't keep coming back
                # due every tick for as long as last_bump_at sits still.
                await db.bump_mark_reminder_sent(listing["id"])
                continue
            try:
                config = await db.bump_get_guild_config(listing["guild_id"], listing.get("clone_id"))
            except Exception:
                logger.exception("[bump] failed to load guild config for reminder, listing %s", listing["id"])
                continue
            channel_id = config.get("bump_channel_id") if config else None
            if not channel_id:
                # Channel was never set (or got cleared) — same reasoning
                # as the guild-gone case, stamp and move on rather than
                # re-querying this row every tick forever.
                await db.bump_mark_reminder_sent(listing["id"])
                continue
            channel = guild.get_channel(int(channel_id))
            if channel is None:
                await db.bump_mark_reminder_sent(listing["id"])
                continue

            label = listing.get("name") or ("this bot" if listing.get("listing_type") == "bot" else "this server")
            try:
                await channel.send(
                    f"⏱️ Bump cooldown's reset for **{label}** — run `/bump now` to send it out again."
                )
            except discord.Forbidden:
                logger.info("[bump] missing perms in channel %s, dropping reminder", channel_id)
            except Exception:
                logger.exception("[bump] reminder send failed for listing %s", listing["id"])
            await db.bump_mark_reminder_sent(listing["id"])

    @bump_reminder_worker.before_loop
    async def _before_reminder_worker(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(BumpCog(bot))
