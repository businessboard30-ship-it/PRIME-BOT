"""Rules screen: this server's catch settings, read-only, as the owner configured them.

Responds first (rule B.6), refuses outside a server, checks the ``view`` gate, then makes one
scoped read of the server setup. Channel lists are capped so the embed always fits.
"""

from __future__ import annotations

import logging

import discord

from modules.catch_gate import check_player_allowed
from modules.catch_i18n import text
from modules.catch_notice import notice_for
from modules.catch_setup import CatchSetup, load_setup
from modules.catch_theme import state_color

logger = logging.getLogger(__name__)

MAX_CHANNELS_SHOWN = 15


def _channels(ids: tuple[int, ...]) -> str:
    if not ids:
        return text("rules.none")
    shown = " ".join(f"<#{channel_id}>" for channel_id in ids[:MAX_CHANNELS_SHOWN])
    extra = len(ids) - MAX_CHANNELS_SHOWN
    return shown + (" " + text("rules.more", count=extra) if extra > 0 else "")


def _duration(seconds: int) -> str:
    minutes, rest = divmod(max(0, seconds), 60)
    if minutes and not rest:
        return text("rules.minutes", count=minutes)
    return text("rules.seconds", count=seconds)


def rules_embed(setup: CatchSetup) -> discord.Embed:
    embed = discord.Embed(title=text("rules.title"), description=text("rules.description"), colour=state_color("info"))
    embed.add_field(name=text("rules.state"), value=text("rules.on") if setup.enabled else text("rules.off"), inline=True)
    embed.add_field(name=text("rules.speed"), value=text("rules.speed_value", preset=setup.speed_preset.title(), messages=setup.spawn_every_n_messages, wait=_duration(setup.min_seconds_between_spawns)), inline=True)
    embed.add_field(name=text("rules.despawn"), value=text("rules.despawn_value", time=_duration(setup.despawn_seconds)), inline=True)
    embed.add_field(name=text("rules.spawn_channels"), value=_channels(setup.spawn_channel_ids), inline=False)
    embed.add_field(name=text("rules.encounter_channels"), value=_channels(setup.encounter_channel_ids), inline=False)
    if setup.rare_ping_role_id:
        embed.add_field(name=text("rules.rare_ping"), value=f"<@&{setup.rare_ping_role_id}>", inline=True)
    if setup.announce_channel_id:
        embed.add_field(name=text("rules.announce"), value=f"<#{setup.announce_channel_id}>", inline=True)
    return embed


async def open_rules(interaction: discord.Interaction) -> None:
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
        setup = await load_setup(interaction.guild_id, clone_id)
    except Exception:
        logger.exception("Catch rules load failed guild=%s", interaction.guild_id)
        await interaction.followup.send(**notice_for("rules.error"), ephemeral=True)
        return
    await interaction.followup.send(embed=rules_embed(setup), ephemeral=True)


__all__ = ["MAX_CHANNELS_SHOWN", "open_rules", "rules_embed"]
