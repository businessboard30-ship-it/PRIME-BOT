import logging

from modules.log_throttle import ThrottleFilter


def _rec(msg, level=logging.ERROR, name="x", exc=None):
    return logging.LogRecord(name, level, "f.py", 1, msg, None, exc)


def test_repeats_inside_window_are_dropped_then_summarised():
    t = [0.0]
    f = ThrottleFilter(window=60, clock=lambda: t[0])
    assert f.filter(_rec("db down"))
    for _ in range(5):
        t[0] += 5
        assert not f.filter(_rec("db down"))
    t[0] = 61
    r = _rec("db down")
    assert f.filter(r)
    assert "+5 similar suppressed" in r.msg


def test_info_and_different_messages_always_pass():
    f = ThrottleFilter(window=60, clock=lambda: 0.0)
    for _ in range(3):
        assert f.filter(_rec("hello", logging.INFO))
    assert f.filter(_rec("a")) and f.filter(_rec("b")) and f.filter(_rec("a", name="other"))


def test_different_exception_types_are_separate():
    f = ThrottleFilter(window=60, clock=lambda: 0.0)
    assert f.filter(_rec("boom", exc=(ValueError, ValueError("x"), None)))
    assert f.filter(_rec("boom", exc=(KeyError, KeyError("x"), None)))
    assert not f.filter(_rec("boom", exc=(ValueError, ValueError("y"), None)))


def test_memory_is_bounded_and_never_raises():
    f = ThrottleFilter(window=1, clock=iter(range(10**6)).__next__)
    for i in range(2000):
        assert f.filter(_rec(f"m{i}"))
    assert len(f._seen) <= 501
    assert f.filter(object())   # junk record: fail open
