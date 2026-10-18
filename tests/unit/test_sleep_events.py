import pytest

from discord_bot.cogs._adaptive_skip import AdaptiveSkip
from modules import sleep_events as se


@pytest.fixture(autouse=True)
def _clean():
    se._queue.clear()
    se._last_sent.clear()
    se.reset_digest()
    yield
    se._queue.clear()
    se._last_sent.clear()


def test_named_gate_reports_sleep_once_and_wake_once():
    g = AdaptiveSkip(idle_threshold=2, idle_skip=1, max_skip=4, name="giveaways")
    for _ in range(40):
        if g.should_run():
            g.record(found_something=False)
    kinds = [k for k, _ in se.drain()]
    assert kinds == ["sleep"]                       # escalating sleep does not repeat the notice
    g.wake()
    assert [k for k, _ in se.drain()] == ["wake"]
    g.wake()
    assert se.drain() == []                         # waking an awake job says nothing


def test_unnamed_gate_stays_silent():
    g = AdaptiveSkip(idle_threshold=1, idle_skip=1)
    for _ in range(5):
        if g.should_run():
            g.record(found_something=False)
    g.wake()
    assert se.pending() == 0


def test_flapping_is_held_back_but_counted():
    g = AdaptiveSkip(idle_threshold=1, idle_skip=1, name="ship")
    for _ in range(3):
        g.record(found_something=False)
        g.record(found_something=True)
    assert [k for k, _ in se.drain()] == ["sleep", "wake"]
    assert se.counts["suppressed"] >= 4


def test_queue_is_bounded_and_switch_off(monkeypatch):
    for i in range(500):
        se.emit("dormant", str(i), f"x{i}")
    assert se.pending() == se.MAX_QUEUE
    monkeypatch.setenv("SLEEP_LOG", "0")
    se._queue.clear()
    se.emit("sleep", "zzz", "nope")
    assert se.pending() == 0


def test_labels_and_digest_reset():
    assert se.label("cat_spawner") == "Cat spawner" and se.label("unknown_x") == "unknown_x"
    se.emit("sleep", "a", "t")
    assert se.reset_digest()["sleep"] == 1 and se.counts["sleep"] == 0
