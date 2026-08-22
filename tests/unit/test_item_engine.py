import random

import pytest

from game import item_engine
from game.items import ITEMS, Category, ItemDefinition, ItemEffect, EffectType, Rarity
from game.models import HeistState


def test_phase_modifier_sums_matching_items():
    r = item_engine.calculate_phase_item_modifier(["lockpick_kit", "signal_jammer"], HeistState.INFILTRATION)
    assert r.raw_total == 5  # 2 + 3
    assert r.capped_total == 5
    assert set(r.contributing_items) == {"lockpick_kit", "signal_jammer"}


def test_phase_modifier_ignores_non_matching_phase():
    r = item_engine.calculate_phase_item_modifier(["lockpick_kit"], HeistState.LOOT)
    assert r.raw_total == 0
    assert r.contributing_items == ()


def test_phase_modifier_ignores_unknown_item_keys():
    r = item_engine.calculate_phase_item_modifier(["not_a_real_item"], HeistState.INFILTRATION)
    assert r.raw_total == 0


def test_phase_modifier_is_capped_globally():
    # Every gameplay tool that hits OBJECTIVE, stacked, would exceed the cap
    # without clamping (thermal_cutter 5 + vault_scanner 4 + disguise_kit 3 = 12).
    keys = ["thermal_cutter", "vault_scanner", "disguise_kit"]
    r = item_engine.calculate_phase_item_modifier(keys, HeistState.OBJECTIVE)
    assert r.raw_total == 12
    assert r.capped_total == item_engine.ITEM_MODIFIER_CAP == 10


def test_reward_and_xp_modifiers_capped():
    keys = ["fence_contact"] * 10  # duplicate keys, only real effects counted per occurrence
    pct = item_engine.calculate_reward_modifier_pct(keys)
    assert pct <= item_engine.REWARD_MODIFIER_CAP_PCT


def test_no_item_definition_exceeds_its_rarity_cap():
    from game.items import RARITY_MAX_MODIFIER
    for item in ITEMS.values():
        for eff in item.effects:
            assert eff.magnitude <= RARITY_MAX_MODIFIER[item.rarity]


def test_cosmetic_items_cannot_hold_effects():
    with pytest.raises(AssertionError):
        ItemDefinition(
            key="bad_skin",
            name="Bad Skin",
            category=Category.SKIN,
            rarity=Rarity.COMMON,
            description="",
            effects=(ItemEffect(EffectType.SUCCESS_MODIFIER, HeistState.INFILTRATION, 2),),
        )


def test_resolve_drop_is_deterministic_for_a_seeded_rng():
    rng1 = random.Random(42)
    rng2 = random.Random(42)
    d1 = item_engine.resolve_drop(rng1)
    d2 = item_engine.resolve_drop(rng2)
    assert (d1.key if d1 else None) == (d2.key if d2 else None)


def test_resolve_drop_never_returns_cosmetic():
    rng = random.Random(7)
    for _ in range(200):
        item = item_engine.resolve_drop(rng)
        if item is not None:
            assert not item.is_cosmetic


def test_collection_progress():
    owned = {"lockpick_kit", "not_real_key"}
    owned_count, total = item_engine.calculate_collection_progress(owned)
    assert owned_count == 1
    assert total == len(ITEMS)
