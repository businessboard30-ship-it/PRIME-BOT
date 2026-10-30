# path: modules/ai_plan.py

"""
Phase 1 of the autonomous-AI plan: run several dependent AI commands as ONE tracked plan.

Pure logic only (no Discord), so it is unit-testable. The cog (discord_bot/cogs/ai_tools.py) supplies the executor.

Rules this enforces:
  * steps run strictly in the order given, one at a time (never gathered), with a pause between them;
  * a step that FAILS makes every later step SKIPPED ("an earlier step failed"): later steps may depend on it
    (e.g. channels that go inside a category the failed step should have created), so nothing runs half-blind;
  * a step that already succeeded is NEVER run again -- not on retry, not on resume;
  * a retry only re-runs failed + skipped steps, and is capped so a permanent error cannot loop forever;
  * a daily-cap hit stops the whole plan (stop_all) -- nothing after it could run either;
  * the report says exactly what was done, failed, skipped, and what was never reached.

Permissions, role hierarchy and the daily cap are NOT checked here: the executor (execute_ai_command ->
resolve_and_check) re-checks them at the moment each step runs.
"""

import asyncio
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, List, Optional

PENDING, DONE, FAILED, SKIPPED = "pending", "done", "failed", "skipped"
MAX_RETRY_ROUNDS = 2          # "Retry remaining" can be used this many times per plan
STEP_TIMEOUT = 120            # seconds one step may take before it counts as failed
MAX_STEPS = 10                # hard ceiling on steps in one plan (matches ai_bulk.MAX_COMMANDS_PER_MESSAGE)
MAX_AUTO_RETRIES = 2          # automatic re-tries of ONE step, transient errors only
MAX_TOTAL_ATTEMPTS = 16       # every execution attempt across the plan, retries included
MAX_PLAN_SECONDS = 600        # wall-clock budget for one run_plan call
BACKOFF_BASE = 2.0            # seconds; doubles per automatic retry, capped below
BACKOFF_CAP = 30.0
_ICON = {DONE: "\u2705", FAILED: "\u274c", SKIPPED: "\u23ed\ufe0f", PENDING: "\u23f8\ufe0f"}


class StepFailed(Exception):
    """Raised by an executor when a step did not complete. `stop_all` ends the whole plan (e.g. daily cap hit)."""

    def __init__(self, message: str, stop_all: bool = False, transient: bool = False, retry_after: float = 0.0):
        super().__init__(message)
        self.stop_all = stop_all
        self.transient = transient            # worth an automatic re-try (rate limit, 5xx, network blip)
        self.retry_after = retry_after        # seconds the server asked us to wait, if any


def classify_http_status(status: Optional[int], retry_after: float = 0.0, text: str = "") -> StepFailed:
    """Turns a Discord/HTTP status into a StepFailed with a plain-English reason. Only 429 and 5xx are transient."""
    if status == 429:
        return StepFailed("Discord rate limit", transient=True, retry_after=retry_after)
    if status is not None and status >= 500:
        return StepFailed(f"Discord had a temporary problem ({status})", transient=True)
    if status == 403:
        return StepFailed("Discord refused: the bot is missing a permission or its role is too low")
    if status == 404:
        return StepFailed("the target no longer exists")
    return StepFailed(("Discord rejected it: " + text).strip(": ")[:160] if text else "Discord rejected the request")


@dataclass
class PlanStep:
    index: int
    name: str                   # command name the AI chose
    label: str                  # what the person sees, e.g. "/role create"
    args: dict = field(default_factory=dict)
    status: str = PENDING
    detail: str = ""
    attempts: int = 0

    def fingerprint(self) -> str:
        return self.name + "|" + repr(sorted(self.args.items(), key=lambda kv: kv[0]))


