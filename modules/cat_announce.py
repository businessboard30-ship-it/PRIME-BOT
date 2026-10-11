# path: modules/cat_announce.py

"""
Public announcements for the cat game, so everyone in the channel can see what is going on (play game only).

What the people involved see privately stays private (coins left, rewards, errors). The public line only says who did
what to which cat. Mentions are shown but never ping anybody. Posting is best effort and can never break the action
it announces: if the bot can't post, the action still went through.
"""

from __future__ import annotations

import logging
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


async def announce(interaction: discord.Interaction, text: str, gold: bool = False) -> bool:
    """Post `text` where everyone can see it: as a public follow-up to the button that was pressed (no channel
    permission needed), else straight into the channel. Never raises. Does nothing in DMs."""
    embed = discord.Embed(description=text, color=GOLD if gold else PINK)
    return await announce_embed(interaction, embed)


async def announce_embed(interaction: discord.Interaction, embed: discord.Embed) -> bool:
    """Same as `announce`, for a ready-made card (profile, badges). Mentions never ping. Never raises."""
    if getattr(interaction, "guild", None) is None:
        return False
    quiet = discord.AllowedMentions.none()
    try:
        await interaction.followup.send(embed=embed, ephemeral=False, allowed_mentions=quiet)
        return True
    except Exception:
        logger.debug("[cat-announce] follow-up failed, trying the channel", exc_info=True)
    try:
        channel = getattr(interaction, "channel", None)
        if channel is not None:
            await channel.send(embed=embed, allowed_mentions=quiet)
            return True
    except Exception:
        logger.debug("[cat-announce] couldn't post in the channel either", exc_info=True)
    return False
