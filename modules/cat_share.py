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


def profile_embed(display_name: str, profile: dict, badges) -> discord.Embed:
    """Public card. Only collection size, rarest cat, pinned (still owned) cats and badge names."""
    e = discord.Embed(title=f"\U0001F4D6 {display_name}'s cat profile", color=GOLD)
    e.description = profile.get("bio") or "No bio yet \u2014 their cats are waiting for a story."
    e.add_field(name="Collection", value=f"\U0001F43E **{profile['collection_size']}** cats", inline=True)
    rarest = profile.get("rarest")
    e.add_field(name="Rarest", value=f"{rarest.name} \u00b7 {rarest.rarity.title()}" if rarest else "None yet", inline=True)
    if profile.get("pinned"):
        e.add_field(name="Showcase", value="\n".join(f"\U0001F431 **{c.name}** \u00b7 {c.trait}" for c in profile["pinned"]),
                    inline=False)
    if badges:
        names = [f"{cat_quests.BADGES[s][0]} {cat_quests.BADGES[s][1]}" for s in cat_quests.BADGES if s in badges]
        if names:
            e.add_field(name="Badges", value=" \u00b7 ".join(names), inline=False)
    return e


def badges_embed(display_name: str, badges) -> Optional[discord.Embed]:
    names = [f"{cat_quests.BADGES[s][0]} **{cat_quests.BADGES[s][1]}**" for s in cat_quests.BADGES if s in badges]
    if not names:
        return None
    return discord.Embed(title=f"\U0001F3C5 {display_name}'s cat badges", description="\n".join(names), color=GOLD)


def care_text(mention: str, streak: int) -> str:
    return f"\U0001F496 {mention} is on a **{int(streak)}-day** cat care streak!"
