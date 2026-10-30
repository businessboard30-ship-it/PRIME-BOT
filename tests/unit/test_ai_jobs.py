"""Phase 3 scheduler logic: allowlist validation, schedule maths, failure/backoff rules, at-most-once execution."""
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

from modules import ai_jobs, ai_plan

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)


def run(c):
    return asyncio.run(c)


def step(**kw):
    return {"kind": "send_message", "channel_id": 5, "content": "hi", **kw}


def job(kind="once", failures=0, **kw):
    return {"id": 1, "schedule_kind": kind, "consecutive_failures": failures, "next_run_at": NOW,
            "interval_seconds": 3600, "daily_at": "09:00", "steps": [step()], **kw}


def test_only_allowlisted_kinds_and_limits():
    ok, err = ai_jobs.validate_steps([step()])
    assert ok and not err
    for bad in ([], None, [step(kind="delete_channel")], [step(content=" ")], [step(content="x" * 2000)],
                [step(channel_id="abc")], [step()] * (ai_jobs.MAX_STEPS + 1), ["send_message"]):
        assert ai_jobs.validate_steps(bad)[0] is None
    cleaned, _ = ai_jobs.validate_steps([step(extra="drop me", permissions="admin")])
    assert set(cleaned[0]) == {"kind", "channel_id", "content"}


def test_schedule_validation():
    assert ai_jobs.validate_schedule("interval", 60)            # too frequent
    assert not ai_jobs.validate_schedule("interval", 300)
    assert ai_jobs.validate_schedule("daily", None, "25:00") and not ai_jobs.validate_schedule("daily", None, "09:30")
    assert ai_jobs.validate_schedule("weird")


def test_next_run_after():
    assert ai_jobs.next_run_after("once", NOW) is None
    assert ai_jobs.next_run_after("interval", NOW, interval_seconds=600) == NOW + timedelta(minutes=10)
    assert ai_jobs.next_run_after("daily", NOW, daily_at="09:00") == datetime(2026, 10, 31, 9, 0, tzinfo=timezone.utc)
    assert ai_jobs.next_run_after("daily", NOW, daily_at="15:30") == datetime(2026, 10, 30, 15, 30, tzinfo=timezone.utc)


def test_recurring_never_bursts_and_disables_after_streak():
    late = job("interval", next_run_at=NOW - timedelta(days=3))          # was down for 3 days
    out = ai_jobs.after_run(late, ai_jobs.OK, NOW)
    assert out["enabled"] and out["next_run_at"] == NOW + timedelta(hours=1)    # ONE next run, no catch-up
    out = ai_jobs.after_run(job("daily", failures=ai_jobs.MAX_RECURRING_FAILURES - 1), ai_jobs.FAILED, NOW)
    assert not out["enabled"] and out["job_status"] == "failed"


def test_once_job_backoff_then_gives_up_and_success_resets():
    o1 = ai_jobs.after_run(job("once", 0), ai_jobs.FAILED, NOW)
    o2 = ai_jobs.after_run(job("once", 1), ai_jobs.PARTIAL, NOW)
    o3 = ai_jobs.after_run(job("once", 2), ai_jobs.FAILED, NOW)
    assert o1["enabled"] and o1["next_run_at"] == NOW + timedelta(seconds=300)
    assert o2["enabled"] and o2["next_run_at"] == NOW + timedelta(seconds=900)
    assert not o3["enabled"] and o3["job_status"] == "failed"
    done = ai_jobs.after_run(job("once", 2), ai_jobs.OK, NOW)
    assert not done["enabled"] and done["job_status"] == "done" and done["consecutive_failures"] == 0


def test_blocked_and_interrupted_are_never_rerun():
    assert not ai_jobs.after_run(job("daily"), ai_jobs.BLOCKED, NOW)["enabled"]
    assert not ai_jobs.after_run(job("once"), ai_jobs.INTERRUPTED, NOW)["enabled"]
    rec = ai_jobs.after_run(job("interval"), ai_jobs.INTERRUPTED, NOW)       # recurring keeps its normal slot
    assert rec["enabled"] and rec["next_run_at"] == NOW + timedelta(hours=1)


def test_execute_blocked_when_creator_lost_permission_and_nothing_is_sent():
    sent = []

    async def send(s):
        sent.append(s)

    async def can_run():
        return "the person who created this job no longer has Manage Server"

    status, report = run(ai_jobs.execute_job(job(), send, can_run))
    assert status == ai_jobs.BLOCKED and not sent and "Manage Server" in report


def test_execute_ok_partial_failed():
    async def allow():
        return ""

    j = job(steps=[step(content="a"), step(content="b"), step(content="c")])
    got = []

    async def good(s):
        got.append(s["content"])

    assert run(ai_jobs.execute_job(j, good, allow))[0] == ai_jobs.OK and got == ["a", "b", "c"]

    async def second_fails(s):
        if s["content"] == "b":
            raise ai_plan.StepFailed("Discord refused: the bot is missing a permission or its role is too low")

    status, report = run(ai_jobs.execute_job(j, second_fails, allow))
    assert status == ai_jobs.PARTIAL and "missing a permission" in report and "step 2 failed first" in report

    async def always(s):
        raise ai_plan.StepFailed("nope")

    assert run(ai_jobs.execute_job(j, always, allow))[0] == ai_jobs.FAILED


def test_execute_rejects_tampered_stored_steps():
    async def allow():
        return ""

    async def send(s):
        raise AssertionError("must not send")

    bad = job(steps=[{"kind": "delete_channel", "channel_id": 5, "content": "x"}])
    assert run(ai_jobs.execute_job(bad, send, allow))[0] == ai_jobs.BLOCKED


def test_wiring_migration_cog_allowlist_and_claim_is_atomic():
    db = (ROOT / "database.py").read_text()
    assert "096_ai_scheduled_jobs.sql" in db and (ROOT / "database/migrations/096_ai_scheduled_jobs.sql").exists()
    assert "FOR UPDATE SKIP LOCKED" in db and "status <> 'running'" in db       # no duplicate runs
    sql = (ROOT / "database/migrations/096_ai_scheduled_jobs.sql").read_text()
    assert "CREATE TABLE IF NOT EXISTS ai_scheduled_jobs" in sql and "ai_job_runs" in sql
    from modules.ai_command_allowlist import AI_COMMANDS
    specs = {c.path: c for c in AI_COMMANDS}
    assert {"aijobs create", "aijobs list", "aijobs cancel"} <= set(specs)
    assert specs["aijobs create"].requires_confirmation and not specs["aijobs list"].requires_confirmation
    assert "manage_guild" in specs["aijobs create"].needs_perms
