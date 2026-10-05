"""Pure rules for XP and levels (no database)."""
import asyncio

import pytest

from modules import catch_xp
from modules.catch_game import LEVEL_MAX


def test_curve_is_positive_increasing_and_zero_at_cap():
    sizes = [catch_xp.xp_to_next(lv) for lv in range(1, LEVEL_MAX)]
    assert all(s > 0 for s in sizes)
    assert sizes == sorted(sizes) and len(set(sizes)) == len(sizes)
    assert catch_xp.xp_to_next(LEVEL_MAX) == 0
    with pytest.raises(ValueError):
        catch_xp.xp_to_next(0)


def test_apply_xp_no_level_up_keeps_progress():
    assert catch_xp.apply_xp(5, 0, 10) == (5, 10)
    assert catch_xp.apply_xp(5, 10, 0) == (5, 10)


def test_apply_xp_exact_boundary_levels_up_with_zero_left():
    need = catch_xp.xp_to_next(5)
    assert catch_xp.apply_xp(5, 0, need) == (6, 0)
    assert catch_xp.apply_xp(5, 0, need - 1) == (5, need - 1)


def test_apply_xp_can_cross_several_levels_and_keeps_remainder():
    total = catch_xp.xp_to_next(5) + catch_xp.xp_to_next(6) + 7
    assert catch_xp.apply_xp(5, 0, total) == (7, 7)


def test_apply_xp_stops_at_cap_and_zeroes_xp():
    assert catch_xp.apply_xp(LEVEL_MAX - 1, 0, 10**9) == (LEVEL_MAX, 0)
    assert catch_xp.apply_xp(LEVEL_MAX, 5, 100) == (LEVEL_MAX, 0)


def test_apply_xp_rejects_negative():
    with pytest.raises(ValueError):
        catch_xp.apply_xp(5, 0, -1)


def test_every_source_budget_is_sane():
    for source, (per_grant, per_day) in catch_xp.SOURCES.items():
        assert 0 < per_grant <= per_day, source


@pytest.mark.parametrize("kwargs, reason", [
    ({"source": "nope", "amount": 5, "idem_key": "k"}, "bad_source"),
    ({"source": "catch", "amount": 0, "idem_key": "k"}, "bad_amount"),
    ({"source": "catch", "amount": -3, "idem_key": "k"}, "bad_amount"),
    ({"source": "catch", "amount": True, "idem_key": "k"}, "bad_amount"),
    ({"source": "catch", "amount": 5, "idem_key": ""}, "bad_amount"),
    ({"source": "catch", "amount": 5, "idem_key": "x" * 201}, "bad_amount"),
])
def test_grant_xp_refuses_bad_input_before_touching_the_database(kwargs, reason):
    result = asyncio.run(catch_xp.grant_xp(1, 1, None, **kwargs))
    assert result.ok is False and result.reason == reason


class _Embed:
    def __init__(self):
        self.fields = []

    def add_field(self, *, name, value, inline):
        self.fields.append((name, value, inline))


class _Interaction:
    class user:
        id = 77

    class client:
        clone_id = None

    guild_id = None


class _Result:
    owned_id = 5
    new_species = False


def _claim(monkeypatch, outcome):
    from discord_bot.cogs import catch as cog

    async def fake(*args, **kwargs):
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(cog, "grant_buddy_catch_xp", fake)
    embed = _Embed()
    asyncio.run(cog.SpawnClaimView._add_buddy_xp(object(), embed, _Interaction(), _Result()))
    return embed.fields


def test_claim_screen_shows_buddy_gain(monkeypatch):
    fields = _claim(monkeypatch, catch_xp.XpResult(True, None, 10, 4, 4, 10))
    assert len(fields) == 1 and "10 XP" in fields[0][1] and "level" not in fields[0][1].lower()


def test_claim_screen_shows_level_up(monkeypatch):
    fields = _claim(monkeypatch, catch_xp.XpResult(True, None, 25, 4, 5, 3))
    assert len(fields) == 1 and "level 5" in fields[0][1]


@pytest.mark.parametrize("outcome", [
    None,
    catch_xp.XpResult(False, "capped"),
    catch_xp.XpResult(False, "max_level"),
    RuntimeError("db down"),
])
def test_claim_screen_adds_nothing_and_never_raises_when_xp_unavailable(monkeypatch, outcome):
    assert _claim(monkeypatch, outcome) == []
