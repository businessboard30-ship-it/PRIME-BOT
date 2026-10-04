import pytest

from modules.catch_gate import Gate, decide


def test_gate_precedence_global_then_action():
    assert decide({"game": (False, "global off"), "catch": (True, None)}, "catch") == Gate(False, "global off")
    assert decide({"game": (True, None), "catch": (False, "catch off")}, "catch") == Gate(False, "catch off")
    assert decide({"game": (True, None), "catch": (True, None)}, "catch") == Gate(True)


def test_unknown_action_is_rejected():
    with pytest.raises(KeyError):
        decide({}, "not-an-action")


def test_missing_flags_default_to_enabled():
    assert decide({}, "catch") == Gate(True)


def test_each_action_feature_is_independently_gated():
    actions_and_features = {
        "view": "game",
        "spawn": "spawn",
        "encounter": "encounter",
        "fish": "fishing",
        "trade": "trading",
        "market": "market",
        "shop": "shop",
        "lootbox": "lootbox",
        "egg": "eggs",
        "swap": "swap",
        "lottery": "lottery",
        "catchbot": "catchbot",
        "crew": "crews",
        "contest": "contests",
        "giveaway": "giveaways",
    }
    for action, feature in actions_and_features.items():
        assert decide({feature: (False, f"{feature} off")}, action) == Gate(False, f"{feature} off")


def test_global_game_gate_wins_over_action_enablement():
    assert decide({"game": (False, "maintenance"), "spawn": (True, None)}, "spawn") == Gate(False, "maintenance")


def test_disabled_flag_uses_stable_default_reason():
    assert decide({"game": (False, None)}, "view") == Gate(False, "game_disabled")
