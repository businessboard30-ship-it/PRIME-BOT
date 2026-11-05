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
MAX_STEPS = 25                # hard ceiling on steps in one plan (must equal ai_bulk.MAX_COMMANDS_PER_MESSAGE; a test checks)
MAX_AUTO_RETRIES = 2          # automatic re-tries of ONE step, transient errors only
MAX_TOTAL_ATTEMPTS = 16       # BASE for every execution attempt across the plan, retries included (see attempt_budget)
MAX_PLAN_SECONDS = 600        # BASE wall-clock budget for one run_plan call (see time_budget)
PLAN_SECONDS_PER_STEP = 30    # a long plan gets more time, up to the cap below
MAX_PLAN_SECONDS_CAP = 840    # stays inside Discord's 15-minute interaction token
BACKOFF_BASE = 2.0            # seconds; doubles per automatic retry, capped below
BACKOFF_CAP = 30.0
_ICON = {DONE: "\u2705", FAILED: "\u274c", SKIPPED: "\u23ed\ufe0f", PENDING: "\u23f8\ufe0f"}


MAX_DESTRUCTIVE_PER_PLAN = 5      # kicks/bans/deletes/purges/removals in ONE plan (bulk commands have their own caps too)
_DESTRUCTIVE_WORDS = frozenset({"delete", "ban", "kick", "purge", "remove", "cancel", "disconnect", "wipe", "clear"})


# Most steps one plan may have, per tier (daily AI-action caps still apply on top; the overall ceiling is MAX_STEPS).
TIER_MAX_STEPS = {"basic": 5, "pro": 12, "elite": MAX_STEPS, "founder": MAX_STEPS}
PROGRESS_EDIT_INTERVAL = 2.5      # seconds between live progress edits (20 edits in a row would hit Discord's edit rate limit)
PROGRESS_WINDOW = 12              # steps shown in the live progress message of a big plan


def tier_step_ceiling(tier: str) -> int:
    """Steps one plan may have for this tier; an unknown tier gets the smallest."""
    return min(TIER_MAX_STEPS.get(str(tier or "").lower(), min(TIER_MAX_STEPS.values())), MAX_STEPS)


class EditThrottle:
    """Lets a live message be edited at most once per `interval` seconds. The FINAL edit must bypass it (force=True)."""

    def __init__(self, interval: float = PROGRESS_EDIT_INTERVAL, clock=time.monotonic):
        self.interval, self.clock, self._last = interval, clock, None

    def ready(self, force: bool = False) -> bool:
        now = self.clock()
        if force or self._last is None or now - self._last >= self.interval:
            self._last = now
            return True
        return False


def is_destructive(command_path: str) -> bool:
    """True for commands whose name says they delete/remove/ban/kick (name based: the allowlist has no flag for it).
    'unban' undoes a ban, so it is not counted."""
    words = [w for w in str(command_path or "").lower().replace("-", "_").replace(" ", "_").split("_") if w]
    return any(w in _DESTRUCTIVE_WORDS for w in words)


def preflight(n_steps: int, paths: List[str], used: Optional[int] = None, cap: Optional[int] = None,
              tier: str = "") -> Optional[str]:
    """Checks a whole plan BEFORE anything runs. Returns the reason to refuse it (plain English), or None if it may go on.

    * daily cap: every step is one AI-run action, so a plan needing more than what is left today is refused up front
      instead of running halfway and stopping (used/cap None = unknown -> the per-step cap check still applies);
    * destructive steps: more than MAX_DESTRUCTIVE_PER_PLAN deletes/bans/kicks in one plan is refused."""
    ceiling = tier_step_ceiling(tier) if tier else MAX_STEPS
    if n_steps > ceiling:
        who = f"the **{tier}** tier" if tier else "one plan"
        return (f"That plan has **{n_steps}** steps, and {who} can run up to {ceiling} at a time, so I didn't run "
                "any of it. Trim the request" + (", or go Premium for longer plans." if tier else "."))
    if cap is not None and used is not None:
        left = max(cap - used, 0)
        if n_steps > left:
            who = f" on the **{tier}** tier" if tier else ""
            return (f"That plan needs **{n_steps}** AI actions but you have **{left}** left today{who} (limit {cap}, resets at "
                    "midnight UTC), so I didn't run any of it. Trim the request, or go Premium for a higher limit.")
    bad = [p for p in paths if is_destructive(p)]
    if len(bad) > MAX_DESTRUCTIVE_PER_PLAN:
        return (f"That plan has **{len(bad)}** delete/ban/kick-type steps, and I only do up to {MAX_DESTRUCTIVE_PER_PLAN} in one go, "
                "so I didn't run any of it. Ask for them in smaller batches.")
    return None


