import random

import pytest

from game import heist_engine, reward_engine
from game.decision_engine import resolve_event_choice, select_event
from game.locations import LOCATIONS, get_event
from game.models import (
    APPROACH_MODIFIERS,
    Approach,
    HeistState,
    is_valid_transition,
)


def _loc():
    return LOCATIONS["jewelry_store"]


class TestSuccessCalculation:
    def test_matches_formula_exactly(self):
        loc = _loc()
        b = heist_engine.calculate_success_chance(loc, Approach.STEALTH, player_level=10, decision_modifier=5)
        expected_raw = loc.base_chance + 10 + APPROACH_MODIFIERS[Approach.STEALTH] + 5 - loc.difficulty_penalty
        assert b.raw_chance == expected_raw
        assert b.final_chance == max(0, min(100, expected_raw))

    def test_level_bonus_caps(self):
        assert heist_engine.level_bonus(25) == 25
        assert heist_engine.level_bonus(1000) == heist_engine.level_bonus(25)

    def test_level_bonus_rejects_negative(self):
        with pytest.raises(ValueError):
            heist_engine.level_bonus(-1)


class TestProbabilityClamping:
    @pytest.mark.parametrize("raw,expected", [(-999, 0), (0, 0), (50, 50), (100, 100), (999, 100)])
    def test_clamp_probability(self, raw, expected):
        assert heist_engine.clamp_probability(raw) == expected

    def test_final_chance_never_exceeds_bounds(self):
        loc = _loc()
        b = heist_engine.calculate_success_chance(loc, Approach.STEALTH, player_level=1000, decision_modifier=1000)
        assert 0 <= b.final_chance <= 100
        b2 = heist_engine.calculate_success_chance(loc, Approach.LOUD, player_level=0, decision_modifier=-1000)
        assert 0 <= b2.final_chance <= 100


class TestApproachModifiers:
    def test_all_approaches_defined(self):
        for approach in Approach:
            assert approach in APPROACH_MODIFIERS


class TestDecisionModifiers:
    def test_resolve_choice_accumulates(self):
        total = 0
        total = heist_engine.resolve_choice(total, 3)
        total = heist_engine.resolve_choice(total, -2)
        assert total == 1


class TestRandomnessHandling:
    def test_outcome_deterministic_given_roll(self):
        assert heist_engine.determine_outcome(final_chance=50, random_modifier=49) is True
        assert heist_engine.determine_outcome(final_chance=50, random_modifier=50) is False

    def test_rejects_out_of_range_roll(self):
        with pytest.raises(ValueError):
            heist_engine.determine_outcome(final_chance=50, random_modifier=100)
        with pytest.raises(ValueError):
            heist_engine.determine_outcome(final_chance=50, random_modifier=-1)

    def test_zero_chance_never_succeeds(self):
        for roll in range(0, 100):
            assert heist_engine.determine_outcome(final_chance=0, random_modifier=roll) is False

    def test_hundred_chance_always_succeeds(self):
        for roll in range(0, 100):
            assert heist_engine.determine_outcome(final_chance=100, random_modifier=roll) is True


class TestStateTransitions:
    def test_valid_forward_chain(self):
        chain = [
            HeistState.PLANNING, HeistState.INFILTRATION, HeistState.OBJECTIVE,
            HeistState.LOOT, HeistState.ESCAPE, HeistState.COMPLETED,
        ]
        for a, b in zip(chain, chain[1:]):
            assert is_valid_transition(a, b)

    def test_terminal_states_reject_everything(self):
        for terminal in (HeistState.COMPLETED, HeistState.FAILED, HeistState.EXPIRED):
            for target in HeistState:
                assert not is_valid_transition(terminal, target)

    def test_cannot_skip_phases(self):
        assert not is_valid_transition(HeistState.PLANNING, HeistState.LOOT)
        assert not is_valid_transition(HeistState.INFILTRATION, HeistState.COMPLETED)

    def test_any_active_phase_can_fail_or_expire(self):
        for phase in (HeistState.PLANNING, HeistState.INFILTRATION, HeistState.OBJECTIVE,
                      HeistState.LOOT, HeistState.ESCAPE):
            assert is_valid_transition(phase, HeistState.FAILED)
            assert is_valid_transition(phase, HeistState.EXPIRED)


class TestEventSelection:
    def test_never_repeats_within_run(self):
        rng = random.Random(1)
        used = set()
        # Only 2 INFILTRATION events exist in v1 content (game/locations.py) —
        # exercise exactly that many draws, since a 3rd would legitimately
        # raise NoEligibleEventError.
        for _ in range(2):
            ev = select_event(HeistState.INFILTRATION, "jewelry_store", Approach.STEALTH, used, rng=rng)
            assert ev.key not in used
            used.add(ev.key)

    def test_resolve_choice_rejects_unknown_key(self):
        event = get_event("security_checkpoint")
        with pytest.raises(ValueError):
            resolve_event_choice(event, "not_a_real_choice")

    def test_instant_fail_flagged_correctly(self):
        event = get_event("silent_alarm")
        res = resolve_event_choice(event, "trigger")
        assert res.instant_fail is True
        res2 = resolve_event_choice(event, "disarm")
        assert res2.instant_fail is False


class TestRewardEngine:
    def test_success_reward_within_location_bounds(self):
        loc = _loc()
        rng = random.Random(42)
        for _ in range(50):
            r = reward_engine.calculate_reward(loc, success=True, rng=rng)
            assert loc.min_reward_cash <= r.cash <= loc.max_reward_cash
            assert loc.min_reward_xp <= r.xp <= loc.max_reward_xp
            assert r.intel == reward_engine.INTEL_ON_SUCCESS
            assert r.reputation == reward_engine.REPUTATION_ON_SUCCESS

    def test_failure_reward_never_exceeds_success_minimum(self):
        loc = _loc()
        rng = random.Random(7)
        for _ in range(50):
            r = reward_engine.calculate_reward(loc, success=False, rng=rng)
            assert 0 <= r.cash <= loc.min_reward_cash
            assert r.intel == 0
            assert r.reputation == 0

    def test_rewards_never_negative(self):
        for loc in LOCATIONS.values():
            for success in (True, False):
                r = reward_engine.calculate_reward(loc, success=success)
                assert r.cash >= 0 and r.xp >= 0 and r.intel >= 0 and r.reputation >= 0


class TestLocationDefinitions:
    def test_all_locations_internally_consistent(self):
        for loc in LOCATIONS.values():
            assert 0 <= loc.base_chance <= 100
            assert loc.max_reward_cash >= loc.min_reward_cash
            assert loc.max_reward_xp >= loc.min_reward_xp
            assert loc.max_crew >= loc.min_crew
