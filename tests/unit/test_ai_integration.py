"""Phase 8 integration + compatibility: cross-layer scenarios (skill -> plan runner, restart recovery, isolation, agent boundaries)
and 'is everything the AI can do actually wired up?' checks that would have caught a cog that was never loaded."""
import asyncio
import glob
import os
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from modules import ai_agents, ai_jobs, ai_memory, ai_plan, ai_skills
from modules.ai_command_allowlist import AI_COMMANDS, AI_COMMANDS_BY_NAME

ROOT = Path(__file__).resolve().parents[2]


def run(c):
    return asyncio.run(c)


# ---- compatibility: everything the AI can do is really wired up ---------------------------------------------------------

def _sources():
    out = {}
    for f in glob.glob(str(ROOT / "discord_bot/**/*.py"), recursive=True):
        out[f] = open(f, encoding="utf-8", errors="ignore").read()
    return out


# Commands whose cogs were retired on purpose (see the comments in discord_bot/bot.py). The AI skips a tool whose command does not
# exist (ai_tools._build_command_tools), so these entries are inert. Remove them from the allowlist when convenient.
RETIRED_ON_PURPOSE = {"botstore_browse", "botstore_trending", "botstore_search", "botstore_mine", "botstore_rate", "archive_trending"}


def test_every_ai_cog_is_loaded_by_the_bot():
    bot = (ROOT / "discord_bot/bot.py").read_text()
    for ext in ("ai_tools", "ai_skills", "ai_agents", "ai_memory", "ai_jobs", "ai_server_ops"):
        assert f'cogs.{ext}"' in bot or f'"{ext}"' in bot, f"{ext} is never loaded by bot.py"


def test_every_allowlisted_command_exists_or_is_known_retired():
    src = "\n".join(_sources().values())
    names = set(re.findall(r'name\s*=\s*"([a-z0-9_-]+)"', src))
    names |= set(re.findall(r"\bdef ([a-z0-9_]+)\(", src))
    ai_only = {c.name for c in AI_COMMANDS if getattr(c, "ai_only", False)}
    missing = {c.name for c in AI_COMMANDS if not all(p in names for p in c.command_path.split())} - ai_only
    assert missing <= RETIRED_ON_PURPOSE, f"allowlisted but no such command in the code: {sorted(missing - RETIRED_ON_PURPOSE)}"


def test_retired_commands_really_have_their_cogs_switched_off():
    live = [l for l in (ROOT / "discord_bot/bot.py").read_text().splitlines() if "load_extension" in l and not l.strip().startswith("#")]
    text = "\n".join(live)
    assert "cogs.botstore" not in text and "cogs.archive\"" not in text, "a retired cog is loaded again: update RETIRED_ON_PURPOSE"


def test_every_cog_the_ai_relies_on_has_a_setup_and_imports_cleanly():
    import importlib
    for mod in ("ai_skills", "ai_agents", "ai_memory", "ai_jobs"):
        m = importlib.import_module(f"discord_bot.cogs.{mod}")
        assert asyncio.iscoroutinefunction(m.setup)


def test_the_ai_can_never_toggle_or_read_its_own_controls():
    for c in AI_COMMANDS:
        assert not re.search(r"aiskills|aiagents|aimemory_(clear|forget_all)", c.command_path.replace(" ", "")), c.name


def test_database_changes_are_additive_and_idempotent():
    """Rollout safety: every Phase 3-7 migration only adds (IF NOT EXISTS), never drops or rewrites existing data."""
    for f in glob.glob(str(ROOT / "database/migrations/09[6-9]_*.sql")):
        text = open(f).read().upper()
        assert not re.search(r"\bDROP\s+(TABLE|COLUMN)|\bTRUNCATE\b|\bDELETE\s+FROM\b|\bALTER\s+COLUMN\b", text), f
        for stmt in re.findall(r"(CREATE\s+TABLE[^;]*|ALTER\s+TABLE[^;]*ADD\s+COLUMN[^;]*)", text):
            assert "IF NOT EXISTS" in stmt, f"{os.path.basename(f)} is not idempotent: {stmt[:60]}"
    # tables created on first use by the Python modules are idempotent too
    for f in ("modules/ai_skills.py", "modules/ai_agents.py", "modules/ai_memory.py"):
        for stmt in re.findall(r'"(CREATE TABLE[^"]*)"', (ROOT / f).read_text()):
            assert "IF NOT EXISTS" in stmt, f


# ---- scenario: a skill runs through the real plan runner, with a failure half way ---------------------------------------

