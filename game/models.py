"""
Heist Wars — core data models.

Pure data. No Discord imports, no database imports. Everything the engine,
decision engine, reward engine and service layer pass around is one of
these types, so the game mechanics stay testable without a live bot or DB.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------

class HeistState(str, Enum):
    PLANNING = "PLANNING"
    INFILTRATION = "INFILTRATION"
    OBJECTIVE = "OBJECTIVE"
    LOOT = "LOOT"
    ESCAPE = "ESCAPE"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    EXPIRED = "EXPIRED"


# Explicit allow-list of transitions. Anything not listed here is invalid.
# PLANNING -> FAILED covers "failed the entry roll before infiltration even
# starts" (see heist_engine.determine_outcome), which the brief's example
# transition list omits but the success-formula section requires.
VALID_TRANSITIONS: dict[HeistState, frozenset[HeistState]] = {
    HeistState.PLANNING: frozenset({HeistState.INFILTRATION, HeistState.FAILED, HeistState.EXPIRED}),
    HeistState.INFILTRATION: frozenset({HeistState.OBJECTIVE, HeistState.FAILED, HeistState.EXPIRED}),
    HeistState.OBJECTIVE: frozenset({HeistState.LOOT, HeistState.FAILED, HeistState.EXPIRED}),
    HeistState.LOOT: frozenset({HeistState.ESCAPE, HeistState.FAILED, HeistState.EXPIRED}),
    HeistState.ESCAPE: frozenset({HeistState.COMPLETED, HeistState.FAILED, HeistState.EXPIRED}),
    HeistState.COMPLETED: frozenset(),
    HeistState.FAILED: frozenset(),
    HeistState.EXPIRED: frozenset(),
}

TERMINAL_STATES = frozenset({HeistState.COMPLETED, HeistState.FAILED, HeistState.EXPIRED})

# Phases that carry a scripted event (PLANNING and ESCAPE do not — PLANNING
# is location/approach selection, ESCAPE is the final success roll only).
EVENT_PHASES: tuple[HeistState, ...] = (HeistState.INFILTRATION, HeistState.OBJECTIVE, HeistState.LOOT)


def is_valid_transition(current: HeistState, target: HeistState) -> bool:
    return target in VALID_TRANSITIONS.get(current, frozenset())


class Approach(str, Enum):
    """How the player commits to the whole run. Chosen once, in PLANNING,
    and never changes after — the brief requires deciding whether choices
    are permanent; approach is (events within a phase are not — see
    decision_engine)."""

    STEALTH = "stealth"
    LOUD = "loud"
    TECHNICAL = "technical"


# approach_modifier applied to every success roll for the whole run.
APPROACH_MODIFIERS: dict[Approach, int] = {
    Approach.STEALTH: 8,
    Approach.LOUD: -5,
    Approach.TECHNICAL: 4,
}


# ---------------------------------------------------------------------------
# Location & event definitions (static game content — see locations.py)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LocationDefinition:
    key: str
    name: str
    required_level: int
    difficulty_penalty: int          # subtracted from success chance
    base_chance: int                 # starting success chance before modifiers
    min_reward_cash: int
    max_reward_cash: int
    min_reward_xp: int
    max_reward_xp: int
    cooldown_seconds: int
    min_crew: int = 1
    max_crew: int = 1

    def __post_init__(self):
        assert 0 <= self.base_chance <= 100
        assert self.difficulty_penalty >= 0
        assert self.required_level >= 1
        assert self.min_reward_cash >= 0 and self.max_reward_cash >= self.min_reward_cash
        assert self.min_reward_xp >= 0 and self.max_reward_xp >= self.min_reward_xp
        assert self.cooldown_seconds >= 0
        assert self.min_crew >= 1 and self.max_crew >= self.min_crew


@dataclass(frozen=True)
class ChoiceDefinition:
    key: str
    label: str
    success_modifier: int
    # If true, picking this choice ends the run immediately as FAILED
    # regardless of the roll (e.g. "trigger the alarm on purpose" is never
    # in v1 content, but the mechanic must exist per the brief's event
    # spec requirements).
    instant_fail: bool = False


@dataclass(frozen=True)
class EventDefinition:
    key: str
    phase: HeistState
    description: str
    choices: tuple[ChoiceDefinition, ...]
    # Restrict which locations/approaches this event can be selected for.
    # Empty tuple = no restriction (applies everywhere).
    location_keys: tuple[str, ...] = ()
    approach_keys: tuple[Approach, ...] = ()

    def __post_init__(self):
        assert self.phase in EVENT_PHASES
        assert len(self.choices) >= 2
        keys = [c.key for c in self.choices]
        assert len(keys) == len(set(keys)), "duplicate choice keys"


# ---------------------------------------------------------------------------
# Runtime state (round-trips through the DB via heist_service)
# ---------------------------------------------------------------------------

@dataclass
class EventOccurrence:
    """One event as it was actually presented during a run (resolved from
    an EventDefinition + phase), persisted so a resumed/replayed run shows
    the exact same event rather than re-rolling it."""
    phase: HeistState
    event_key: str
    choice_key: Optional[str] = None
    modifier_applied: Optional[int] = None


@dataclass
class HeistRunState:
    run_id: int
    guild_id: int
    user_id: int
    location_key: str
    approach: Approach
    level_at_start: int
    state: HeistState
    events: list[EventOccurrence] = field(default_factory=list)
    decision_modifier_total: int = 0
    random_modifier: Optional[int] = None       # rolled exactly once, at completion
    final_success_chance: Optional[int] = None
    succeeded: Optional[bool] = None

    def current_phase_index(self) -> int:
        return len(self.events)
