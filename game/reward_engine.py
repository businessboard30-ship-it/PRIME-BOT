"""
Heist Wars — reward calculation.

v1 scope decision (brief section 18/31): NO partial success. A run either
COMPLETED (success) or FAILED — rewards are binary, not scaled by how far
the player got. Intel/reputation are defined here per the brief's minimum
field list but kept simple: intel is granted only on success, reputation
is a small flat amount on success and zero on failure. No premium/economy
systems beyond that are implemented.

Rounding: all reward values are integers; `random.randint` is inclusive on
both ends so results already land on whole numbers, no rounding needed.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from game.models import LocationDefinition

# Flat v1 numbers, deliberately not per-location (kept simple per brief §15/18).
INTEL_ON_SUCCESS = 1
REPUTATION_ON_SUCCESS = 1
REPUTATION_ON_FAILURE = 0
INTEL_ON_FAILURE = 0

# On failure, cash/XP are not zero — a total wipe on every failed run makes
# the loop punishing with no learning curve, so failure pays out a small
# consolation fraction of the location's range instead of nothing. This is
# a v1 design decision, documented rather than left implicit.
FAILURE_REWARD_FRACTION = 0.15


@dataclass(frozen=True)
class RewardResult:
    cash: int
    xp: int
    intel: int
    reputation: int


def calculate_reward(location: LocationDefinition, success: bool, rng: random.Random | None = None) -> RewardResult:
    rng = rng or random.Random()
    if success:
        cash = rng.randint(location.min_reward_cash, location.max_reward_cash)
        xp = rng.randint(location.min_reward_xp, location.max_reward_xp)
        return RewardResult(cash=cash, xp=xp, intel=INTEL_ON_SUCCESS, reputation=REPUTATION_ON_SUCCESS)

    max_fail_cash = max(0, int(location.min_reward_cash * FAILURE_REWARD_FRACTION))
    max_fail_xp = max(0, int(location.min_reward_xp * FAILURE_REWARD_FRACTION))
    cash = rng.randint(0, max_fail_cash) if max_fail_cash > 0 else 0
    xp = rng.randint(0, max_fail_xp) if max_fail_xp > 0 else 0
    return RewardResult(cash=cash, xp=xp, intel=INTEL_ON_FAILURE, reputation=REPUTATION_ON_FAILURE)
