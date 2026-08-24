"""
Heist Wars — item/inventory/loadout service layer.

Same contract as heist_service.py: this is the only module allowed to touch
heist_player_inventory / heist_player_loadout / heist_item_log directly.
The Discord cog calls this, never raw SQL, and every mutating call
re-validates ownership + requirements server-side rather than trusting
anything the client sent (brief §9, §16, §18).
"""

from __future__ import annotations

import logging
from typing import Optional

from database import get_pool
from game.items import ItemDefinition, get_item

logger = logging.getLogger(__name__)

TOOL_SLOTS = ("tool_slot_1", "tool_slot_2", "tool_slot_3")
COSMETIC_SLOTS = ("skin", "mask", "badge")
ALL_SLOTS = TOOL_SLOTS + COSMETIC_SLOTS


class ItemServiceError(Exception):
    pass


class ItemNotOwnedError(ItemServiceError):
    pass


class ItemNotFoundError(ItemServiceError):
    pass


class InvalidSlotError(ItemServiceError):
    pass


class RequirementsNotMetError(ItemServiceError):
    pass


async def _log(conn, guild_id, clone_id, user_id, action: str, item_key: str,
                source_type: str | None = None, source_id: str | None = None) -> None:
    await conn.execute(
        """
        INSERT INTO heist_item_log (guild_id, clone_id, user_id, action, item_key, source_type, source_id)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        """,
        guild_id, clone_id, user_id, action, item_key, source_type, source_id,
    )


async def get_inventory(guild_id: int, clone_id: Optional[int], user_id: int) -> list[dict]:
    """Returns [{item, quantity, acquired_at}] for every item the player
    owns, joined against the code-side catalog (unknown/retired keys are
    silently skipped rather than crashing the UI)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT item_key, quantity, acquired_at, updated_at FROM heist_player_inventory
            WHERE guild_id = $1 AND clone_id IS NOT DISTINCT FROM $2 AND user_id = $3 AND quantity > 0
            ORDER BY acquired_at DESC
            """,
            guild_id, clone_id, user_id,
        )
    out = []
    for r in rows:
        item = get_item(r["item_key"])
        if item is None:
            continue
        out.append({"item": item, "quantity": r["quantity"], "acquired_at": r["acquired_at"]})
    return out


async def owns_item(conn, guild_id: int, clone_id: Optional[int], user_id: int, item_key: str) -> bool:
    row = await conn.fetchrow(
        """
        SELECT 1 FROM heist_player_inventory
        WHERE guild_id = $1 AND clone_id IS NOT DISTINCT FROM $2 AND user_id = $3
          AND item_key = $4 AND quantity > 0
        """,
        guild_id, clone_id, user_id, item_key,
    )
    return row is not None


async def grant_item(
    conn, guild_id: int, clone_id: Optional[int], user_id: int, item_key: str,
    *, source_type: str, source_id: str, quantity: int = 1,
) -> None:
    """Idempotent by construction ONLY when the caller already holds a
    higher-level idempotency guarantee for (source_type, source_id) — e.g.
    heist_service.complete_heist's UNIQUE(run_id) reward-ledger insert.
    This function itself just increments; callers granting from a
    non-atomic source must add their own dedupe check first."""
    if get_item(item_key) is None:
        raise ItemNotFoundError(item_key)
    await conn.execute(
        """
        INSERT INTO heist_player_inventory (guild_id, clone_id, user_id, item_key, quantity)
        VALUES ($1, $2, $3, $4, $5)
        ON CONFLICT (guild_id, (COALESCE(clone_id, -1)), user_id, item_key) DO UPDATE SET
            quantity = heist_player_inventory.quantity + EXCLUDED.quantity,
            updated_at = NOW()
        """,
        guild_id, clone_id, user_id, item_key, quantity,
    )
    await _log(conn, guild_id, clone_id, user_id, "granted", item_key, source_type, source_id)


async def _get_loadout_row(conn, guild_id: int, clone_id: Optional[int], user_id: int) -> dict:
    row = await conn.fetchrow(
        """
        SELECT * FROM heist_player_loadout
        WHERE guild_id = $1 AND clone_id IS NOT DISTINCT FROM $2 AND user_id = $3
        """,
        guild_id, clone_id, user_id,
    )
    if row:
        return dict(row)
    return {slot: None for slot in ALL_SLOTS}


async def get_loadout(guild_id: int, clone_id: Optional[int], user_id: int) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await _get_loadout_row(conn, guild_id, clone_id, user_id)


