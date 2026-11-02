"""Phase 6 agents: registration rules, no escalation, vetting, off by default, bounded delegation, wiring."""
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from modules import ai_agents as ag
from modules.ai_command_allowlist import AI_COMMANDS_BY_NAME

ROOT = Path(__file__).resolve().parents[2]


def run(c):
    return asyncio.run(c)


def perms(**on):
    names = ("manage_guild", "moderate_members", "manage_channels", "administrator")
    return SimpleNamespace(**{n: on.get(n, False) for n in names})


def agent(name):
    return ag.get_agent(name)


def test_builtin_agents_exist_are_off_by_default_and_only_use_allowlisted_tools():
    names = {a.name for a in ag.all_agents()}
    assert {"server_manager", "moderation_assistant", "research_assistant", "workflow_scheduler"} <= names
    for a in ag.all_agents():
        assert a.enabled_by_default is False
        assert a.tools and all(t in AI_COMMANDS_BY_NAME for t in a.tools)
        assert 1 <= a.max_tool_calls <= ag.MAX_AGENT_TOOL_CALLS


def test_agents_never_get_destructive_or_account_level_tools():
    banned = {"ban", "kick", "unban", "purge", "messages_purge", "role_bulk_delete", "setup_bulk_delete", "serveredit_edit",
              "removeclone", "messages_user_purge", "channeledit_permissions"}
    for a in ag.all_agents():
        assert not (set(a.tools) & banned), a.name


def test_registration_rules_and_escalation_attempts_are_refused():
    ok = dict(description="d", instructions="i", tools=("rank",))
    with pytest.raises(ValueError):
        ag.register_agent(ag.Agent("server_manager", **ok))                       # duplicate
    for bad in (dict(name="Bad Name", **ok), dict(name="x", **ok),
                dict(name="no_tools", description="d", instructions="i", tools=()),
                dict(name="fake_cmd", description="d", instructions="i", tools=("not_a_command",)),
                dict(name="nests", description="d", instructions="i", tools=("agent__server_manager",)),
                dict(name="skills", description="d", instructions="i", tools=("skill__lockdown_channel",)),
                dict(name="lookup", description="d", instructions="i", tools=("scam_lookup",)),
                dict(name="choice", description="d", instructions="i", tools=("ask_user_choice",)),
                dict(name="toomany", max_tool_calls=99, **ok), dict(name="zero", max_tool_calls=0, **ok),
                dict(name="nodesc", description=" ", instructions="i", tools=("rank",))):
        with pytest.raises(ValueError):
            ag.register_agent(ag.Agent(**bad))


def test_tool_schema_and_clean_task():
    t = ag.tool_schema(agent("research_assistant"))
    assert t["function"]["name"] == "agent__research_assistant" and t["function"]["parameters"]["required"] == ["task"]
    assert ag.clean_task({"task": "  show   stats\x00 \u202e now "})[0] == "show stats now"
    assert len(ag.clean_task({"task": "x" * 5000})[0]) == ag.MAX_TASK_CHARS
    for bad in ({}, {"task": ""}, {"task": "  "}, {"task": None}, {"task": {"a": 1}}, {"task": ["a"]}, {"task": True}, "x", None, []):
        assert ag.clean_task(bad)[0] is None


def test_restricted_tools_keep_only_the_agents_own_commands():
    offered = [{"function": {"name": n}} for n in ("rank", "ban", "serveranalytics", "agent__server_manager", "skill__x")]
    names = [t["function"]["name"] for t in ag.restricted_tools(agent("research_assistant"), offered)]
    assert names == ["rank", "serveranalytics"]


def test_vet_calls_drops_anything_outside_the_agent_and_caps_the_count():
    a = agent("moderation_assistant")
    kept, dropped = ag.vet_calls(a, [
        {"name": "warns", "arguments": {"user": "1"}}, {"name": "ban", "arguments": {"user": "1"}},
        {"name": "agent__server_manager", "arguments": {"task": "x"}}, {"name": "skill__lockdown_channel", "arguments": {}},
        {"name": "scam_lookup", "arguments": {"user_id": "1"}}, {"name": "warn", "arguments": "not a dict"}, "junk", None,
        {"name": "timeout", "arguments": {}}, {"name": "modlogs", "arguments": {}}, {"name": "automod_status", "arguments": {}}])
    assert [c["name"] for c in kept] == ["warns", "timeout", "modlogs"] and len(kept) == a.max_tool_calls
    assert dropped == 8
    assert ag.vet_calls(a, None) == ([], 0)


