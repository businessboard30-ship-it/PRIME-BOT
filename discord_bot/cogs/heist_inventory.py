"""
Heist Wars — /inventory and /loadout.

Presentation only, same contract as heist.py: all ownership/requirement
checks happen in game/item_service.py, never here. Every equip/unequip
button re-derives the acting user from interaction.user.id and the service
layer re-validates ownership against the DB — a modified custom_id cannot
equip an item the interacting user doesn't own (brief §16).
"""

from __future__ import annotations

import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from discord_bot.cogs import _heist_ui as ui
from discord_bot.cogs._dm_support import GuildOnlyCog
from game import heist_service, item_engine, item_service
from game.items import Category, get_item, items_by_category

logger = logging.getLogger(__name__)

CATEGORY_LABELS = {
    Category.TOOL: "Tools", Category.EQUIPMENT: "Equipment", Category.SKIN: "Skins",
    Category.MASK: "Masks", Category.BADGE: "Badges", Category.THEME: "Themes",
}


def _clone_id_for(interaction: discord.Interaction) -> Optional[int]:
    return getattr(interaction.client, "clone_id", None)


async def _player_level(guild_id: int, clone_id: Optional[int], user_id: int) -> int:
    run = await heist_service.get_active_run(guild_id, clone_id, user_id)
    if run:
        return run["level_at_start"]
    # No active run — fall back to querying the player row directly.
    from database import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT level FROM heist_players WHERE guild_id = $1 AND clone_id IS NOT DISTINCT FROM $2 AND user_id = $3",
            guild_id, clone_id, user_id,
        )
        return row["level"] if row else 1


class CategorySelect(discord.ui.Select):
    def __init__(self, cog: "HeistInventoryCog"):
        options = [
            discord.SelectOption(label=label, value=cat.value, emoji="🔷")
            for cat, label in CATEGORY_LABELS.items()
        ]
        super().__init__(placeholder="Browse category...", options=options, min_values=1, max_values=1)
        self.cog = cog

    async def callback(self, interaction: discord.Interaction):
        await self.cog.show_category(interaction, Category(self.values[0]))


class InventoryView(discord.ui.View):
    def __init__(self, cog: "HeistInventoryCog"):
        super().__init__(timeout=180)
        self.add_item(CategorySelect(cog))


class HeistInventoryCog(GuildOnlyCog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def _summary_embed(self, guild_id: int, clone_id: Optional[int], user_id: int) -> discord.Embed:
        inv = await item_service.get_inventory(guild_id, clone_id, user_id)
        owned_keys = {row["item"].key for row in inv}
        owned_count, total_count = item_engine.calculate_collection_progress(owned_keys)
        pct = int((owned_count / total_count) * 100) if total_count else 0

        counts = {cat: 0 for cat in CATEGORY_LABELS}
        for row in inv:
            counts[row["item"].category] += 1

        embed = ui.base_embed("Nexus Inventory", color=ui.CYAN, description=f"OPERATIVE: <@{user_id}>")
        embed.add_field(name="TOOLS", value=str(counts[Category.TOOL] + counts[Category.EQUIPMENT]), inline=True)
        embed.add_field(name="SKINS", value=str(counts[Category.SKIN]), inline=True)
        embed.add_field(name="MASKS", value=str(counts[Category.MASK]), inline=True)
        embed.add_field(name="BADGES", value=str(counts[Category.BADGE]), inline=True)
        embed.add_field(name="COLLECTION", value=f"{ui.progress_bar(pct)} ({owned_count}/{total_count})", inline=False)
        embed.set_footer(text="Select a category below to browse.")
        return embed

    async def show_category(self, interaction: discord.Interaction, category: Category):
        guild_id, clone_id, user_id = interaction.guild_id, _clone_id_for(interaction), interaction.user.id
        inv = await item_service.get_inventory(guild_id, clone_id, user_id)
        owned = {row["item"].key: row["quantity"] for row in inv}
        loadout = await item_service.get_loadout(guild_id, clone_id, user_id)
        equipped_keys = {v for k, v in loadout.items() if k in item_service.ALL_SLOTS and v}

        items = items_by_category(category)
        embed = ui.base_embed(f"Inventory // {CATEGORY_LABELS[category]}", color=ui.CYAN)
        if not items:
            embed.description = "Nothing in this category yet."
        else:
            lines = []
            for item in items:
                if item.key in owned:
                    lines.append(ui.item_line(item, owned=True, equipped=item.key in equipped_keys, quantity=owned[item.key]))
                else:
                    lines.append(f"○ {item.name} — _{item.rarity.value.upper()}_ (not owned)")
            embed.description = "\n".join(lines)
        await interaction.response.edit_message(embed=embed, view=InventoryView(self))

    @app_commands.command(name="inventory", description="View your Heist Wars inventory")
    async def inventory(self, interaction: discord.Interaction):
        embed = await self._summary_embed(interaction.guild_id, _clone_id_for(interaction), interaction.user.id)
        await interaction.response.send_message(embed=embed, view=InventoryView(self), ephemeral=True)

    # -- /loadout ------------------------------------------------------------

    @app_commands.command(name="loadout", description="Manage your equipped tools and cosmetics")
    @app_commands.describe(item="Item to equip (leave empty to just view your loadout)")
    async def loadout(self, interaction: discord.Interaction, item: Optional[str] = None):
        guild_id, clone_id, user_id = interaction.guild_id, _clone_id_for(interaction), interaction.user.id

        if item:
            item_def = get_item(item.strip().lower().replace(" ", "_"))
            if not item_def:
                await interaction.response.send_message("✕ Unknown item.", ephemeral=True)
                return
            level = await _player_level(guild_id, clone_id, user_id)
            try:
                if item_def.category in (Category.TOOL, Category.EQUIPMENT):
                    loadout = await item_service.get_loadout(guild_id, clone_id, user_id)
                    free_slot = next((s for s in item_service.TOOL_SLOTS if not loadout.get(s)), item_service.TOOL_SLOTS[0])
                    await item_service.equip_item(guild_id, clone_id, user_id, item_def.key, slot=free_slot, player_level=level)
                else:
                    await item_service.equip_item(guild_id, clone_id, user_id, item_def.key, player_level=level)
            except item_service.ItemNotOwnedError:
                await interaction.response.send_message("✕ You don't own that item.", ephemeral=True)
                return
            except item_service.RequirementsNotMetError as e:
                await interaction.response.send_message(f"✕ {e}", ephemeral=True)
                return
            except item_service.ItemServiceError as e:
                await interaction.response.send_message(f"✕ {e}", ephemeral=True)
                return

        loadout = await item_service.get_loadout(guild_id, clone_id, user_id)
        embed = ui.base_embed("Active Loadout", color=ui.VIOLET, description=f"OPERATIVE: <@{user_id}>")
        for slot in item_service.TOOL_SLOTS:
            key = loadout.get(slot)
            it = get_item(key) if key else None
            embed.add_field(name=slot.replace("_", " ").upper(), value=ui.item_line(it, equipped=True) if it else "— empty —", inline=True)
        for slot in item_service.COSMETIC_SLOTS:
            key = loadout.get(slot)
            it = get_item(key) if key else None
            embed.add_field(name=slot.upper(), value=ui.item_line(it, equipped=True) if it else "— empty —", inline=True)
        embed.set_footer(text="Use /loadout item:<name> to equip, or /inventory to browse what you own.")
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(HeistInventoryCog(bot))
