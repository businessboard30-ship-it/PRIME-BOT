import random

import pytest

from modules.catch_game import (
    RARITIES,
    catch_chance,
    compute_stats,
    roll_ivs,
    roll_rarity,
    weighted_pick,
)


def test_rarity_weights_and_seeded_rolls():
    assert sum(item.spawn_weight_bp for item in RARITIES) == 10_000
    rng = random.Random(7)
    assert all(0 <= roll_rarity(rng).code <= 4 for _ in range(100))


def test_rolls_and_stats_formula():
    ivs = roll_ivs(random.Random(3))
    assert len(ivs) == 5 and all(0 <= value <= 31 for value in ivs)
    assert compute_stats([10, 20, 30, 40, 50], [0, 1, 2, 3, 4], 10) == {"vigor": 22, "power": 9, "guard": 11, "speed": 13, "spirit": 15}


def test_catch_chance_bounds_and_modifiers():
    tier = RARITIES[2]
    assert 0.01 <= catch_chance(tier, species_mod=0) <= 0.95
    assert catch_chance(tier, ball="capsule_prime") > catch_chance(tier)
    assert catch_chance(tier, ball="capsule_sovereign") == 1.0
    assert catch_chance(tier, shiny=True) < catch_chance(tier)
    with pytest.raises(ValueError):
        catch_chance(tier, ball="unknown")


def test_weighted_pick_is_seeded_and_rejects_empty_weights():
    assert weighted_pick(random.Random(1), {"a": 100, "b": 0}) == "a"
    with pytest.raises(ValueError):
        weighted_pick(random.Random(1), {})
    with pytest.raises(ValueError):
        weighted_pick(random.Random(1), {"a": 0})


def test_cooldown_format():
    from modules.catch_cooldowns import format_remaining
    assert format_remaining(0) == "ready"
    assert format_remaining(1) == "1s"
    assert format_remaining(61) == "1m 1s"


def test_game_module_exports_no_discord_dependency():
    assert callable(compute_stats)
