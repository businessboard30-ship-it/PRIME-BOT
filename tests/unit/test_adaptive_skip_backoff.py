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


def test_verification_poll_sleeps_only_after_link_window_and_wakes_on_link():
    from discord_bot.cogs.verification import VerificationCog

    cog = VerificationCog.__new__(VerificationCog)
    VerificationCog.__init__(cog, bot=object())
    ran = _run(cog._gate, 120)
    assert len(ran) == 120                     # full speed for the 10-minute link window
    for _ in range(40):
        if cog._gate.should_run():
            cog._gate.record(found_something=False)
    assert cog._gate.sleeping or cog._gate._next_skip > 3
    cog.wake_poll()
    assert not cog._gate.sleeping and cog._gate.should_run()


def test_verification_idle_check_is_never_slower_than_10_seconds():
    from discord_bot.cogs.verification import VerificationCog

    cog = VerificationCog.__new__(VerificationCog)
    VerificationCog.__init__(cog, bot=object())
    _run(cog._gate, 120)                       # burn the 10-minute link window
    ran = _run(cog._gate, 200)                 # 5 s ticks afterwards
    gaps = {b - a for a, b in zip(ran, ran[1:])}
    assert gaps == {2}                         # every other tick = every 10 s, forever (no escalation)


def test_captcha_button_wakes_the_cog_by_its_real_name():
    import inspect
    from discord_bot.cogs import _views_verification
    from discord_bot.cogs.verification import VerificationCog

    assert VerificationCog.__cog_name__ == "VerificationCog"
    assert 'get_cog("VerificationCog").wake_poll()' in inspect.getsource(_views_verification)
