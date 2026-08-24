"""
Heist Wars — decision/event selection.

Mechanics this module makes explicit (per the implementation brief, section
7 — these must not stay implicit):

- Exactly ONE event occurs per event phase (INFILTRATION, OBJECTIVE, LOOT).
  Three phases carry an event, so a run presents exactly 3 events total.
  PLANNING is location/approach selection only; ESCAPE is the final roll
  only — neither carries a scripted event.
- Events do NOT repeat within a single run (selection excludes any
  event_key already recorded in HeistRunState.events).
- Event selection is server-side, uniform-random among the events eligible
  for (phase, location, approach) at the moment that phase is entered —
  never client-supplied.
- A choice is permanent once submitted: it is appended to
  HeistRunState.events and the run advances; there is no "change your
  choice" action. This is enforced by heist_service (it only accepts a
  choice for the CURRENT unresolved event of the CURRENT phase).
- A choice may cause immediate failure (ChoiceDefinition.instant_fail).
  When that happens the run transitions straight to FAILED and no further
  phases/rolls occur, regardless of the eventual success-chance math.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from game.locations import events_for_phase
from game.models import Approach, ChoiceDefinition, EventDefinition, HeistState


class NoEligibleEventError(RuntimeError):
    """Raised if a phase has zero eligible, not-yet-used events. This is a
    v1 content-authoring bug (not enough events defined for some
    location/approach/phase combination), not a player-facing condition."""


def select_event(
    phase: HeistState,
    location_key: str,
    approach: Approach,
    already_used_keys: set[str],
    rng: random.Random | None = None,
) -> EventDefinition:
    rng = rng or random.Random()
    candidates = [ev for ev in events_for_phase(phase, location_key, approach) if ev.key not in already_used_keys]
    if not candidates:
        raise NoEligibleEventError(f"no eligible event for phase={phase} location={location_key} approach={approach}")
    return rng.choice(candidates)


@dataclass(frozen=True)
class ChoiceResolution:
    choice: ChoiceDefinition
    instant_fail: bool


def resolve_event_choice(event: EventDefinition, choice_key: str) -> ChoiceResolution:
    for choice in event.choices:
        if choice.key == choice_key:
            return ChoiceResolution(choice=choice, instant_fail=choice.instant_fail)
    raise ValueError(f"invalid choice_key {choice_key!r} for event {event.key!r}")


def next_phase(current: HeistState) -> HeistState:
    order = [HeistState.PLANNING, HeistState.INFILTRATION, HeistState.OBJECTIVE, HeistState.LOOT, HeistState.ESCAPE]
    idx = order.index(current)
    if idx + 1 >= len(order):
        raise ValueError(f"{current} has no next phase")
    return order[idx + 1]