def attempt_budget(n_steps: int) -> int:
    """Attempts allowed for a plan of n_steps: at least the base, and always room for every step plus one retry each
    (a flat 16 would stop a 20-step plan at step 17 with nothing having gone wrong)."""
    return max(MAX_TOTAL_ATTEMPTS, 2 * min(max(n_steps, 0), MAX_STEPS))


def time_budget(n_steps: int) -> int:
    """Seconds one run_plan call may take: the base, growing with the plan, never past the interaction-token lifetime."""
    return min(MAX_PLAN_SECONDS_CAP, max(MAX_PLAN_SECONDS, PLAN_SECONDS_PER_STEP * min(max(n_steps, 0), MAX_STEPS)))


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


def link_dependencies(entries: List[tuple], dropped: set, satisfied: set, plan_keys: set) -> List[tuple]:
    """Turns each planned call's task-level dependencies into plan-level ones, safely.

    entries: [(task_id_or_None, depends_on_task_ids_or_None)] for the calls that made it into the plan, in order.
    dropped: task ids that never reached the plan (refused, bad arguments, ...): anything depending on them is skipped.
    satisfied: task ids already done before the plan (read-only commands run up front).
    plan_keys: task ids that are steps of this plan.
    Returns [(depends_on_or_None, pre_skipped)]. A dependency we cannot place anywhere (e.g. its call was merged or expanded
    into other steps) makes the step 'unknown' (None = fail-fast), never 'satisfied': guessing 'fine' could run a step
    whose prerequisite silently failed."""
    out = []
    for _tid, deps in entries:
        if deps is None:
            out.append((None, False))
            continue
        keep, skip, unknown = [], False, False
        for d in deps:
            if d in dropped:
                skip = True
            elif d in plan_keys:
                keep.append(d)
            elif d in satisfied:
                continue
            else:
                unknown = True
        out.append((None if unknown else keep, skip))
    return out


