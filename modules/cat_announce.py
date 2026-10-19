# path: modules/cat_announce.py

"""
Public announcements for the cat game, so everyone in the channel can see what is going on (play game only).

What the people involved see privately stays private (coins left, rewards, errors). The public line only says who did
what to which cat. Mentions are shown but never ping anybody. Posting is best effort and can never break the action
it announces: if the bot can't post, the action still went through.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

import discord

from modules import cat_rank as cr

logger = logging.getLogger(__name__)

PINK = 0xFFB6C1
GOLD = 0xF5C542


def adopt_text(mention: str, name: str, cat_id: int, rarity_badge: str, trait: str, rarity: str) -> str:
    head = (f"\U0001F451 **LEGENDARY!** **{name}** was adopted by {mention}!" if rarity == "legendary"
            else f"\U0001F496 **{name}** was adopted by {mention}!")
    return f"{head}\n{rarity_badge} \u00b7 {trait} \u00b7 Cat #{cat_id}"


def rank_up_text(mention: str, name: str, cat_id: int, rank: int) -> str:
    if cr.clamp(rank) >= cr.MAX_RANK:
        return (f"\U0001F31F {mention} took **{name}** (cat #{cat_id}) all the way to the top rank: "
                f"**{cr.badge(rank)}**!")
    return f"\u2B50 {mention} ranked **{name}** (cat #{cat_id}) up to **{cr.badge(rank)}**!"


def sale_text(buyer: str, seller: str, name: str, cat_id: int, rank: int, price: int) -> str:
    rank_part = f" ({cr.badge(rank)})" if cr.clamp(rank) else ""
    return f"\U0001F3F7\uFE0F {buyer} adopted **{name}**{rank_part} (cat #{cat_id}) from {seller} for **{price:,} coins**!"


# ── Milestones: rare moments only, never every action ────────────────────────
MILESTONE_BADGES = ("full-set", "legend")      # Full Set, Legend Keeper
MILESTONE_STREAKS = (7, 30)
USER_LIMIT, USER_WINDOW = 3, 3600              # at most 3 milestone posts per player per hour ...
CHANNEL_GAP = 5.0                              # ... and one per channel every 5 seconds
_user_posts: dict = {}
_channel_last: dict = {}


def badge_text(mention: str, slug: str) -> str:
    from modules import cat_quests
    emoji, name, how, _ = cat_quests.BADGES[slug]
    return f"{emoji} {mention} earned the **{name}** badge: {how.lower()}!"


def streak_text(mention: str, streak: int) -> str:
    return f"\U0001F525 {mention} is on a **{int(streak)}-day** cat care streak!"


def quests_text(mention: str) -> str:
    return f"\U0001F3AF {mention} finished **every daily cat quest** today!"


def milestone_ok(user_id: int, channel_id, now: Optional[float] = None) -> bool:
    """Rate limit for automatic milestone posts, per player and per channel. Does not record anything."""
    t = time.monotonic() if now is None else now
    recent = [x for x in _user_posts.get(user_id, ()) if t - x < USER_WINDOW]
    if len(recent) >= USER_LIMIT:
        return False
    return t - _channel_last.get(channel_id, -1e9) >= CHANNEL_GAP


def _mark_milestone(user_id: int, channel_id, now: Optional[float] = None) -> None:
    t = time.monotonic() if now is None else now
    if len(_user_posts) > 5000:
        _user_posts.clear()
        _channel_last.clear()
    _user_posts[user_id] = [x for x in _user_posts.get(user_id, ()) if t - x < USER_WINDOW] + [t]
    _channel_last[channel_id] = t


# ── Per-server setting (/catsetup): all / milestones only / off ──────────────
_mode_cache: dict = {}
MODE_TTL = 60.0


def remember_mode(guild_id: int, clone_id, mode: str) -> None:
    _mode_cache[(guild_id, clone_id)] = (mode, time.monotonic() + MODE_TTL)


async def mode_for(guild_id: int, clone_id) -> str:
    """The server's choice. Any problem reading it means 'all' (today's behaviour). Never raises."""
    hit = _mode_cache.get((guild_id, clone_id))
    if hit and hit[1] > time.monotonic():
        return hit[0]
    try:
        from modules import cat_setup
        cfg = await asyncio.wait_for(cat_setup.get_config(guild_id, clone_id), 2.0)
        mode = cat_setup.clamp_announce(cfg.announce_mode)
    except Exception:
        return "all"
    remember_mode(guild_id, clone_id, mode)
    return mode


def mode_allows(mode: str, kind: str) -> bool:
    """all: everything. milestones: big moments plus cards a player chose to share. off: nothing."""
    return mode == "all" or (mode == "milestones" and kind in ("milestone", "share"))


def card_view(text: str, gold: bool = False, image: Optional[str] = None, footer: Optional[str] = None) -> discord.ui.LayoutView:
    """A public one-card Components V2 message: the text, an optional picture (a URL, or `attachment://name`) and a
    small footer. Nothing to tap, so it needs no owner lock. Mentions in the text never ping (see `announce_card`)."""
    view = discord.ui.LayoutView(timeout=None)
    box = discord.ui.Container(accent_colour=GOLD if gold else PINK)
    box.add_item(discord.ui.TextDisplay(text[:3900]))
    if image:
        box.add_item(discord.ui.MediaGallery(discord.MediaGalleryItem(image)))
    if footer:
        box.add_item(discord.ui.TextDisplay(f"-# {footer}"[:400]))
    view.add_item(box)
    return view


async def announce(interaction: discord.Interaction, text: str, gold: bool = False, kind: str = "event") -> bool:
    """Post `text` where everyone can see it: as a public follow-up to the button that was pressed (no channel
    permission needed), else straight into the channel. Never raises. Does nothing in DMs, or when the server's
    setting turns this kind of post off. kind="milestone" posts are also rate limited."""
    return await announce_card(interaction, card_view(text, gold), kind)


def _pic(png: Optional[bytes], name: str) -> dict:
    """A fresh discord.File for each send (a File can only be sent once), or nothing."""
    if not png:
        return {}
    import io
    return {"file": discord.File(io.BytesIO(png), filename=name)}


def _body(card) -> dict:
    """A V2 card goes out as `view=`, a classic embed as `embed=` (a V2 message can't carry an embed)."""
    return {"embed": card} if isinstance(card, discord.Embed) else {"view": card}


async def announce_card(interaction: discord.Interaction, card, kind: str = "event",
                        png: Optional[bytes] = None, png_name: str = "cat_profile.png") -> bool:
    """Same as `announce`, for a ready-made card (profile, badges): a Components V2 `LayoutView` (or, for old callers,
    a classic `discord.Embed`). A `png` is attached and must be named by the card as `attachment://<png_name>`.
    Mentions never ping. Never raises."""
    guild = getattr(interaction, "guild", None)
    if guild is None:
        return False
    gid = getattr(guild, "id", None)
    if gid is not None and not mode_allows(await mode_for(gid, getattr(getattr(interaction, "client", None), "clone_id", None)), kind):
        return False
    uid = getattr(getattr(interaction, "user", None), "id", 0)
    chan = getattr(getattr(interaction, "channel", None), "id", None)
    if kind == "milestone" and not milestone_ok(uid, chan):
        return False
    quiet = discord.AllowedMentions.none()
    posted = False
    try:
        await interaction.followup.send(ephemeral=False, allowed_mentions=quiet, **_body(card), **_pic(png, png_name))
        posted = True
    except Exception:
        logger.debug("[cat-announce] follow-up failed, trying the channel", exc_info=True)
    if not posted:
        try:
            channel = getattr(interaction, "channel", None)
            if channel is not None:
                await channel.send(allowed_mentions=quiet, **_body(card), **_pic(png, png_name))
                posted = True
        except Exception:
            logger.debug("[cat-announce] couldn't post in the channel either", exc_info=True)
    if posted and kind == "milestone":
        _mark_milestone(uid, chan)
    return posted


# Older name, kept so existing callers keep working; takes a V2 card or a classic embed.
announce_embed = announce_card


async def announce_badges(interaction: discord.Interaction, slugs) -> None:
    """Public line for the rare badges only (Full Set, Legend Keeper). The small ones stay private."""
    for slug in slugs or ():
        if slug in MILESTONE_BADGES:
            await announce(interaction, badge_text(interaction.user.mention, slug), gold=True, kind="milestone")