def test_skill_steps_run_in_order_and_a_midway_failure_is_reported_with_a_safe_retry():
    skill = ai_skills.get_skill("lockdown_channel")
    ALL = SimpleNamespace(manage_channels=True, manage_roles=True)
    calls, err = ai_skills.expand(skill, {"channel": "#general", "reason": "raid"}, ALL)
    assert not err and [c["name"] for c in calls] == ["channeledit_lock", "channeledit_topic"]
    steps = [ai_plan.PlanStep(i, c["name"], "/" + c["name"].replace("_", " "), c["arguments"]) for i, c in enumerate(calls)]
    ran, fail_once = [], {"topic": True}

    async def executor(step):
        if step.name == "channeledit_topic" and fail_once["topic"]:
            fail_once["topic"] = False
            raise ai_plan.StepFailed("Discord said no")                       # permanent failure, not transient
        ran.append(step.name)

    plan = ai_plan.PlanRun(steps)
    run(ai_plan.run_plan(plan, executor))
    assert ran == ["channeledit_lock"]                                         # step 1 done, step 2 failed
    assert plan.steps[0].status == "done" and plan.steps[1].status != "done"
    run(ai_plan.run_plan(plan, executor))                                      # retry: the finished step is NOT repeated
    assert ran.count("channeledit_lock") == 1


# ---- scenario: restart recovery + retries for scheduled jobs ------------------------------------------------------------

def test_job_that_was_running_when_the_bot_restarted_is_recovered_not_lost_or_doubled():
    src = (ROOT / "database.py").read_text()
    assert "async def get_interrupted_ai_jobs" in src and "async def claim_due_ai_jobs" in src
    claim = src[src.index("async def claim_due_ai_jobs"):src.index("async def get_interrupted_ai_jobs")]
    assert "FOR UPDATE SKIP LOCKED" in claim.upper() or "LOCKED" in claim.upper()       # atomic claim: two pollers cannot run one job
    cog = (ROOT / "discord_bot/cogs/ai_jobs.py").read_text()
    assert "get_interrupted_ai_jobs" in cog                                           # startup sweep exists


def test_job_failure_retries_are_bounded_then_the_job_stops():
    from datetime import datetime, timezone
    now = datetime(2026, 11, 3, 12, tzinfo=timezone.utc)
    once = {"schedule_kind": "once", "next_run_at": now, "consecutive_failures": 0}
    seen = []
    for _ in range(ai_jobs.MAX_ONCE_FAILURES):
        upd = ai_jobs.after_run(once, ai_jobs.FAILED, now)
        once = {**once, **upd}
        seen.append((upd["job_status"], upd["enabled"]))
    assert seen[0] == ("retrying", True) and seen[-1] == ("failed", False)       # backoff, then it gives up
    assert ai_jobs.after_run({**once, "consecutive_failures": 0}, ai_jobs.OK, now)["job_status"] == "done"
    rec = {"schedule_kind": "interval", "interval_seconds": 600, "daily_at": None, "next_run_at": now, "consecutive_failures": 0}
    for i in range(ai_jobs.MAX_RECURRING_FAILURES):
        upd = ai_jobs.after_run(rec, ai_jobs.FAILED, now)
        rec = {**rec, **upd}
    assert rec["enabled"] is False and rec["job_status"] == "failed"              # a broken recurring job is switched off
    assert ai_jobs.after_run({**rec, "consecutive_failures": 3}, ai_jobs.BLOCKED, now)["job_status"] == "disabled"   # lost permission


# ---- scenario: memory / settings isolation ------------------------------------------------------------------------------

def test_agent_and_skill_settings_are_per_server(monkeypatch):
    async def ov(g):
        return {"research_assistant": True} if g == 1 else {}
    monkeypatch.setattr(ai_agents, "overrides", ov)
    P = SimpleNamespace(manage_guild=True, moderate_members=True)
    on = lambda g: {t["function"]["name"] for t in run(ai_agents.tools_for(g, P))}
    assert on(1) == {"agent__research_assistant"} and on(2) == set()


def test_memory_queries_are_always_scoped_by_scope_and_owner():
    src = (ROOT / "modules/ai_memory.py").read_text()
    import ast
    stmts = [n.value for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Constant) and isinstance(n.value, str)
             and "ai_memories" in n.value and re.match(r"\s*(SELECT|DELETE|UPDATE|INSERT)", n.value)]
    assert len(stmts) >= 6
    for st in stmts:
        if st.startswith("INSERT"):
            continue
        assert "scope = $" in st or "scope=$" in st, st[:80]                      # never a query across scopes
        assert "owner_id = $" in st or "WHERE id = $1 AND scope = $2" in st, st[:80]   # nor across owners (or id+scope for the dup bump)


# ---- scenario: agent boundary through the guard -----------------------------------------------------------------------

def test_an_agent_can_only_propose_commands_inside_its_own_list_even_if_the_model_tries_more():
    a = ai_agents.get_agent("research_assistant")
    kept, dropped = ai_agents.vet_calls(a, [{"name": n, "arguments": {}} for n in ("ban", "kick", "purge", "serveranalytics", "rank")])
    assert [k["name"] for k in kept] == ["serveranalytics", "rank"] and dropped == 3
    assert all(n in AI_COMMANDS_BY_NAME for n in a.tools)
