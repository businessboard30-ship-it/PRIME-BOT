import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from modules import ai_choice, ai_tool_args
from modules.ai_tool_select import select_tools_multi, split_clauses

PARAMS = {"properties": {"user": {"type": "string"}, "days": {"type": "integer"}, "mode": {"type": "string", "enum": ["soft", "hard"]},
                         "dm": {"type": "boolean"}}, "required": ["user"]}


def run(c):
    return asyncio.new_event_loop().run_until_complete(c)


def test_clean_args_coerces_drops_unknown_and_reports_missing():
    r = ai_tool_args.clean_args({"user": "<@1>", "days": "7", "mode": "HARD", "dm": "yes", "bogus": 1}, PARAMS)
    assert r.args == {"user": "<@1>", "days": 7, "mode": "hard", "dm": True} and r.dropped == ["bogus"] and not r.problems
    r = ai_tool_args.clean_args({"days": "x", "user": "null"}, PARAMS)
    assert r.problems and "user" in r.missing and "days" in r.message("ban")
    assert ai_tool_args.clean_args({"user": "a", "mode": "weird"}, PARAMS).invalid
    assert ai_tool_args.clean_args(None, PARAMS).missing == ["user"]


def test_sanitize_bounds_and_rejects_unusable_questions():
    assert ai_choice.sanitize(None) is None and ai_choice.sanitize({"questions": [{"prompt": "x", "options": ["only"]}]}) is None
    spec = ai_choice.sanitize({"title": "t" * 500, "questions": [
        {"prompt": "Which?", "options": [{"label": "A", "value": "1"}, {"label": "B", "value": "1"}, "C", {"label": ""}]}] * 5})
    assert len(spec["title"]) == 100 and len(spec["questions"]) == ai_choice.MAX_QUESTIONS
    assert [o["value"] for o in spec["questions"][0]["options"]] == ["1", "C"]        # duplicate + empty dropped
    msg = ai_choice.followup_message(spec, {"q0": [1]}, "ban someone")
    assert "C (value: C)" in msg and "ban someone" in msg


def test_choice_tool_is_added_only_with_real_tools_and_below_depth_limit():
    from discord_bot.cogs.ai_tools import AIToolsCog
    tool = {"function": {"name": "kick"}}
    assert AIToolsCog._with_choice_tool(None, "ctx", 0) == (None, "ctx")
    tools, ctx = AIToolsCog._with_choice_tool([tool], None, 0)
    assert tools[-1]["function"]["name"] == ai_choice.CHOICE_TOOL_NAME and "COMMAND RULES" in ctx
    assert AIToolsCog._with_choice_tool([tool], "c", ai_choice.MAX_CHOICE_DEPTH) == ([tool], "c")


def _tool(name, desc):
    return {"type": "function", "function": {"name": name, "description": desc, "parameters": {"type": "object", "properties": {}}}}


def test_every_part_of_a_many_demands_sentence_gets_its_own_tools():
    tools = [_tool("ban", "ban a member"), _tool("kick", "kick a member"), _tool("lock_channel", "lock a channel"),
             _tool("slowmode", "set channel slowmode"), _tool("announce", "send an announcement"), _tool("giveaway", "start a giveaway")]
    msg = "ban the spammer, lock this channel, set slowmode and start a giveaway then send an announcement"
    assert len(split_clauses(msg)) >= 4
    names = {t["function"]["name"] for t in select_tools_multi(tools, msg)}
    assert {"ban", "lock_channel", "slowmode", "giveaway", "announce"} <= names


def test_choice_views_build_with_real_discord_components():
    from discord_bot.cogs._views_ai_choice import AIChoiceView
    fast = ai_choice.sanitize({"questions": [{"prompt": "Which?", "options": ["a", "b", "c"]}]})
    big = ai_choice.sanitize({"questions": [
        {"prompt": "One", "options": [f"o{i}" for i in range(25)]},
        {"prompt": "Many", "multiple": True, "options": [f"m{i}" for i in range(8)]}]})
    async def build():
        v1, v2 = AIChoiceView(fast, 1, AsyncMock()), AIChoiceView(big, 1, AsyncMock())
        return v1, v2
    v1, v2 = run(build())
    assert v1._fast and not v2._fast
    assert v1.to_components() and v2.to_components()
    assert not any(i.disabled for i in v1.walk_children() if hasattr(i, "label") and i.label == "Choose")
    sub = [i for i in v2.walk_children() if getattr(i, "label", "") == "Submit"][0]
    assert sub.disabled                                                              # nothing answered yet


def test_a_choice_call_asks_first_and_runs_nothing_else():
    from discord_bot.cogs import ai_tools
    cog = object.__new__(ai_tools.AIToolsCog)
    cog._dispatch_tool_call, cog._run_command_batch, cog._ask_choice = AsyncMock(), AsyncMock(), AsyncMock()
    ctx = SimpleNamespace(followup=SimpleNamespace(send=AsyncMock()))
    calls = [{"name": "kick", "arguments": {}}, {"name": ai_choice.CHOICE_TOOL_NAME, "arguments": {"title": "x"}}]
    run(cog._run_tool_calls(ctx, calls, real_interaction=True, origin_message="kick him"))
    cog._ask_choice.assert_awaited_once()
    assert cog._ask_choice.await_args.kwargs["origin_message"] == "kick him"
    cog._dispatch_tool_call.assert_not_awaited()
    cog._run_command_batch.assert_not_awaited()