@dataclass
class PlanStep:
    index: int
    name: str                   # command name the AI chose
    label: str                  # what the person sees, e.g. "/role create"
    args: dict = field(default_factory=dict)
    status: str = PENDING
    detail: str = ""
    attempts: int = 0
    key: str = ""                                   # task id from the planner ("" = none)
    depends_on: Optional[List[str]] = None          # keys of steps this one needs; None = unknown -> after ANY failure, skip (fail-fast)

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
        return cls([PlanStep(i, c["name"], c.get("label") or c["name"], dict(c.get("args") or {}),
                             key=str(c.get("key") or ""),
                             depends_on=None if c.get("depends_on") is None else [str(d) for d in c["depends_on"]])
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

    def failed_steps(self) -> List[PlanStep]:
        return [s for s in self.steps if s.status == FAILED]

    def progress(self) -> str:
        """Short live status shown while the plan runs. A big plan shows a window around the step in progress."""
        n = len(self.steps)
        head = f"Running {n} step(s)... {self.count(DONE)} done."
        steps = self.steps
        hidden = ""
        if n > PROGRESS_WINDOW:
            first_open = next((i for i, s in enumerate(steps) if s.status == PENDING), n - 1)
            lo = max(0, min(first_open - 3, n - PROGRESS_WINDOW))
            steps, hidden = steps[lo:lo + PROGRESS_WINDOW], f" (steps {lo + 1}-{lo + PROGRESS_WINDOW} shown)"
        lines = [f"{_ICON[s.status] if s.status != PENDING else chr(0x23f3)} {s.index}. {s.label}" for s in steps]
        return head + hidden + "\n" + "\n".join(lines)

    def report_pages(self, limit: int = 1900) -> List[str]:
        """The final report split into messages of at most `limit` characters; nothing is cut off."""
        parts = [f"{self.count(DONE)} done"]
        for status in (FAILED, SKIPPED, PENDING):
            c = self.count(status)
            if c:
                parts.append(f"{c} {'not reached' if status == PENDING else status}")
        head = "Plan finished: " + ", ".join(parts) + "."
        if self.stopped:
            head += f" Stopped early: {self.stopped}"
        elif self.retryable():
            head += " Nothing that already succeeded will run again if you retry the failed ones."
        lines = [f"{_ICON[s.status]} {s.index}. {s.label}" + (f" -- {s.detail}" if s.detail else "") for s in self.steps]
        out, cur = [], head
        for line in lines:
            line = line if len(line) <= limit else line[:limit - 1] + "\u2026"
            if len(cur) + 1 + len(line) > limit:
                out.append(cur)
                cur = line
            else:
                cur += "\n" + line
        out.append(cur)
        return out

    def report(self) -> str:
        return "\n".join(self.report_pages(limit=10 ** 6))


async def run_plan(run: PlanRun, executor: Callable[[PlanStep], Awaitable[None]], *, pause: float = 0.0,
                   timeout: float = STEP_TIMEOUT, sleep=asyncio.sleep, clock=time.monotonic,
                   on_update: Optional[Callable[[PlanRun], Awaitable[None]]] = None) -> PlanRun:
    """Runs every PENDING step in order. DONE steps are skipped over untouched (that is what makes retry safe).

    Bounded on every axis: steps, total attempts, wall-clock time, per-step automatic retries. Only transient errors
    are re-tried automatically (with backoff, honouring Retry-After). A permanent error is never repeated."""
    async def _notify() -> None:
        if on_update is not None:
            try:
                await on_update(run)
            except Exception:                       # a failed progress edit must never break the plan
                pass

    started = clock()
    attempts_allowed, seconds_allowed = attempt_budget(len(run.steps)), time_budget(len(run.steps))
    broken: Optional[PlanStep] = None
    first = True
    if len(run.steps) > MAX_STEPS:
        for step in run.steps[MAX_STEPS:]:
            if step.status == PENDING:
                step.status, step.detail = SKIPPED, f"over the {MAX_STEPS}-step limit"
    by_key = {st.key: st for st in run.steps if st.key}
    for step in run.steps:
        if step.status != PENDING:
            continue
        if step.depends_on is None:
            blocker = broken                         # unknown prerequisites: after any failure, don't run half-blind
        else:                                        # known prerequisites: only what it NEEDS decides; independent steps go on
            blocker = next((by_key[k] for k in step.depends_on
                            if k in by_key and by_key[k].status in (FAILED, SKIPPED)), None)
        if blocker is not None:
            step.status, step.detail = SKIPPED, f"not run, step {blocker.index} failed first"
            continue
        if run.stopped:
            continue                                # stays PENDING -> reported as "not reached"
        if run.cancel.is_set():
            run.stopped = "cancelled"
            continue
        if clock() - started > seconds_allowed:
            run.stopped = f"time budget ({seconds_allowed}s) used up"
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
            if run.total_attempts >= attempts_allowed:
                step.status, step.detail = FAILED, "attempt budget used up"
                run.stopped = step.detail
                broken = step
                break
            run.total_attempts += 1
            step.attempts += 1
            try:
                await asyncio.wait_for(executor(step), timeout)
                step.status, step.detail = DONE, ""
                await _notify()
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
