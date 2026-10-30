"""Phase 1 plan runner: order, fail-fast skipping, no repeat of finished steps, bounded retry, stop_all."""
import asyncio

from modules import ai_plan
from modules.ai_plan import DONE, FAILED, PENDING, SKIPPED, PlanRun, StepFailed, run_plan


def run(coro):
    return asyncio.run(coro)


def make(n=4):
    return PlanRun.from_calls([{"name": f"c{i}", "label": f"/c{i}", "args": {"i": i}} for i in range(1, n + 1)])


async def _nosleep(_):
    return None


def test_runs_in_order_and_all_done():
    seen = []

    async def ex(step):
        seen.append(step.name)

    p = run(run_plan(make(3), ex, sleep=_nosleep))
    assert seen == ["c1", "c2", "c3"] and p.count(DONE) == 3 and not p.retryable()


def test_failure_skips_later_steps():
    async def ex(step):
        if step.name == "c2":
            raise StepFailed("no permission")

    p = run(run_plan(make(4), ex, sleep=_nosleep))
    assert [s.status for s in p.steps] == [DONE, FAILED, SKIPPED, SKIPPED]
    assert "step 2 failed first" in p.steps[2].detail
    assert "no permission" in p.report() and p.retryable()


def test_retry_never_reruns_done_steps():
    calls = []
    fail_once = {"c2"}

    async def ex(step):
        calls.append(step.name)
        if step.name in fail_once:
            fail_once.discard(step.name)
            raise StepFailed("transient")

    p = run(run_plan(make(3), ex, sleep=_nosleep))
    assert p.prepare_retry()
    run(run_plan(p, ex, sleep=_nosleep))
    assert calls == ["c1", "c2", "c2", "c3"]          # c1 ran exactly once
    assert p.count(DONE) == 3


def test_retry_is_bounded():
    async def ex(step):
        raise StepFailed("permanent")

    p = run(run_plan(make(2), ex, sleep=_nosleep))
    for _ in range(ai_plan.MAX_RETRY_ROUNDS):
        assert p.prepare_retry()
        run(run_plan(p, ex, sleep=_nosleep))
    assert not p.retryable() and not p.prepare_retry()


def test_stop_all_blocks_retry_and_marks_rest_not_reached():
    async def ex(step):
        if step.name == "c1":
            raise StepFailed("daily limit reached", stop_all=True)

    p = run(run_plan(make(3), ex, sleep=_nosleep))
    assert p.stopped and not p.retryable()
    assert p.steps[0].status == FAILED and p.steps[1].status in (SKIPPED, PENDING)


def test_timeout_and_unexpected_error_count_as_failed():
    async def slow(step):
        await asyncio.sleep(5)

    p = run(run_plan(make(2), slow, timeout=0.05, sleep=_nosleep))
    assert p.steps[0].status == FAILED and "timed out" in p.steps[0].detail and p.steps[1].status == SKIPPED

    async def boom(step):
        raise ValueError("x")

    p = run(run_plan(make(2), boom, sleep=_nosleep))
    assert p.steps[0].status == FAILED and "unexpected error" in p.steps[0].detail


def test_pause_between_steps_only():
    pauses = []

    async def sl(t):
        pauses.append(t)

    async def ex(step):
        pass

    run(run_plan(make(3), ex, pause=0.8, sleep=sl))
    assert pauses == [0.8, 0.8]


# ---- Phase 2: bounded self-correction -------------------------------------------------------------------------

def test_transient_error_is_retried_then_succeeds():
    n = {"c": 0}
    slept = []

    async def sl(t):
        slept.append(t)

    async def ex(step):
        n["c"] += 1
        if n["c"] < 3:
            raise StepFailed("rate limit", transient=True)

    p = run(run_plan(make(1), ex, sleep=sl))
    assert p.steps[0].status == DONE and p.steps[0].attempts == 3 and slept == [2.0, 4.0]


def test_transient_gives_up_after_bound_and_says_so():
    async def ex(step):
        raise StepFailed("5xx", transient=True)

    p = run(run_plan(make(2), ex, sleep=_nosleep))
    assert p.steps[0].status == FAILED and p.steps[0].attempts == 1 + ai_plan.MAX_AUTO_RETRIES
    assert "gave up after 2 automatic retries" in p.steps[0].detail and p.steps[1].status == SKIPPED


def test_retry_after_is_honoured_and_capped():
    slept = []

    async def sl(t):
        slept.append(t)

    async def ex(step):
        raise StepFailed("429", transient=True, retry_after=999)

    run(run_plan(make(1), ex, sleep=sl))
    assert slept[0] == ai_plan.BACKOFF_CAP


def test_permanent_error_is_not_auto_retried_and_identical_action_not_repeated():
    calls = []

    async def ex(step):
        calls.append(step.name)
        raise StepFailed("missing permission")

    p = run(run_plan(make(1), ex, sleep=_nosleep))
    assert calls == ["c1"] and p.steps[0].attempts == 1
    # an identical action later in the same run is not executed again
    twin = PlanRun.from_calls([{"name": "x", "args": {"a": 1}}, {"name": "x", "args": {"a": 1}}])
    twin.steps[0].status = ai_plan.FAILED
    twin.dead_actions.add(twin.steps[0].fingerprint())
    run(run_plan(twin, ex, sleep=_nosleep))
    assert calls == ["c1"] and "not repeating" in twin.steps[1].detail
    # but a manual retry after the person fixed the cause DOES try again
    assert p.prepare_retry()
    run(run_plan(p, ex, sleep=_nosleep))
    assert calls == ["c1", "c1"]


def test_cancel_stops_before_next_step():
    async def ex(step):
        if step.name == "c1":
            p.cancel.set()

    p = make(3)
    run(run_plan(p, ex, sleep=_nosleep))
    assert p.count(DONE) == 1 and p.stopped == "cancelled" and not p.retryable()


def test_attempt_budget_is_a_hard_ceiling():
    async def ex(step):
        raise StepFailed("flaky", transient=True)

    p = make(1)
    p.total_attempts = ai_plan.MAX_TOTAL_ATTEMPTS - 1
    run(run_plan(p, ex, sleep=_nosleep))
    assert p.total_attempts == ai_plan.MAX_TOTAL_ATTEMPTS and "attempt budget" in p.stopped


def test_time_budget_stops_plan():
    t = {"now": 0.0}

    async def ex(step):
        t["now"] += ai_plan.MAX_PLAN_SECONDS + 1

    p = run(run_plan(make(3), ex, sleep=_nosleep, clock=lambda: t["now"]))
    assert p.count(DONE) == 1 and "time budget" in p.stopped and p.count(PENDING) == 2


def test_over_step_limit_is_skipped():
    p = run(run_plan(make(ai_plan.MAX_STEPS + 2), lambda s: _ok(), sleep=_nosleep))
    assert p.count(DONE) == ai_plan.MAX_STEPS and p.count(SKIPPED) == 2


async def _ok():
    return None


def test_classify_http_status():
    assert ai_plan.classify_http_status(429, 3.0).transient and ai_plan.classify_http_status(503).transient
    assert not ai_plan.classify_http_status(403).transient and "permission" in str(ai_plan.classify_http_status(403))
    assert not ai_plan.classify_http_status(404).transient
