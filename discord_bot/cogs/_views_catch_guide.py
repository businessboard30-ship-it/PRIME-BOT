"""Guide screen: a short, static how-to-play. Responds first (rule B.6), then the ``view`` gate.

The text lives in ``locales/en.json`` (``catch.guide.*``). It only describes features that
are built; keep it in step when a new one ships.
"""

from __future__ import annotations

import discord

from modules import catch_emoji
from modules.catch_card import send_kwargs
from modules.catch_guide_card import guide_art
from modules.catch_gate import check_player_allowed
from modules.catch_i18n import text
from modules.catch_notice import notice_for
from modules.catch_theme import state_color

GUIDE_SECTIONS = ("catching", "items", "rarity", "coins", "collection")


def _marks() -> dict[str, str]:
    """Emoji the guide text refers to, read from the shared table so a restyle shows up here too."""
    return {**catch_emoji.RARITY, "shiny": catch_emoji.FLAG["shiny"]}


def guide_embed() -> discord.Embed:
    embed = discord.Embed(title=text("guide.title"), description=text("guide.intro"), colour=state_color("info"))
    for key in GUIDE_SECTIONS:
        embed.add_field(name=text(f"guide.{key}.name"), value=text(f"guide.{key}.body", **_marks()), inline=False)
    return embed


async def open_guide(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)
    clone_id = getattr(interaction.client, "clone_id", None)
    gate = await check_player_allowed(interaction.user.id, interaction.guild_id, "view", clone_id)
    if not gate.allowed:
        await interaction.followup.send(**notice_for("catch.unavailable", reason=gate.reason or "disabled"), ephemeral=True)
        return
    embed = guide_embed()
    file = await guide_art(embed)  # None (plain embed, unchanged) if the card cannot be drawn
    await interaction.followup.send(embed=embed, ephemeral=True, **send_kwargs(file))


__all__ = ["GUIDE_SECTIONS", "guide_embed", "open_guide"]