def _slot_for_category(item: ItemDefinition) -> str | None:
    from game.items import Category
    return {
        Category.SKIN: "skin",
        Category.MASK: "mask",
        Category.BADGE: "badge",
    }.get(item.category)


async def equip_item(
    guild_id: int, clone_id: Optional[int], user_id: int, item_key: str,
    *, slot: Optional[str] = None, player_level: int = 1,
) -> dict:
    """Equips `item_key` into `slot` (required for TOOL/EQUIPMENT — the
    player picks which of the 3 tool slots; auto-derived for cosmetics from
    the item's category). Verifies ownership + level requirement
    server-side before writing. Returns the resulting loadout row."""
    from game.items import Category

    item = get_item(item_key)
    if item is None:
        raise ItemNotFoundError(item_key)
    if player_level < item.required_level:
        raise RequirementsNotMetError(
            f"requires level {item.required_level}, player is level {player_level}"
        )

    if item.category in (Category.TOOL, Category.EQUIPMENT):
        if slot not in TOOL_SLOTS:
            raise InvalidSlotError(f"tool/equipment must be equipped into one of {TOOL_SLOTS}")
        target_slot = slot
    else:
        target_slot = _slot_for_category(item)
        if target_slot is None:
            raise InvalidSlotError(f"item category {item.category} has no loadout slot")

    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            if not await owns_item(conn, guild_id, clone_id, user_id, item_key):
                raise ItemNotOwnedError(f"player does not own {item_key}")

            current = await _get_loadout_row(conn, guild_id, clone_id, user_id)
            # A tool already equipped in a different slot gets moved, not
            # duplicated — prevents the same tool occupying two slots and
            # silently double-contributing to the item modifier.
            if item.category in (Category.TOOL, Category.EQUIPMENT):
                for s in TOOL_SLOTS:
                    if current.get(s) == item_key and s != target_slot:
                        current[s] = None
            current[target_slot] = item_key

            await conn.execute(
                f"""
                INSERT INTO heist_player_loadout (guild_id, clone_id, user_id, {', '.join(ALL_SLOTS)})
                VALUES ($1, $2, $3, {', '.join(f'${i+4}' for i in range(len(ALL_SLOTS)))})
                ON CONFLICT (guild_id, (COALESCE(clone_id, -1)), user_id) DO UPDATE SET
                    {', '.join(f'{s} = EXCLUDED.{s}' for s in ALL_SLOTS)},
                    updated_at = NOW()
                """,
                guild_id, clone_id, user_id, *[current[s] for s in ALL_SLOTS],
            )
            await _log(conn, guild_id, clone_id, user_id, "equipped", item_key)
            return await _get_loadout_row(conn, guild_id, clone_id, user_id)


async def unequip_item(guild_id: int, clone_id: Optional[int], user_id: int, slot: str) -> dict:
    if slot not in ALL_SLOTS:
        raise InvalidSlotError(f"unknown slot {slot!r}")
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            current = await _get_loadout_row(conn, guild_id, clone_id, user_id)
            removed_key = current.get(slot)
            current[slot] = None
            await conn.execute(
                f"""
                INSERT INTO heist_player_loadout (guild_id, clone_id, user_id, {', '.join(ALL_SLOTS)})
                VALUES ($1, $2, $3, {', '.join(f'${i+4}' for i in range(len(ALL_SLOTS)))})
                ON CONFLICT (guild_id, (COALESCE(clone_id, -1)), user_id) DO UPDATE SET
                    {slot} = NULL,
                    updated_at = NOW()
                """,
                guild_id, clone_id, user_id, *[current[s] for s in ALL_SLOTS],
            )
            if removed_key:
                await _log(conn, guild_id, clone_id, user_id, "unequipped", removed_key)
            return await _get_loadout_row(conn, guild_id, clone_id, user_id)


async def equipped_gameplay_item_keys(guild_id: int, clone_id: Optional[int], user_id: int) -> list[str]:
    """Tool/equipment keys currently equipped — what heist_service feeds
    into item_engine.calculate_phase_item_modifier. Re-validates ownership
    here too (not just at equip time) so an item removed/refunded after
    being equipped can never silently keep contributing a bonus."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        loadout = await _get_loadout_row(conn, guild_id, clone_id, user_id)
        keys = [loadout[s] for s in TOOL_SLOTS if loadout.get(s)]
        owned = []
        for k in keys:
            if await owns_item(conn, guild_id, clone_id, user_id, k):
                owned.append(k)
        return owned