class PlanRun:
    def __init__(self, steps: List[PlanStep]):
        self.steps = steps
        self.retry_rounds = 0
        self.stopped = ""                     # non-empty when the plan was ended early (e.g. daily limit)
        self.total_attempts = 0
        self.cancel = asyncio.Event()         # set() to stop after the current step
        self.dead_actions = set()             # fingerprints that failed PERMANENTLY: never re-run within one run

    @classmethod
    def from_calls(cls, calls: List[dict]) -> "PlanRun":
        """calls: [{'name', 'label', 'args'}] in the order to run."""
        return cls([PlanStep(i, c["name"], c.get("label") or c["name"], dict(c.get("args") or {}))
                    for i, c in enumerate(calls, 1)])

    def count(self, status: str) -> int:
        return sum(1 for s in self.steps if s.status == status)

    def retryable(self) -> bool:
        return (self.retry_rounds < MAX_RETRY_ROUNDS and not self.stopped
                and any(s.status in (FAILED, SKIPPED) for s in self.steps))

    def prepare_retry(self) -> bool:
        """Queue only failed + skipped steps again. Done steps stay done. False if a retry isn't allowed."""
        if not self.retryable():
            return False
        self.retry_rounds += 1
        self.dead_actions.clear()             # the person may have fixed the cause (granted a permission) before retrying
        for s in self.steps:
            if s.status in (FAILED, SKIPPED):
                s.status, s.detail = PENDING, ""
        return True

    def report(self) -> str:
        lines = [f"{_ICON[s.status]} {s.index}. {s.label}" + (f" -- {s.detail}" if s.detail else "") for s in self.steps]
        parts = [f"{self.count(DONE)} done"]
        for status in (FAILED, SKIPPED, PENDING):
            n = self.count(status)
            if n:
                parts.append(f"{n} {'not reached' if status == PENDING else status}")
        head = "Plan finished: " + ", ".join(parts) + "."
        if self.stopped:
            head += f" Stopped early: {self.stopped}"
        elif self.retryable():
            head += " Nothing that already succeeded will run again if you retry."
        return head + "\n" + "\n".join(lines)


async def run_plan(run: PlanRun, executor: Callable[[PlanStep], Awaitable[None]], *, pause: float = 0.0,
                   timeout: float = STEP_TIMEOUT, sleep=asyncio.sleep, clock=time.monotonic) -> PlanRun:
    """Runs every PENDING step in order. DONE steps are skipped over untouched (that is what makes retry safe).

    Bounded on every axis: steps, total attempts, wall-clock time, per-step automatic retries. Only transient errors
    are re-tried automatically (with backoff, honouring Retry-After). A permanent error is never repeated."""
    started = clock()
    broken: Optional[PlanStep] = None
    first = True
    if len(run.steps) > MAX_STEPS:
        for step in run.steps[MAX_STEPS:]:
            if step.status == PENDING:
                step.status, step.detail = SKIPPED, f"over the {MAX_STEPS}-step limit"
    for step in run.steps:
        if step.status != PENDING:
            continue
        if broken is not None:
            step.status, step.detail = SKIPPED, f"not run, step {broken.index} failed first"
            continue
        if run.stopped:
            continue                                # stays PENDING -> reported as "not reached"
        if run.cancel.is_set():
            run.stopped = "cancelled"
            continue
        if clock() - started > MAX_PLAN_SECONDS:
            run.stopped = f"time budget ({MAX_PLAN_SECONDS}s) used up"
            continue
        if step.fingerprint() in run.dead_actions:
            step.status, step.detail = FAILED, "same action already failed for good earlier in this run; not repeating it"
            broken = step
            continue
        if not first and pause:
            await sleep(pause)
        first = False
        auto = 0
        while True:
            if run.total_attempts >= MAX_TOTAL_ATTEMPTS:
                step.status, step.detail = FAILED, "attempt budget used up"
                run.stopped = step.detail
                broken = step
                break
            run.total_attempts += 1
            step.attempts += 1
            try:
                await asyncio.wait_for(executor(step), timeout)
                step.status, step.detail = DONE, ""
                break
            except StepFailed as exc:
                if exc.transient and not exc.stop_all and auto < MAX_AUTO_RETRIES and not run.cancel.is_set():
                    auto += 1
                    await sleep(min(max(exc.retry_after, BACKOFF_BASE * (2 ** (auto - 1))), BACKOFF_CAP))
                    continue
                step.status = FAILED
                step.detail = str(exc)[:200] + (f" (gave up after {auto} automatic retries)" if auto else "")
                if not exc.transient:
                    run.dead_actions.add(step.fingerprint())
                if exc.stop_all:
                    run.stopped = step.detail
                broken = step
                break
            except asyncio.TimeoutError:
                step.status, step.detail = FAILED, f"timed out after {int(timeout)}s"
                broken = step
                break
            except Exception as exc:                # unknown error: stop, don't guess and push on
                step.status, step.detail = FAILED, f"unexpected error ({type(exc).__name__}); see logs"
                run.dead_actions.add(step.fingerprint())
                broken = step
                break
    return run
