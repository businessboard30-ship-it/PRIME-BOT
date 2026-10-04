"""Cheap, deterministic trigger policy for wild catch spawns (P2-01)."""
from __future__ import annotations

from dataclasses import dataclass, field
import random
import time

from modules.catch_setup import CatchSetup

MIN_MESSAGE_LENGTH = 3
DEFAULT_USER_COOLDOWN_SECONDS = 20.0

@dataclass
class ChannelTriggerState:
    message_count: int = 0
    last_spawn_at: float | None = None
    last_message_at: float | None = None
    user_last_message_at: dict[int, float] = field(default_factory=dict)

@dataclass(frozen=True)
class TriggerDecision:
    should_spawn: bool
    reason: str
    channel_id: int
    message_count: int
    next_due_at: float | None = None

def _jitter(rng: random.Random, base: float) -> float:
    return base * rng.uniform(0.85, 1.15)

def consider_message(setup: CatchSetup, state: ChannelTriggerState, *, channel_id: int, user_id: int, content: str, is_bot: bool = False, now: float | None = None, rng: random.Random | None = None, user_cooldown_seconds: float = DEFAULT_USER_COOLDOWN_SECONDS) -> TriggerDecision:
    now = time.monotonic() if now is None else now
    rng = rng or random.Random()
    if is_bot:
        return TriggerDecision(False, "bot", channel_id, state.message_count)
    if not setup.enabled or channel_id not in setup.spawn_channel_ids:
        return TriggerDecision(False, "channel-disabled", channel_id, state.message_count)
    if len(content.strip()) < MIN_MESSAGE_LENGTH:
        return TriggerDecision(False, "message-too-short", channel_id, state.message_count)
    previous_user = state.user_last_message_at.get(user_id)
    if previous_user is not None and now - previous_user < user_cooldown_seconds:
        return TriggerDecision(False, "user-rate-limited", channel_id, state.message_count)
    state.user_last_message_at[user_id] = now
    state.message_count += 1
    state.last_message_at = now
    threshold = max(1, round(_jitter(rng, setup.spawn_every_n_messages)))
    if state.message_count < threshold:
        return TriggerDecision(False, "counter", channel_id, state.message_count, now + setup.min_seconds_between_spawns)
    if state.last_spawn_at is not None and now - state.last_spawn_at < setup.min_seconds_between_spawns:
        return TriggerDecision(False, "minimum-gap", channel_id, state.message_count, state.last_spawn_at + setup.min_seconds_between_spawns)
    state.message_count = 0
    state.last_spawn_at = now
    return TriggerDecision(True, "threshold", channel_id, state.message_count)

def timer_fallback_due(state: ChannelTriggerState, *, now: float | None = None, quiet_seconds: float = 300.0) -> bool:
    now = time.monotonic() if now is None else now
    return state.last_message_at is not None and now - state.last_message_at >= quiet_seconds

__all__ = ["ChannelTriggerState", "TriggerDecision", "consider_message", "timer_fallback_due"]
