# path: modules/ai_jobs.py

"""
Phase 3 of the autonomous-AI plan: the pure logic of the persistent scheduler (no Discord, no database).

Safety model (option 1 "safe first"):
  * a job is a short list of ALLOW-LISTED steps. Today the only kind is `send_message`; nothing destructive can be
    scheduled, because an unattended run has no live Confirm click;
  * every run re-checks, at run time, that the person who created the job STILL has the right in that server
    (the cog passes `can_run`); if not, the job is disabled -- never "run anyway";
  * at-most-once: a job is claimed atomically and marked 'running'. If the bot dies mid-run the run is marked
    'interrupted' and is not repeated automatically;
  * steps run through ai_plan.run_plan, so transient Discord errors are retried with backoff, permanent errors stop
    the run with a plain reason, and execution is bounded;
  * repeated failures back off (5 min, 15 min, 1 h) and then disable the job instead of hammering Discord.
"""

from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable, List, Optional, Tuple

from modules import ai_plan

ALLOWED_KINDS = frozenset({"send_message"})
MAX_STEPS = 5
MAX_CONTENT = 1900
MIN_INTERVAL_SECONDS = 300
MAX_JOBS_PER_GUILD = 20
MAX_TITLE = 80
STEP_PAUSE_SECONDS = 1.5
RETRY_BACKOFF_SECONDS = (300, 900, 3600)
MAX_ONCE_FAILURES = 3              # a one-off job is retried this many times in total, then disabled
MAX_RECURRING_FAILURES = 5         # a recurring job is disabled after this many failed runs in a row
CLAIM_LOCK_SECONDS = 900           # past this, a 'running' job is treated as interrupted
MAX_RUNS_KEPT = 50

OK, PARTIAL, FAILED, BLOCKED, INTERRUPTED = "ok", "partial", "failed", "blocked", "interrupted"


def validate_steps(raw) -> Tuple[Optional[List[dict]], str]:
    """Returns (clean_steps, "") or (None, reason). Only allow-listed kinds and fields survive."""
    if not isinstance(raw, list) or not raw:
        return None, "A job needs at least one step."
    if len(raw) > MAX_STEPS:
        return None, f"A job can have at most {MAX_STEPS} steps."
    clean = []
    for i, st in enumerate(raw, 1):
        if not isinstance(st, dict) or st.get("kind") not in ALLOWED_KINDS:
            return None, f"Step {i}: only {', '.join(sorted(ALLOWED_KINDS))} can be scheduled."
        content = str(st.get("content") or "").strip()
        if not content or len(content) > MAX_CONTENT:
            return None, f"Step {i}: message must be 1-{MAX_CONTENT} characters."
        try:
            channel_id = int(st.get("channel_id"))
        except (TypeError, ValueError):
            return None, f"Step {i}: missing channel."
        clean.append({"kind": "send_message", "channel_id": channel_id, "content": content})
    return clean, ""


def validate_schedule(kind: str, interval_seconds=None, daily_at=None) -> str:
    if kind == "once":
        return ""
    if kind == "interval":
        if not interval_seconds or interval_seconds < MIN_INTERVAL_SECONDS:
            return f"Repeat interval must be at least {MIN_INTERVAL_SECONDS // 60} minutes."
        return ""
    if kind == "daily":
        try:
            h, m = (int(x) for x in str(daily_at).split(":"))
        except ValueError:
            return "Daily time must look like 09:00 (UTC)."
        return "" if 0 <= h < 24 and 0 <= m < 60 else "Daily time must look like 09:00 (UTC)."
    return "Unknown schedule type."


def next_run_after(kind: str, after: datetime, *, interval_seconds=None, daily_at=None) -> Optional[datetime]:
    """Next fire time strictly after `after` (None for a one-off). Never returns a time in the past relative to `after`."""
    if kind == "once":
        return None
    if kind == "interval":
        return after + timedelta(seconds=int(interval_seconds))
    h, m = (int(x) for x in str(daily_at).split(":"))
    nxt = after.astimezone(timezone.utc).replace(hour=h, minute=m, second=0, microsecond=0)
    if nxt <= after:
        nxt += timedelta(days=1)
    return nxt


def after_run(job: dict, status: str, now: datetime) -> dict:
    """What to store once a run ends: {enabled, next_run_at, consecutive_failures, job_status}.
    `job` is the claimed row (before this run). Recurring jobs always continue on their normal schedule (never a burst
    of catch-up runs); a one-off retries with backoff only after a failure."""
    failures = 0 if status in (OK,) else job.get("consecutive_failures", 0) + 1
    kind = job["schedule_kind"]
    if status == BLOCKED:                                       # creator lost the right: stop for good
        return {"enabled": False, "next_run_at": job["next_run_at"], "consecutive_failures": failures, "job_status": "disabled"}
    if kind == "once":
        if status == OK or status == INTERRUPTED:
            return {"enabled": False, "next_run_at": job["next_run_at"], "consecutive_failures": failures, "job_status": "done"}
        if failures >= MAX_ONCE_FAILURES:
            return {"enabled": False, "next_run_at": job["next_run_at"], "consecutive_failures": failures, "job_status": "failed"}
        delay = RETRY_BACKOFF_SECONDS[min(failures - 1, len(RETRY_BACKOFF_SECONDS) - 1)]
        return {"enabled": True, "next_run_at": now + timedelta(seconds=delay), "consecutive_failures": failures,
                "job_status": "retrying"}
    nxt = next_run_after(kind, now, interval_seconds=job.get("interval_seconds"), daily_at=job.get("daily_at"))
    if failures >= MAX_RECURRING_FAILURES:
        return {"enabled": False, "next_run_at": nxt, "consecutive_failures": failures, "job_status": "failed"}
    return {"enabled": True, "next_run_at": nxt, "consecutive_failures": failures, "job_status": "scheduled"}


async def execute_job(job: dict, send: Callable[[dict], Awaitable[None]],
                      can_run: Callable[[], Awaitable[str]]) -> Tuple[str, str]:
    """Runs one claimed job. `send(step)` posts the message and raises ai_plan.StepFailed on failure.
    `can_run()` returns "" when the creator is still allowed to do this here, else the reason.
    Returns (status, report_text)."""
    reason = await can_run()
    if reason:
        return BLOCKED, f"Not run: {reason}"
    steps, err = validate_steps(job.get("steps"))
    if steps is None:                                           # stored steps no longer valid (tampered / rules changed)
        return BLOCKED, f"Not run: {err}"
    plan = ai_plan.PlanRun.from_calls(
        [{"name": s["kind"], "label": f"post in <#{s['channel_id']}>", "args": s} for s in steps])

    async def _exec(step: ai_plan.PlanStep) -> None:
        await send(step.args)

    await ai_plan.run_plan(plan, _exec, pause=STEP_PAUSE_SECONDS)
    done = plan.count(ai_plan.DONE)
    status = OK if done == len(plan.steps) else (PARTIAL if done else FAILED)
    return status, plan.report()
