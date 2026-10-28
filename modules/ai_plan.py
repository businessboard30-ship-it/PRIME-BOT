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
from dataclasses import dataclass, field
from typing import Awaitable, Callable, List, Optional

PENDING, DONE, FAILED, SKIPPED = "pending", "done", "failed", "skipped"
MAX_RETRY_ROUNDS = 2          # "Retry remaining" can be used this many times per plan
STEP_TIMEOUT = 120            # seconds one step may take before it counts as failed
_ICON = {DONE: "\u2705", FAILED: "\u274c", SKIPPED: "\u23ed\ufe0f", PENDING: "\u23f8\ufe0f"}


class StepFailed(Exception):
    """Raised by an executor when a step did not complete. `stop_all` ends the whole plan (e.g. daily cap hit)."""

    def __init__(self, message: str, stop_all: bool = False):
        super().__init__(message)
        self.stop_all = stop_all


@dataclass
class PlanStep:
    index: int
    name: str                   # command name the AI chose
    label: str                  # what the person sees, e.g. "/role create"
    args: dict = field(default_factory=dict)
    status: str = PENDING
    detail: str = ""
    attempts: int = 0


class PlanRun:
    def __init__(self, steps: List[PlanStep]):
        self.steps = steps
        self.retry_rounds = 0
        self.stopped = ""                     # non-empty when the plan was ended early (e.g. daily limit)

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
                   timeout: float = STEP_TIMEOUT, sleep=asyncio.sleep) -> PlanRun:
    """Runs every PENDING step in order. DONE steps are skipped over untouched (that is what makes retry safe)."""
    broken: Optional[PlanStep] = None
    first = True
    for step in run.steps:
        if step.status != PENDING:
            continue
        if broken is not None:
            step.status, step.detail = SKIPPED, f"not run, step {broken.index} failed first"
            continue
        if run.stopped:
            continue                                # stays PENDING -> reported as "not reached"
        if not first and pause:
            await sleep(pause)
        first = False
        step.attempts += 1
        try:
            await asyncio.wait_for(executor(step), timeout)
            step.status = DONE
        except StepFailed as exc:
            step.status, step.detail = FAILED, str(exc)[:200]
            if exc.stop_all:
                run.stopped = step.detail
            broken = step
        except asyncio.TimeoutError:
            step.status, step.detail = FAILED, f"timed out after {int(timeout)}s"
            broken = step
        except Exception as exc:                    # unknown error: stop, don't guess and push on
            step.status, step.detail = FAILED, f"unexpected error ({type(exc).__name__}); see logs"
            broken = step
    return run
