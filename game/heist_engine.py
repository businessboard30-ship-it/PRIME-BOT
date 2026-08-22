"""
Heist Wars — pure game mechanics.

No Discord imports. No database imports. No global mutable state. Every
function here takes explicit inputs and returns explicit outputs so it can
be unit tested without a bot or a DB connection.

--------------------------------------------------------------------------
SUCCESS FORMULA (authoritative — see docs/HEIST_WARS.md for the writeup)
--------------------------------------------------------------------------

    raw_chance = base_chance
               + level_bonus
               + approach_modifier
               + decision_modifier
               + item_modifier
               - difficulty_penalty

    final_chance = clamp(raw_chance, 0, 100)     # persisted, pre-randomness
    roll = random_modifier                       # 0-99, generated exactly once
    success = roll < final_chance

- `base_chance` and `difficulty_penalty` come from the LocationDefinition.
- `level_bonus` = min(player_level, LEVEL_BONUS_CAP_LEVEL) * LEVEL_BONUS_PER_LEVEL
  (see level_bonus() below). Capped so a very high level can't push chance
  to a guaranteed 100 on its own.
- `approach_modifier` comes from APPROACH_MODIFIERS (models.py), fixed for
  the whole run.
- `decision_modifier` is the running sum of every choice's success_modifier
  across all phases played so far (decision_engine.py resolves each choice;
  this module just sums what it's given).
- `item_modifier` is the combined success-chance contribution of the
  player's equipped tools/equipment across all event phases played, already
  capped server-side by game/item_engine.py (ITEM_MODIFIER_CAP) before it
  ever reaches this function. Defaults to 0 for callers/tests that don't
  care about items — this function does not itself re-derive or re-cap it,
  the caller (heist_service) is responsible for passing an already-capped
  value.
- `random_modifier` is an integer roll in [0, 99] used as roll < final_chance
  (i.e. a 70% final_chance succeeds on rolls 0-69). It is generated exactly
  once per run at completion time (see heist_service) and persisted, so a
  retried/duplicated completion request never re-rolls.
- All intermediate modifiers (decision_modifier, level_bonus, etc.) are NOT
  clamped individually — only the final probability is clamped to [0, 100].
  A raw_chance of 137 is stored as an intermediate value if ever inspected,
  but the persisted `final_success_chance` is always clamped.
"""

from __future__ import annotations

from dataclasses import dataclass

from game.models import Approach, APPROACH_MODIFIERS, LocationDefinition

LEVEL_BONUS_PER_LEVEL = 1
LEVEL_BONUS_CAP_LEVEL = 25  # levels beyond this add no further success bonus


def level_bonus(player_level: int) -> int:
    if player_level < 0:
        raise ValueError("player_level must be >= 0")
    return min(player_level, LEVEL_BONUS_CAP_LEVEL) * LEVEL_BONUS_PER_LEVEL


def approach_modifier(approach: Approach) -> int:
    return APPROACH_MODIFIERS[approach]


def clamp_probability(value: int) -> int:
    """Clamp to a valid probability. This is the ONLY place a probability
    is allowed to leave the [0, 100] range unclamped."""
    return max(0, min(100, value))


@dataclass(frozen=True)
class SuccessBreakdown:
    base_chance: int
    level_bonus: int
    approach_modifier: int
    decision_modifier: int
    item_modifier: int
    difficulty_penalty: int
    raw_chance: int
    final_chance: int  # clamped, this is what gets persisted/rolled against


def calculate_success_chance(
    location: LocationDefinition,
    approach: Approach,
    player_level: int,
    decision_modifier: int,
    item_modifier: int = 0,
) -> SuccessBreakdown:
    lb = level_bonus(player_level)
    am = approach_modifier(approach)
    raw = location.base_chance + lb + am + decision_modifier + item_modifier - location.difficulty_penalty
    return SuccessBreakdown(
        base_chance=location.base_chance,
        level_bonus=lb,
        approach_modifier=am,
        decision_modifier=decision_modifier,
        item_modifier=item_modifier,
        difficulty_penalty=location.difficulty_penalty,
        raw_chance=raw,
        final_chance=clamp_probability(raw),
    )


def resolve_choice(current_decision_modifier: int, choice_modifier: int) -> int:
    """Apply one choice's modifier to the running decision-modifier total.
    Not individually clamped — only the final probability is clamped."""
    return current_decision_modifier + choice_modifier


def determine_outcome(final_chance: int, random_modifier: int) -> bool:
    """random_modifier must be an integer in [0, 99], generated exactly
    once per run (see randomness rules in the module docstring)."""
    if not (0 <= random_modifier <= 99):
        raise ValueError("random_modifier must be in [0, 99]")
    final_chance = clamp_probability(final_chance)
    return random_modifier < final_chance