def test_permissions_cooldown_and_per_person_scope():
    a = agent("moderation_assistant")
    assert not ag.has_needed_perms(a, perms()) and ag.has_needed_perms(a, perms(moderate_members=True))
    assert ag.has_needed_perms(agent("research_assistant"), perms())
    ag._last_used.clear()
    assert ag.check_cooldown(1, a, now=100.0) == (True, 0)
    ag.mark_used(1, a, now=100.0)
    ok, wait = ag.check_cooldown(1, a, now=105.0)
    assert not ok and wait >= 1 and ag.check_cooldown(2, a, now=105.0)[0]
    assert ag.check_cooldown(1, a, now=100.0 + a.cooldown_seconds + 1)[0]


def test_off_by_default_and_tools_only_offered_when_enabled_and_permitted(monkeypatch):
    async def ov(g):
        return {"server_manager": True}
    monkeypatch.setattr(ag, "overrides", ov)
    names = lambda p: {t["function"]["name"] for t in run(ag.tools_for(5, p))}
    assert names(perms(manage_guild=True)) == {"agent__server_manager"}      # enabled + permitted; others stay off
    assert names(perms()) == set()                                           # enabled but no permission
    async def none(g):
        return {}
    monkeypatch.setattr(ag, "overrides", none)
    assert names(perms(manage_guild=True, moderate_members=True)) == set()   # nothing on by default


def test_storage_failure_falls_back_to_off_and_fails_closed_on_the_limit(monkeypatch):
    async def boom():
        raise RuntimeError("db")
    monkeypatch.setattr(ag, "_pool", boom)
    ag._cache.clear()
    assert run(ag.overrides(5)) == {}
    assert run(ag.recent_task_count(5)) is None                               # caller refuses when it cannot count
    assert run(ag.start_task(5, 1, agent("research_assistant"), "x")) is None
    run(ag.finish_task(None, "x"))                                            # no-op, never raises
    run(ag.finish_task(3, "x"))                                               # swallowed


def test_set_enabled_rejects_unknown_agent():
    assert run(ag.set_enabled(1, "no_such_agent", True, 2)) is False


# ---- delegation through the cog, with fakes -------------------------------------------------------------------------

class Followup:
    def __init__(self):
        self.sent = []

    async def send(self, content=None, **kw):
        self.sent.append(content)


def make_ctx(**p):
    return SimpleNamespace(guild=SimpleNamespace(id=9), user=SimpleNamespace(id=7), permissions=perms(**p), followup=Followup())


@pytest.fixture
def cog(monkeypatch):
    from discord_bot.cogs import ai_tools
    c = ai_tools.AIToolsCog.__new__(ai_tools.AIToolsCog)
    state = SimpleNamespace(model_calls=[], finished=[], reply={"tool_calls": []}, count=0, enabled=True)

    async def is_enabled(g, a):
        return state.enabled

    async def count(g, hours=1):
        return state.count

    async def start(g, u, a, t):
        return 1

    async def finish(tid, status, proposed=None, note=""):
        state.finished.append((status, proposed))

    async def build(ctx):
        return [{"function": {"name": n}} for n in ("rank", "serveranalytics", "ban", "warns")]

    async def tier(u):
        return "basic"

    async def fake_chat(user_id, message, **kw):
        state.model_calls.append((message, kw))
        return state.reply
    monkeypatch.setattr(ag, "is_enabled", is_enabled)
    monkeypatch.setattr(ag, "recent_task_count", count)
    monkeypatch.setattr(ag, "start_task", start)
    monkeypatch.setattr(ag, "finish_task", finish)
    monkeypatch.setattr(ai_tools, "ai_chat", fake_chat)
    c._build_command_tools = build
    c._tier = tier
    ag._last_used.clear()
    c.state = state
    return c


def call(name="research_assistant", task="show server stats"):
    return {"name": "agent__" + name, "arguments": {"task": task}}


def test_delegation_runs_one_bounded_call_with_only_the_agents_tools(cog):
    cog.state.reply = {"tool_calls": [{"name": "serveranalytics", "arguments": {}}, {"name": "ban", "arguments": {"user": "1"}}]}
    ctx = make_ctx()
    out = run(cog._delegate_agent_calls(ctx, [call(), {"name": "rank", "arguments": {}}]))
    assert [c["name"] for c in out] == ["rank", "serveranalytics"]          # the ban the agent tried is gone
    (msg, kw), = cog.state.model_calls
    assert msg == "TASK: show server stats" and kw["history_override"] == []
    assert {t["function"]["name"] for t in kw["tools"]} == {"rank", "serveranalytics"}   # no ban, no warns (not this agent's)
    assert cog.state.finished[0][0] == "proposed"
    assert any("outside the agent" in (m or "") for m in ctx.followup.sent)


