"""Guide screen: a short, static how-to-play. Responds first (rule B.6), then the ``view`` gate.

The text lives in ``locales/en.json`` (``catch.guide.*``). It only describes features that
are built; keep it in step when a new one ships.
"""

from __future__ import annotations

import discord

from modules.catch_gate import check_player_allowed
from modules.catch_i18n import text
from modules.catch_theme import state_color

GUIDE_SECTIONS = ("catching", "items", "rarity", "coins", "collection")


def guide_embed() -> discord.Embed:
    embed = discord.Embed(title=text("guide.title"), description=text("guide.intro"), colour=state_color("info"))
    for key in GUIDE_SECTIONS:
        embed.add_field(name=text(f"guide.{key}.name"), value=text(f"guide.{key}.body"), inline=False)
    return embed


async def open_guide(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)
    clone_id = getattr(interaction.client, "clone_id", None)
    gate = await check_player_allowed(interaction.user.id, interaction.guild_id, "view", clone_id)
    if not gate.allowed:
        await interaction.followup.send(text("catch.unavailable", reason=gate.reason or "disabled"), ephemeral=True)
        return
    await interaction.followup.send(embed=guide_embed(), ephemeral=True)


__all__ = ["GUIDE_SECTIONS", "guide_embed", "open_guide"]
