"""Daily and Inventory screens for the catch hub. Both are ephemeral, defer first (rule B.6),
and are gated by the same "view" action as Collection and Dex."""

from __future__ import annotations

import logging

import discord

from modules.catch_game import BAIT_BONUS, BALLS
from modules.catch_gate import check_player_allowed
from modules.catch_card import send_kwargs
from modules.catch_coin_card import coin_art, daily_art
from modules.catch_i18n import text
from modules.catch_items import (
    claim_daily, ensure_starter_kit, item_name, load_inventory, load_player, sorted_inventory,
)
from modules.catch_theme import state_color

logger = logging.getLogger(__name__)


def _stack_lines(stacks: list[tuple[str, int]], bonus: dict[str, float], *, as_multiplier: bool) -> str:
    lines = []
    for key, quantity in stacks:
        detail = f"×{bonus[key]:g} catch chance" if as_multiplier else f"+{int(bonus[key] * 100)}% catch chance"
        lines.append(f"**{item_name(key)}** ×{quantity} · {detail}")
    return "\n".join(lines)


def inventory_embed(inventory: dict[str, int], player, *, starter_granted: bool = False) -> discord.Embed:
    balls, berries = sorted_inventory(inventory)
    embed = discord.Embed(title=text("inventory.title"), colour=state_color("info"))
    embed.add_field(name=text("inventory.capsules"), value=_stack_lines(balls, BALLS, as_multiplier=True) or text("inventory.none"), inline=False)
    embed.add_field(name=text("inventory.berries"), value=_stack_lines(berries, BAIT_BONUS, as_multiplier=False) or text("inventory.none"), inline=False)
    embed.add_field(name=text("inventory.coins"), value=f"{player.coins:,}", inline=True)
    embed.add_field(name=text("inventory.catches"), value=f"{player.total_catches:,}", inline=True)
    embed.add_field(name=text("inventory.daily_streak"), value=str(player.daily_streak), inline=True)
    if starter_granted:
        embed.set_footer(text=text("inventory.starter"))
    return embed


def daily_embed(result) -> discord.Embed:
    if not result.claimed:
        return discord.Embed(
            title=text("daily.wait_title"),
            description=text("daily.wait", ready_at=int(result.ready_at.timestamp()), streak=result.streak),
            colour=state_color("warning"),
        )
    reward = result.reward
    items = ", ".join(f"{item_name(k)} ×{v}" for k, v in reward.items.items())
    embed = discord.Embed(title=text("daily.title"), description=text("daily.claimed", streak=result.streak), colour=state_color("success"))
    embed.add_field(name=text("inventory.coins"), value=f"+{reward.coins:,}", inline=True)
    embed.add_field(name=text("daily.items"), value=items, inline=True)
    embed.set_footer(text=text("daily.next", ready_at=int(result.ready_at.timestamp())))
    return embed


async def _gate(interaction: discord.Interaction, clone_id) -> bool:
    gate = await check_player_allowed(interaction.user.id, interaction.guild_id, "view", clone_id)
    if not gate.allowed:
        await interaction.followup.send(text("catch.unavailable", reason=gate.reason or "disabled"), ephemeral=True)
    return gate.allowed


async def open_inventory(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)
    clone_id = getattr(interaction.client, "clone_id", None)
    if not await _gate(interaction, clone_id):
        return
    try:
        granted = await ensure_starter_kit(interaction.user.id, clone_id)
        inventory = await load_inventory(interaction.user.id, clone_id)
        player = await load_player(interaction.user.id, clone_id)
    except Exception:
        logger.exception("Catch inventory load failed user=%s", interaction.user.id)
        await interaction.followup.send(text("inventory.error"), ephemeral=True)
        return
    embed = inventory_embed(inventory, player, starter_granted=granted)
    file = await coin_art(embed, label="YOUR BAG", coins=player.coins, drop_field=text("inventory.coins"))
    await interaction.followup.send(embed=embed, ephemeral=True, **send_kwargs(file))


async def open_daily(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)
    clone_id = getattr(interaction.client, "clone_id", None)
    if not await _gate(interaction, clone_id):
        return
    try:
        await ensure_starter_kit(interaction.user.id, clone_id)
        result = await claim_daily(interaction.user.id, clone_id)
    except Exception:
        logger.exception("Catch daily claim failed user=%s", interaction.user.id)
        await interaction.followup.send(text("daily.error"), ephemeral=True)
        return
    embed = daily_embed(result)
    file = None
    if result.claimed:
        reward = result.reward
        file = await daily_art(
            embed, coins=reward.coins, streak=result.streak,
            items={item_name(k): v for k, v in reward.items.items()},
            drop_fields=(text("inventory.coins"), text("daily.items")),
        )
    await interaction.followup.send(embed=embed, ephemeral=True, **send_kwargs(file))


__all__ = ["daily_embed", "inventory_embed", "open_daily", "open_inventory"]
