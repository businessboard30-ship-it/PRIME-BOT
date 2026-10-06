"""Status screen: the player's own catch progress, read-only.

Responds first (rule B.6), then checks the ``view`` gate, then reads. Ephemeral, so it
only ever shows the person who pressed the button their own numbers.
"""

from __future__ import annotations

import logging

import discord

from modules.catch_card import send_kwargs
from modules.catch_gate import check_player_allowed
from modules.catch_i18n import text
from modules.catch_status import PlayerStatus, load_status
from modules.catch_status_card import status_art
from modules.catch_theme import state_color

logger = logging.getLogger(__name__)


def status_embed(status: PlayerStatus) -> discord.Embed:
    embed = discord.Embed(title=text("status.title"), colour=state_color("info"))
    daily = (
        text("status.daily_ready") if status.daily_ready_at is None
        else text("status.daily_wait", when=f"<t:{int(status.daily_ready_at.timestamp())}:R>")
    )
    embed.add_field(name=text("status.coins"), value=f"{status.coins:,}", inline=True)
    embed.add_field(name=text("status.catches"), value=f"{status.total_catches:,}", inline=True)
    embed.add_field(name=text("status.collection"), value=text("status.collection_value", owned=status.owned, shinies=status.shinies, favourites=status.favourites), inline=True)
    embed.add_field(name=text("status.streak"), value=text("status.streak_value", current=status.catch_streak, best=status.best_streak), inline=True)
    embed.add_field(name=text("status.daily"), value=text("status.daily_value", streak=status.daily_streak, state=daily), inline=True)
    embed.add_field(name=text("status.dex"), value=text("status.dex_value", caught=status.dex_caught, seen=status.dex_seen, total=status.dex_total), inline=True)
    return embed


async def open_status(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)
    clone_id = getattr(interaction.client, "clone_id", None)
    gate = await check_player_allowed(interaction.user.id, interaction.guild_id, "view", clone_id)
    if not gate.allowed:
        await interaction.followup.send(text("catch.unavailable", reason=gate.reason or "disabled"), ephemeral=True)
        return
    try:
        status = await load_status(interaction.user.id, clone_id)
    except Exception:
        logger.exception("Catch status load failed user=%s", interaction.user.id)
        await interaction.followup.send(text("status.error"), ephemeral=True)
        return
    embed = status_embed(status)
    file = await status_art(embed, status)  # None (plain embed, unchanged) if the card cannot be drawn
    await interaction.followup.send(embed=embed, ephemeral=True, **send_kwargs(file))


__all__ = ["open_status", "status_embed"]
