from discord_bot.cogs._adaptive_skip import AdaptiveSkip


def _run(gate, ticks, found=lambda i: False):
    ran = []
    for i in range(ticks):
        if gate.should_run():
            ran.append(i)
            gate.record(found_something=found(i))
    return ran


def test_old_behaviour_unchanged_without_max_skip():
    g = AdaptiveSkip(idle_threshold=2, idle_skip=3)
    assert _run(g, 12) == [0, 1, 5, 9]


def test_escalating_sleep_doubles_up_to_max():
    g = AdaptiveSkip(idle_threshold=2, idle_skip=1, max_skip=4)
    ran = _run(g, 30)
    gaps = [b - a for a, b in zip(ran, ran[1:])]
    assert gaps[:4] == [1, 2, 3, 5] and max(gaps) == 5   # skips 1,2,4,4 -> gaps 2.. capped


def test_work_wakes_it_up_instantly():
    g = AdaptiveSkip(idle_threshold=1, idle_skip=1, max_skip=8)
    _run(g, 20)
    g.wake()
    assert not g.sleeping and g.should_run()
    g.record(found_something=False)
    assert g.sleeping and g._skip_remaining == 1   # escalation restarted from the base skip


def test_found_work_resets_streak():
    g = AdaptiveSkip(idle_threshold=2, idle_skip=2, max_skip=8)
    _run(g, 10, found=lambda i: True)
    assert g.should_run()
