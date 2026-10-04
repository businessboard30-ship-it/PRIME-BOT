import random

from modules.catch_setup import CatchSetup
from modules.catch_trigger import ChannelTriggerState, consider_message, timer_fallback_due

def setup():
    return CatchSetup(enabled=True, spawn_channel_ids=(10,), spawn_every_n_messages=2, min_seconds_between_spawns=30)

def test_ignores_bots_short_messages_and_wrong_channels():
    state = ChannelTriggerState()
    assert consider_message(setup(), state, channel_id=10, user_id=1, content="hello", is_bot=True, now=0).reason == "bot"
    assert consider_message(setup(), state, channel_id=10, user_id=1, content="x", now=1).reason == "message-too-short"
    assert consider_message(setup(), state, channel_id=11, user_id=1, content="hello", now=2).reason == "channel-disabled"

def test_counts_human_messages_and_applies_user_cooldown():
    state = ChannelTriggerState()
    rng = random.Random(5)
    assert not consider_message(setup(), state, channel_id=10, user_id=1, content="hello", now=0, rng=rng).should_spawn
    assert consider_message(setup(), state, channel_id=10, user_id=1, content="again", now=1, rng=rng).reason == "user-rate-limited"
    decision = consider_message(setup(), state, channel_id=10, user_id=2, content="again", now=2, rng=rng)
    assert decision.should_spawn
    assert state.message_count == 0

def test_minimum_gap_blocks_then_allows_next_spawn():
    state = ChannelTriggerState()
    rng = random.Random(1)
    consider_message(setup(), state, channel_id=10, user_id=1, content="one", now=0, rng=rng)
    assert consider_message(setup(), state, channel_id=10, user_id=2, content="two", now=1, rng=rng).should_spawn
    assert not consider_message(setup(), state, channel_id=10, user_id=3, content="three", now=2, rng=rng).should_spawn
    assert consider_message(setup(), state, channel_id=10, user_id=4, content="four", now=31, rng=rng).should_spawn

def test_timer_fallback_is_quiet_only():
    state = ChannelTriggerState(last_message_at=10)
    assert not timer_fallback_due(state, now=100, quiet_seconds=100)
    assert timer_fallback_due(state, now=110, quiet_seconds=100)
