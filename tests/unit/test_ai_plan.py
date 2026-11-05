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


def test_on_update_called_per_done_step_and_failure_in_it_is_harmless():
    seen = []

    async def upd(r):
        seen.append(r.count(DONE))
        raise RuntimeError("edit failed")

    async def ex(step):
        pass

    p = run(run_plan(make(3), ex, sleep=_nosleep, on_update=upd))
    assert seen == [1, 2, 3] and p.count(DONE) == 3


def test_progress_text_lists_every_step():
    p = make(2)
    p.steps[0].status = DONE
    txt = p.progress()
    assert "1 done" in txt and "/c1" in txt and "/c2" in txt


# ── Phase 0 of the 20+ task upgrade ─────────────────────────────────────────
def test_step_ceilings_agree_and_allow_twenty_plus():
    from modules import ai_bulk
    assert ai_plan.MAX_STEPS == ai_bulk.MAX_COMMANDS_PER_MESSAGE >= 20


def test_a_twenty_five_step_plan_finishes_with_no_retries():
    p = run(run_plan(make(25), lambda s: _ok(), sleep=_nosleep))
    assert p.count(DONE) == 25 and not p.stopped          # the flat 16-attempt budget used to stop this at step 17


def test_budgets_scale_but_stay_bounded():
    assert ai_plan.attempt_budget(3) == ai_plan.MAX_TOTAL_ATTEMPTS
    assert ai_plan.attempt_budget(25) == 50
    assert ai_plan.attempt_budget(10 ** 6) == 2 * ai_plan.MAX_STEPS
    assert ai_plan.time_budget(3) == ai_plan.MAX_PLAN_SECONDS
    assert ai_plan.MAX_PLAN_SECONDS < ai_plan.time_budget(25) <= ai_plan.MAX_PLAN_SECONDS_CAP < 900


# ── Phase 2: dependency-aware steps ─────────────────────────────────────────
def dmake(spec):
    """spec: [(key, depends_on_or_None)] -> PlanRun"""
    return ai_plan.PlanRun.from_calls([{"name": f"c{i}", "label": f"/c{i}", "args": {"i": i}, "key": k, "depends_on": d}
                                       for i, (k, d) in enumerate(spec, 1)])


def fails(*bad):
    async def ex(step):
        if step.index in bad:
            raise ai_plan.StepFailed("nope")
    return ex


def test_independent_steps_keep_going_after_a_failure():
    p = run(run_plan(dmake([("1", []), ("2", []), ("3", []), ("4", [])]), fails(2), sleep=_nosleep))
    assert [s.status for s in p.steps] == [DONE, FAILED, DONE, DONE]


def test_only_dependents_are_skipped_including_transitively():
    p = run(run_plan(dmake([("1", []), ("2", ["1"]), ("3", ["2"]), ("4", []), ("5", ["4"])]), fails(1), sleep=_nosleep))
    assert [s.status for s in p.steps] == [FAILED, SKIPPED, SKIPPED, DONE, DONE]


def test_unknown_dependencies_stay_fail_fast_like_before():
    p = run(run_plan(dmake([("1", []), ("2", None), ("3", None), ("4", [])]), fails(1), sleep=_nosleep))
    assert [s.status for s in p.steps] == [FAILED, SKIPPED, SKIPPED, DONE]       # step 4 declared itself independent


def test_retry_reruns_failed_and_skipped_only_then_dependents_follow():
    p = dmake([("1", []), ("2", ["1"]), ("3", [])])
    ran = []

    async def ex(step):
        ran.append(step.index)
        if step.index == 1 and ran.count(1) == 1:
            raise ai_plan.StepFailed("first time")
    run(run_plan(p, ex, sleep=_nosleep))
    assert [s.status for s in p.steps] == [FAILED, SKIPPED, DONE]
    assert p.prepare_retry()
    run(run_plan(p, ex, sleep=_nosleep))
    assert [s.status for s in p.steps] == [DONE, DONE, DONE] and ran.count(3) == 1       # the done step never ran twice


def test_link_dependencies_is_conservative():
    out = ai_plan.link_dependencies(
        [("1", []), ("2", ["1"]), ("3", ["9"]), ("4", ["7"]), ("5", ["8"]), ("6", None)],
        dropped={"9"}, satisfied={"7"}, plan_keys={"1", "2", "3", "4", "5", "6"})
    assert out[0] == ([], False)
    assert out[1] == (["1"], False)
    assert out[2] == ([], True)                 # needs a refused task: pre-skipped
    assert out[3] == ([], False)                # needs a read-only step that already ran: satisfied
    assert out[4] == (None, False)              # needs a task we can't place anywhere: unknown -> fail-fast
    assert out[5] == (None, False)


