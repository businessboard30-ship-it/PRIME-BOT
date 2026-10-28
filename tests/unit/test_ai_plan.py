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
