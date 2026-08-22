"""
Heist Wars — pure item/equipment calculations.

Same rules as heist_engine.py: no Discord imports, no database imports, no
global mutable state, fully unit-testable. This module never touches the
database — game/item_service.py is the only thing allowed to call into it
with live inventory data.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from game.items import ItemDefinition, ITEMS, Rarity, RARITY_DROP_WEIGHTS, EffectType, droppable_items
from game.models import HeistState

# Global per-run cap on the *combined* success-chance contribution from all
# equipped tools/equipment, regardless of how many items are equipped or
# how the loadout is arranged. This is the ceiling referenced throughout
# the brief ("Maximum total modifier from equipped items") — no single
# item's own per-rarity cap (game/items.py RARITY_MAX_MODIFIER) can be
# stacked past this by equipping multiple items.
ITEM_MODIFIER_CAP = 10

# Global per-run cap on combined reward/XP % bonuses from equipped items.
REWARD_MODIFIER_CAP_PCT = 15
XP_MODIFIER_CAP_PCT = 15

# Chance a successful heist rolls for a bonus item drop at all.
ITEM_DROP_CHANCE_PCT = 25


@dataclass(frozen=True)
class ItemModifierResult:
    raw_total: int
    capped_total: int
    contributing_items: tuple[str, ...]  # item keys that had a non-zero effect for this phase


def calculate_phase_item_modifier(equipped_item_keys: list[str], phase: HeistState) -> ItemModifierResult:
    """Sums SUCCESS_MODIFIER effects from equipped gameplay items that apply
    to `phase`, then clamps to ITEM_MODIFIER_CAP. Unknown/cosmetic item keys
    contribute nothing (defensive — equip validation should already prevent
    a cosmetic reaching here, but this function never trusts that)."""
    total = 0
    contributing: list[str] = []
    for key in equipped_item_keys:
        item = ITEMS.get(key)
        if item is None:
            continue
        for eff in item.effects:
            if eff.type == EffectType.SUCCESS_MODIFIER and eff.phase == phase:
                total += eff.magnitude
                contributing.append(key)
    capped = max(0, min(ITEM_MODIFIER_CAP, total))
    return ItemModifierResult(raw_total=total, capped_total=capped, contributing_items=tuple(contributing))


def calculate_reward_modifier_pct(equipped_item_keys: list[str]) -> int:
    """Combined % bonus to cash reward from equipped items with a global
    (non-phase) REWARD_MODIFIER effect, capped."""
    total = sum(
        eff.magnitude
        for key in equipped_item_keys
        if (item := ITEMS.get(key)) is not None
        for eff in item.effects
        if eff.type == EffectType.REWARD_MODIFIER
    )
    return max(0, min(REWARD_MODIFIER_CAP_PCT, total))


def calculate_xp_modifier_pct(equipped_item_keys: list[str]) -> int:
    total = sum(
        eff.magnitude
        for key in equipped_item_keys
        if (item := ITEMS.get(key)) is not None
        for eff in item.effects
        if eff.type == EffectType.XP_MODIFIER
    )
    return max(0, min(XP_MODIFIER_CAP_PCT, total))


def resolve_drop(rng: random.Random | None = None) -> ItemDefinition | None:
    """Server-side-only random item roll for a successful heist. Returns
    None if no item drops this time (see ITEM_DROP_CHANCE_PCT), otherwise
    an ItemDefinition picked from a weighted rarity table, then a uniform
    pick among items of that rarity. Never accepts client input."""
    rng = rng or random.Random()
    if rng.randint(1, 100) > ITEM_DROP_CHANCE_PCT:
        return None

    pool = droppable_items()
    weighted_rarities = [(r, w) for r, w in RARITY_DROP_WEIGHTS.items() if w > 0]
    if not weighted_rarities:
        return None
    rarities, weights = zip(*weighted_rarities)
    chosen_rarity: Rarity = rng.choices(rarities, weights=weights, k=1)[0]

    candidates = [it for it in pool if it.rarity == chosen_rarity]
    if not candidates:
        return None
    return rng.choice(candidates)


def calculate_collection_progress(owned_item_keys: set[str]) -> tuple[int, int]:
    """(owned_count, total_count) across the entire catalog — used for the
    inventory 'Collection %' display."""
    total = len(ITEMS)
    owned = len(owned_item_keys & set(ITEMS.keys()))
    return owned, total