def test_disabled_missing_perms_cooldown_and_limit_run_nothing(cog):
    ctx = make_ctx()
    cog.state.enabled = False
    assert run(cog._delegate_agent_calls(ctx, [call()])) == [] and "turned off" in ctx.followup.sent[-1]
    cog.state.enabled = True
    assert run(cog._delegate_agent_calls(ctx, [call("moderation_assistant")])) == [] and "permissions" in ctx.followup.sent[-1]
    assert run(cog._delegate_agent_calls(ctx, [call(task="")])) == [] and "task" in ctx.followup.sent[-1].lower()
    cog.state.count = ag.SERVER_HOURLY_LIMIT
    assert run(cog._delegate_agent_calls(ctx, [call()])) == [] and "limit" in ctx.followup.sent[-1]
    cog.state.count = 0
    run(cog._delegate_agent_calls(ctx, [call()]))                              # a good run...
    n = len(cog.state.model_calls)
    assert run(cog._delegate_agent_calls(ctx, [call()])) == [] and "wait" in ctx.followup.sent[-1].lower()
    assert len(cog.state.model_calls) == n                                      # ...and the cooldown made no model call
    assert run(cog._delegate_agent_calls(ctx, [call("nope")])) == [] and "don't know" in ctx.followup.sent[-1]


def test_only_one_agent_per_message_and_no_nesting(cog):
    cog.state.reply = {"tool_calls": [{"name": "agent__research_assistant", "arguments": {"task": "x"}},
                                      {"name": "skill__lockdown_channel", "arguments": {}}]}
    ctx = make_ctx(manage_guild=True)
    out = run(cog._delegate_agent_calls(ctx, [call(), call("server_manager")]))
    assert out == [] and len(cog.state.model_calls) == 1                        # nested agent / skill calls dropped
    assert any("One agent per message" in (m or "") for m in ctx.followup.sent)


def test_agent_answer_text_timeout_and_error_are_reported_not_raised(cog, monkeypatch):
    from discord_bot.cogs import ai_tools
    ctx = make_ctx()
    cog.state.reply = "That is outside what I can do."
    assert run(cog._delegate_agent_calls(ctx, [call()])) == [] and "outside what I can do" in ctx.followup.sent[-1]
    assert cog.state.finished[-1][0] == "answered"

    async def slow(*a, **k):
        await asyncio.sleep(5)
    monkeypatch.setattr(ai_tools, "ai_chat", slow)
    monkeypatch.setattr(ag, "TASK_TIMEOUT_SECONDS", 0.05)
    ag._last_used.clear()
    assert run(cog._delegate_agent_calls(ctx, [call()])) == [] and "too long" in ctx.followup.sent[-1]

    async def err(*a, **k):
        raise RuntimeError("groq down")
    monkeypatch.setattr(ai_tools, "ai_chat", err)
    ag._last_used.clear()
    assert run(cog._delegate_agent_calls(ctx, [call()])) == [] and "error" in ctx.followup.sent[-1]
    assert [s for s, _ in cog.state.finished][-2:] == ["timeout", "error"]


def test_person_with_no_matching_tools_gets_a_plain_refusal(cog):
    async def none(ctx):
        return [{"function": {"name": "ban"}}]
    cog._build_command_tools = none
    ctx = make_ctx()
    assert run(cog._delegate_agent_calls(ctx, [call()])) == [] and "can't run any" in ctx.followup.sent[-1]
    assert cog.state.model_calls == []


# ---- wiring ---------------------------------------------------------------------------------------------------------

def test_wiring_and_isolation():
    tools = (ROOT / "discord_bot/cogs/ai_tools.py").read_text()
    assert "_delegate_agent_calls(ctx, calls)" in tools and "ai_agents.tools_for" in tools
    assert tools.index("_delegate_agent_calls(ctx, calls)") < tools.index("_expand_skill_calls(ctx, calls)") < tools.index("ai_bulk.pick_tool_calls(calls)")
    from modules.ai_command_allowlist import AI_COMMANDS
    assert not [c for c in AI_COMMANDS if "agent" in c.name]                    # the AI cannot toggle or read agents
    for f in ("modules/ai_command_guard.py", "modules/ai_command_allowlist.py", "modules/ai_plan.py"):
        assert "ai_agents" not in (ROOT / f).read_text()                        # the safeguards never import agents
    bot = (ROOT / "discord_bot/bot.py").read_text()
    for ext in ("ai_skills", "ai_agents", "ai_memory", "ai_jobs"):
        assert f'"{ext}"' in bot
    assert bot.index('"ai_skills", "ai_agents"') > bot.index("discord_bot.cogs.my_data")   # loaded after the existing commands