def test_unplaceable_dependency_becomes_unknown_not_satisfied():
    out = ai_plan.link_dependencies([("1", ["42"])], dropped=set(), satisfied=set(), plan_keys={"1"})
    assert out == [(None, False)]               # never 'fine': it falls back to fail-fast


# ── Phase 3: throttled progress, windowed progress, paged report, retry failed ──
def test_edit_throttle_spaces_edits_and_never_blocks_the_final_one():
    now = [100.0]
    t = ai_plan.EditThrottle(interval=2.5, clock=lambda: now[0])
    assert t.ready() is True                      # first edit goes
    now[0] += 1.0
    assert t.ready() is False                     # too soon
    now[0] += 1.6
    assert t.ready() is True                      # 2.6s later
    now[0] += 0.1
    assert t.ready() is False and t.ready(force=True) is True


def test_a_20_step_plan_makes_far_fewer_progress_edits_than_steps():
    run_ = make(20)
    now = [0.0]
    throttle = ai_plan.EditThrottle(interval=2.5, clock=lambda: now[0])
    edits = []

    async def ex(step):
        now[0] += 0.8                              # the cog pauses ~0.8s between steps

    async def on_update(r):
        if throttle.ready():
            edits.append(r.count(DONE))

    run(run_plan(run_, ex, on_update=on_update))
    assert run_.count(DONE) == 20 and 4 <= len(edits) <= 8


def test_progress_of_a_big_plan_is_windowed_and_short():
    r = make(25)
    for s in r.steps[:14]:
        s.status = DONE
    text = r.progress()
    assert len(text) < 1900 and text.count("\n") <= ai_plan.PROGRESS_WINDOW
    assert "14. /c14" in text and "25. /c25" not in text and "shown" in text
    assert "1. /c1\n" in make(5).progress() or "1. /c1" in make(5).progress()      # a small plan is shown whole


def test_report_pages_hold_every_step_within_the_message_limit():
    r = make(25)
    for s in r.steps:
        s.status, s.detail = FAILED, "Discord refused: the bot is missing a permission or its role is too low"
    pages = r.report_pages(limit=500)
    assert len(pages) > 1 and all(len(p) <= 500 for p in pages)
    joined = "\n".join(pages)
    assert all(f"{i}. /c{i}" in joined for i in range(1, 26))
    assert pages[0].startswith("Plan finished: 0 done, 25 failed.")
    assert r.report().count("\n") == 25            # report() stays the one-string form


def test_failed_steps_lists_only_failures():
    r = make(4)
    r.steps[0].status, r.steps[1].status, r.steps[2].status = DONE, FAILED, SKIPPED
    assert [s.index for s in r.failed_steps()] == [2]


# ── dependencies ──
def calls(deps):
    return [{"name": f"c{i}", "label": f"/c{i}", "args": {"i": i}, "key": str(i), "depends_on": d}
            for i, d in enumerate(deps, 1)]


def test_failure_skips_only_its_dependents_across_a_20_step_plan():
    deps = [[]] + [["1"] if i % 2 else [] for i in range(2, 21)]       # odd steps need step 1; even steps are independent
    r = PlanRun.from_calls(calls(deps))
    ran = []

    async def ex(step):
        ran.append(step.index)
        if step.index == 1:
            raise StepFailed("nope")

    run(run_plan(r, ex, sleep=_nosleep))
    assert r.steps[0].status == FAILED
    for s in r.steps[1:]:
        assert s.status == (SKIPPED if s.index % 2 else DONE), s.index
    assert ran == [1] + [i for i in range(2, 21) if i % 2 == 0]


def test_a_skipped_step_skips_what_depends_on_it():
    r = PlanRun.from_calls(calls([[], ["1"], ["2"], []]))

    async def ex(step):
        if step.index == 1:
            raise StepFailed("nope")

    run(run_plan(r, ex, sleep=_nosleep))
    assert [s.status for s in r.steps] == [FAILED, SKIPPED, SKIPPED, DONE]


def test_retry_after_a_dependency_failure_runs_only_failed_and_skipped():
    r = PlanRun.from_calls(calls([[], ["1"], []]))
    seen = []
    state = {"fail": True}

    async def ex(step):
        seen.append(step.index)
        if step.index == 1 and state["fail"]:
            raise StepFailed("nope", transient=False)

    run(run_plan(r, ex, sleep=_nosleep))
    state["fail"] = False
    assert r.prepare_retry()
    run(run_plan(r, ex, sleep=_nosleep))
    assert seen == [1, 3, 1, 2] and all(s.status == DONE for s in r.steps)       # step 3 never ran twice
