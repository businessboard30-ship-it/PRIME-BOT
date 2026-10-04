import pytest

from modules.catch_gate import Gate, decide


def test_gate_precedence_global_then_action():
    assert decide({"game": (False, "global off"), "catch": (True, None)}, "catch") == Gate(False, "global off")
    assert decide({"game": (True, None), "catch": (False, "catch off")}, "catch") == Gate(False, "catch off")
    assert decide({"game": (True, None), "catch": (True, None)}, "catch") == Gate(True)


def test_unknown_action_is_rejected():
    with pytest.raises(KeyError):
        decide({}, "not-an-action")
