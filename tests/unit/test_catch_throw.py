from modules.catch_throw import ThrowChoice, available_items, choice_label, consume_items, validate_choice


def test_available_items_is_ordered_and_filters_empty():
    balls, berries = available_items({"capsule_prime": 1, "capsule_basic": 0, "goldberry": 2})
    assert balls == ("capsule_prime",)
    assert berries == ("goldberry",)


def test_consume_items_returns_copy():
    inventory = {"capsule_basic": 2, "honeyberry": 1}
    assert consume_items(ThrowChoice("capsule_basic", "honeyberry"), inventory) == {
        "capsule_basic": 1,
        "honeyberry": 0,
    }
    assert inventory["capsule_basic"] == 2


def test_choice_requires_owned_items():
    try:
        validate_choice(ThrowChoice("capsule_prime"), {"capsule_basic": 1})
    except ValueError as exc:
        assert "available" in str(exc)
    else:
        raise AssertionError("expected unavailable ball to fail")


def test_choice_label():
    assert choice_label("capsule_sovereign") == "Sovereign"
    assert choice_label("honeyberry") == "Honey"
