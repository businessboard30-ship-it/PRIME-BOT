"""Phase 4: a whole plan is checked against the daily cap and the destructive-step limit BEFORE anything runs.
Also the first real test of ai_tools._run_command_batch (links, pre-skips, confirm text)."""
import asyncio
from types import SimpleNamespace as N
from unittest.mock import AsyncMock

from discord_bot.cogs import ai_tools as at
from modules import ai_bulk, ai_plan


def run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def test_destructive_names():
    for p in ("ban", "kick", "purge", "setup_bulk_delete", "role bulk-delete", "member role-remove", "thread_delete", "eventops cancel"):
        assert ai_plan.is_destructive(p), p
    for p in ("unban", "role_create", "lock", "setup_bulk_create", "serverstats", "member role-add", ""):
        assert not ai_plan.is_destructive(p), p


def test_preflight_refuses_a_plan_bigger_than_what_is_left():
    msg = ai_plan.preflight(5, ["role_create"] * 5, used=17, cap=20, tier="pro")
    assert msg and "needs **5**" in msg and "**3** left" in msg and "didn't run any of it" in msg
    assert ai_plan.preflight(3, ["role_create"] * 3, used=17, cap=20, tier="pro") is None      # exactly what is left: fine
    assert ai_plan.preflight(30, [], used=None, cap=None) is None                              # unknown budget: per-step check decides


def test_preflight_limits_destructive_steps():
    assert ai_plan.preflight(6, ["ban"] * 6, used=0, cap=50) is None
    msg = ai_plan.preflight(7, ["ban"] * 7, used=0, cap=50)
    assert msg and "**7**" in msg and "smaller batches" in msg
    assert ai_plan.preflight(12, ["ban"] * 6 + ["role_create"] * 6, used=0, cap=50) is None     # only destructive ones count


# ── _run_command_batch, driven for real ─────────────────────────────────────
def spec(name, confirm=True):
    return N(name=name, command_path=name, requires_confirmation=confirm)


def make_batch(monkeypatch, names, budget=("pro", 20, 0), refused=()):
    cog = at.AIToolsCog.__new__(at.AIToolsCog)
    specs = {n: spec(n) for n in names}

    async def fake_resolve(ctx, name, raw):
        if name in refused:
            return None, None, "not allowed here", False
        return specs[name], N(binding=None), None, False
    monkeypatch.setattr(at, "resolve_and_check", fake_resolve)
    monkeypatch.setattr(at, "needs_confirmation", lambda sp, args: sp.requires_confirmation)
    monkeypatch.setattr(at, "get_command_budget", AsyncMock(return_value=budget))
    monkeypatch.setattr(at.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(at.ai_bulk, "DEDUPER", ai_bulk.CallDeduper())          # the real one is bot-wide state
    cog._validate_tool_args = lambda sp, cmd, raw: (raw, None)
    cog._resolve_tool_args = lambda ctx, cmd, raw: dict(raw)
    cog._dispatch_tool_call = AsyncMock()
    ctx = N(user=N(id=42), followup=N(send=AsyncMock()))
    return cog, ctx


def calls_for(names, **extra):
    return [{"name": n, "arguments": {"i": i}, **extra.get(n, {})} for i, n in enumerate(names)]


def sent(ctx):
    return [c.args[0] for c in ctx.followup.send.await_args_list]


def test_batch_refused_up_front_when_the_cap_is_too_low(monkeypatch):
    names = [f"cmd{i}" for i in range(5)]
    cog, ctx = make_batch(monkeypatch, names, budget=("pro", 20, 17))
    run(cog._run_command_batch(ctx, calls_for(names), real_interaction=True))
    text = " ".join(sent(ctx))
    assert "didn't run any of it" in text and "**3** left" in text
    assert not any(c.kwargs.get("view") for c in ctx.followup.send.await_args_list)            # no Confirm button was offered
    cog._dispatch_tool_call.assert_not_awaited()


def test_batch_that_fits_gets_one_confirm(monkeypatch):
    names = [f"cmd{i}" for i in range(4)]
    cog, ctx = make_batch(monkeypatch, names, budget=("pro", 20, 0))
    run(cog._run_command_batch(ctx, calls_for(names), real_interaction=True))
    last = ctx.followup.send.await_args_list[-1]
    assert last.kwargs.get("view") is not None and "I'd run these 4 commands" in sent(ctx)[0]


def test_a_budget_lookup_failure_does_not_block_the_plan(monkeypatch):
    names = ["cmd0", "cmd1"]
    cog, ctx = make_batch(monkeypatch, names)
    at.get_command_budget.side_effect = RuntimeError("no database")
    run(cog._run_command_batch(ctx, calls_for(names), real_interaction=True))
    assert ctx.followup.send.await_args_list[-1].kwargs.get("view") is not None


def test_dependent_of_a_refused_task_is_marked_skipped_in_the_confirm_text(monkeypatch):
    names = ["cat_make", "chan_make", "role_make"]
    cog, ctx = make_batch(monkeypatch, names, refused=("cat_make",))
    calls = calls_for(names, cat_make={"task_id": "1", "depends_on_tasks": []},
                      chan_make={"task_id": "2", "depends_on_tasks": ["1"]},
                      role_make={"task_id": "3", "depends_on_tasks": []})
    run(cog._run_command_batch(ctx, calls, real_interaction=True))
    prompt = sent(ctx)[0]
    assert "chan_make" in prompt and "will be skipped" in prompt.split("chan_make")[1].split("\n")[0]
    assert "will be skipped" not in prompt.split("role_make")[1]
