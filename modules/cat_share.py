"""Sharing the cat game with the channel: public profile card, badge card, care streak (play game only).

Everything here is consent based: nothing is posted unless the player taps Share or runs `/cat profile:@player`.
Cards are rebuilt from the database at the moment of sharing, so a hidden profile stays hidden and a pinned cat the
player no longer owns never shows. Wallet balances, rewards and IDs are never part of a public card.
"""
from __future__ import annotations

import re
import time
from typing import Optional

import discord

from modules import cat_adopt, cat_quests

BIO_MAX = 280
SHARE_COOLDOWN = 30          # seconds between two public shares of the same kind by the same player
GOLD = 0xF5C542
PINK = 0xFFB6C1

_last_share: dict = {}


def clean_bio(raw) -> Optional[str]:
    """Plain text, no mentions. '' for nothing, None when it holds a link or an invite."""
    text = re.sub(r"[\x00-\x1f\x7f@<>`]", "", str(raw or ""))
    text = re.sub(r"[ \t]+", " ", text).strip()
    low = text.lower()
    if "http" in low or "discord.gg" in low or "discord.com" in low or "www." in low or ".com" in low:
        return None
    return text[:BIO_MAX].strip()


def cooldown_left(user_id: int, kind: str, now: Optional[float] = None) -> float:
    t = time.monotonic() if now is None else now
    return max(0.0, SHARE_COOLDOWN - (t - _last_share.get((user_id, kind), -1e9)))


def mark_shared(user_id: int, kind: str, now: Optional[float] = None) -> None:
    if len(_last_share) > 5000:
        _last_share.clear()
    _last_share[(user_id, kind)] = time.monotonic() if now is None else now


def profile_text(display_name: str, profile: dict, badges) -> str:
    """Public card text. Only collection size, rarest cat, pinned (still owned) cats and badge names."""
    rarest = profile.get("rarest")
    lines = [f"## \U0001F4D6 {display_name}'s cat profile",
             profile.get("bio") or "No bio yet \u2014 their cats are waiting for a story.", "",
             f"**Collection:** \U0001F43E **{profile['collection_size']}** cats",
             f"**Rarest:** {rarest.name} \u00b7 {rarest.rarity.title()}" if rarest else "**Rarest:** None yet"]
    worn = [f"{v['emoji']} {v['name']}" for v in (profile.get("equipped") or {}).values() if v]
    if worn:
        lines.append("**Wearing:** " + " \u00b7 ".join(worn))
    if profile.get("pinned"):
        lines += ["", "**Showcase**"] + [f"\U0001F431 **{c.name}** \u00b7 {c.trait}" for c in profile["pinned"]]
    if badges:
        names = [f"{cat_quests.BADGES[s][0]} {cat_quests.BADGES[s][1]}" for s in cat_quests.BADGES if s in badges]
        if names:
            lines += ["", "**Badges:** " + " \u00b7 ".join(names)]
    return "\n".join(lines)


def profile_box(display_name: str, profile: dict, badges, avatar_url: Optional[str] = None,
                footer: Optional[str] = None) -> discord.ui.Container:
    """The public profile as one V2 Container (text, the player's own Discord picture beside it, optional footer).
    Callers add their own buttons below it when the card is a private screen."""
    box = discord.ui.Container(accent_colour=discord.Colour(GOLD))
    text = profile_text(display_name, profile, badges)[:3900]
    if avatar_url:
        box.add_item(discord.ui.Section(text, accessory=discord.ui.Thumbnail(avatar_url)))   # nothing private
    else:
        box.add_item(discord.ui.TextDisplay(text))
    if footer:
        box.add_item(discord.ui.TextDisplay(f"-# {footer}"[:400]))
    return box


def profile_card_box(display_name: str, footer: Optional[str] = None) -> discord.ui.Container:
    """The profile as a drawn card (modules/cat_profile_card.py). The picture carries the details; the heading keeps
    the message readable for screen readers and notifications. The picture is the attachment `cat_profile.png`."""
    box = discord.ui.Container(accent_colour=discord.Colour(GOLD))
    box.add_item(discord.ui.TextDisplay(f"## \U0001F4D6 {display_name}'s cat profile"))
    box.add_item(discord.ui.MediaGallery(discord.MediaGalleryItem("attachment://cat_profile.png",
                                                                  description=f"{display_name}'s cat profile"[:256])))
    if footer:
        box.add_item(discord.ui.TextDisplay(f"-# {footer}"[:400]))
    return box


def badges_view(display_name: str, badges) -> Optional[discord.ui.LayoutView]:
    names = [f"{cat_quests.BADGES[s][0]} **{cat_quests.BADGES[s][1]}**" for s in cat_quests.BADGES if s in badges]
    if not names:
        return None
    return layout(discord.ui.Container(
        discord.ui.TextDisplay(f"## \U0001F3C5 {display_name}'s cat badges\n" + "\n".join(names)),
        accent_colour=discord.Colour(GOLD)))


def layout(box: discord.ui.Container, timeout: Optional[float] = None) -> discord.ui.LayoutView:
    """Wraps one Container as a V2 message (a public card has nothing to tap, so no timeout)."""
    view = discord.ui.LayoutView(timeout=timeout)
    view.add_item(box)
    return view


def profile_view(display_name: str, profile: dict, badges, avatar_url: Optional[str] = None,
                 footer: Optional[str] = None) -> discord.ui.LayoutView:
    return layout(profile_box(display_name, profile, badges, avatar_url, footer))


def profile_card_view(display_name: str, footer: Optional[str] = None) -> discord.ui.LayoutView:
    return layout(profile_card_box(display_name, footer))


def care_text(mention: str, streak: int) -> str:
    return f"\U0001F496 {mention} is on a **{int(streak)}-day** cat care streak!"
