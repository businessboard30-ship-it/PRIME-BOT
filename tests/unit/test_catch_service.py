import asyncio

import pytest

from modules.catch_service import CatchResult, _check_values, counters_for, record_catch


def test_counter_matrix_covers_supported_sources():
    assert "dex" in counters_for("wild")
    assert "ownership" in counters_for("encounter")
    with pytest.raises(KeyError):
        counters_for("unknown")


def test_record_catch_rejects_unknown_source_before_database():
    with pytest.raises(KeyError, match="unknown source"):
        asyncio.run(record_catch(user_id=1, clone_id=None, guild_id=2, source="unknown", spawn_id=3))


def test_record_catch_requires_exactly_one_idempotency_input():
    with pytest.raises(ValueError, match="exactly one"):
        asyncio.run(record_catch(user_id=1, clone_id=None, guild_id=2, source="wild"))
    with pytest.raises(ValueError, match="exactly one"):
        asyncio.run(record_catch(user_id=1, clone_id=None, guild_id=2, source="wild", spawn_id=3, idem_key="duplicate-input"))


def test_catch_result_defaults_are_safe():
    result = CatchResult(claimed=False, reason="spawn_unavailable")
    assert result.claimed is False
    assert result.replay is False
    assert result.owned_id is None
    assert result.reason == "spawn_unavailable"


def test_check_values_rejects_invalid_rolls():
    with pytest.raises(ValueError, match="positive int"):
        _check_values(0, 1, [0, 0, 0, 0, 0])
    with pytest.raises(ValueError, match="level"):
        _check_values(1, 101, [0, 0, 0, 0, 0])
    with pytest.raises(ValueError, match="ivs"):
        _check_values(1, 1, [0, 0])


def test_check_values_accepts_valid_rolls():
    _check_values(1, 50, [0, 10, 20, 30, 31])


def test_record_catch_rejects_both_spawn_and_idempotency_key():
    with pytest.raises(ValueError, match="exactly one"):
        asyncio.run(record_catch(user_id=1, clone_id=None, guild_id=2, source="wild", species_id=1, level=1, ivs=[0, 0, 0, 0, 0, 0], spawn_id=1, idem_key="both"))
