"""
Heist Wars — static v1 content (locations + events).

This is deliberately just data. The original spec did not enumerate exact
locations/events/numbers, so v1 ships a small, hand-picked, conservative
set (4 locations, 6 events) rather than inventing a large content system.
Adding more later is additive — no code changes required beyond appending
to these two collections, since heist_service resolves everything by key.
"""

from __future__ import annotations

from game.models import Approach, ChoiceDefinition, EventDefinition, HeistState, LocationDefinition

LOCATIONS: dict[str, LocationDefinition] = {
    loc.key: loc
    for loc in [
        LocationDefinition(
            key="convenience_store",
            name="Convenience Store",
            required_level=1,
            difficulty_penalty=5,
            base_chance=70,
            min_reward_cash=50,
            max_reward_cash=200,
            min_reward_xp=10,
            max_reward_xp=25,
            cooldown_seconds=30 * 60,
        ),
        LocationDefinition(
            key="jewelry_store",
            name="Jewelry Store",
            required_level=5,
            difficulty_penalty=20,
            base_chance=55,
            min_reward_cash=300,
            max_reward_cash=900,
            min_reward_xp=40,
            max_reward_xp=80,
            cooldown_seconds=2 * 60 * 60,
        ),
        LocationDefinition(
            key="bank_vault",
            name="Bank Vault",
            required_level=12,
            difficulty_penalty=35,
            base_chance=45,
            min_reward_cash=1000,
            max_reward_cash=3000,
            min_reward_xp=100,
            max_reward_xp=200,
            cooldown_seconds=6 * 60 * 60,
        ),
        LocationDefinition(
            key="casino_vault",
            name="Casino Vault",
            required_level=20,
            difficulty_penalty=45,
            base_chance=40,
            min_reward_cash=2500,
            max_reward_cash=7000,
            min_reward_xp=200,
            max_reward_xp=400,
            cooldown_seconds=12 * 60 * 60,
        ),
    ]
}


EVENTS: dict[str, EventDefinition] = {
    ev.key: ev
    for ev in [
        EventDefinition(
            key="security_checkpoint",
            phase=HeistState.INFILTRATION,
            description="A guard is patrolling near the only entrance.",
            choices=(
                ChoiceDefinition("wait", "Wait for the patrol to pass", success_modifier=-2),
                ChoiceDefinition("reroute", "Reroute through the service hall", success_modifier=3),
                ChoiceDefinition("bribe", "Bribe the guard", success_modifier=6),
            ),
        ),
        EventDefinition(
            key="camera_loop",
            phase=HeistState.INFILTRATION,
            description="A CCTV camera sweeps the corridor every 8 seconds.",
            choices=(
                ChoiceDefinition("time_it", "Time your run between sweeps", success_modifier=4),
                ChoiceDefinition("disable", "Disable the camera", success_modifier=-3),
            ),
        ),
        EventDefinition(
            key="locked_safe",
            phase=HeistState.OBJECTIVE,
            description="The safe is a model you don't recognize.",
            choices=(
                ChoiceDefinition("crack", "Crack it manually", success_modifier=-4),
                ChoiceDefinition("drill", "Drill it", success_modifier=2),
                ChoiceDefinition("override", "Override the electronic lock", success_modifier=5,),
            ),
        ),
        EventDefinition(
            key="silent_alarm",
            phase=HeistState.OBJECTIVE,
            description="You spot a silent alarm trigger under the counter.",
            choices=(
                ChoiceDefinition("disarm", "Carefully disarm it", success_modifier=3),
                ChoiceDefinition("ignore", "Ignore it and hope", success_modifier=-6),
                ChoiceDefinition("trigger", "Cut the wire blind", success_modifier=0, instant_fail=True),
            ),
        ),
        EventDefinition(
            key="heavy_loot",
            phase=HeistState.LOOT,
            description="There's more here than you planned for.",
            choices=(
                ChoiceDefinition("take_all", "Take everything", success_modifier=-5),
                ChoiceDefinition("take_priority", "Grab the priority items only", success_modifier=4),
            ),
        ),
        EventDefinition(
            key="second_guard",
            phase=HeistState.LOOT,
            description="A second guard is doing an off-schedule check.",
            choices=(
                ChoiceDefinition("hide", "Hide until they pass", success_modifier=2),
                ChoiceDefinition("rush", "Rush past them", success_modifier=-4),
            ),
        ),
    ]
}


def get_location(key: str) -> LocationDefinition | None:
    return LOCATIONS.get(key)


def get_event(key: str) -> EventDefinition | None:
    return EVENTS.get(key)


def events_for_phase(phase: HeistState, location_key: str, approach: Approach) -> list[EventDefinition]:
    """Events eligible for a given phase/location/approach. An event with
    an empty location_keys/approach_keys tuple applies everywhere."""
    out = []
    for ev in EVENTS.values():
        if ev.phase != phase:
            continue
        if ev.location_keys and location_key not in ev.location_keys:
            continue
        if ev.approach_keys and approach not in ev.approach_keys:
            continue
        out.append(ev)
    return out
