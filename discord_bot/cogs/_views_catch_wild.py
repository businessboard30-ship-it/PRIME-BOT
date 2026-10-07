"""Wild zone screen: a read-only list of the live spawns in this server.

Responds first (rule B.6), then checks the ``view`` gate, then reads. Shows the rarity,
level, time left and a jump link for each live spawn; it never claims anything.
"""

from __future__ import annotations

import logging

import discord

from modules.catch_card import send_kwargs
from modules.catch_gate import check_player_allowed
from modules.catch_i18n import text
from modules.catch_notice import notice_for
from modules.catch_theme import state_color
from modules.catch_wild_card import wild_art
from modules.catch_wild import RARITY_MARK, WILD_LIST_LIMIT, jump_url, list_active_spawns

logger = logging.getLogger(__name__)


def wild_embed(guild_id: int, spawns, total: int) -> discord.Embed:
    embed = discord.Embed(title=text("wild.title"), colour=state_color("info"))
    if not spawns:
        embed.description = text("wild.empty")
        return embed
    lines = []
    for spawn in spawns:
        url = jump_url(guild_id, spawn.channel_id, spawn.message_id)
        where = f"[{text('wild.go')}]({url})" if url else ""
        shiny = "✨ " if spawn.shiny else ""
        mine = f" · {text('wild.yours')}" if spawn.personal else ""
        lines.append(
            f"{RARITY_MARK.get(spawn.rarity, '⚪')} {shiny}**{spawn.name}** · Lv {spawn.level} · "
            f"<t:{int(spawn.expires_at.timestamp())}:R>{mine} {where}".rstrip()
        )
    embed.description = text("wild.description") + "\n\n" + "\n".join(lines)
    if total > len(spawns):
        embed.set_footer(text=text("wild.more", shown=len(spawns), total=total))
    return embed


async def open_wild_zone(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)
    if interaction.guild_id is None:
        await interaction.followup.send(**notice_for("encounter.server_only"), ephemeral=True)
        return
    clone_id = getattr(interaction.client, "clone_id", None)
    gate = await check_player_allowed(interaction.user.id, interaction.guild_id, "view", clone_id)
    if not gate.allowed:
        await interaction.followup.send(**notice_for("catch.unavailable", reason=gate.reason or "disabled"), ephemeral=True)
        return
    try:
        spawns, total = await list_active_spawns(interaction.guild_id, interaction.user.id, clone_id, limit=WILD_LIST_LIMIT)
    except Exception:
        logger.exception("Catch wild zone load failed guild=%s", interaction.guild_id)
        await interaction.followup.send(**notice_for("wild.error"), ephemeral=True)
        return
    embed = wild_embed(interaction.guild_id, spawns, total)
    file = await wild_art(embed, spawns, total)  # None (plain embed, unchanged) if the card cannot be drawn
    await interaction.followup.send(embed=embed, ephemeral=True, **send_kwargs(file))


__all__ = ["open_wild_zone", "wild_embed"]
