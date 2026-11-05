"""Phase 1 planner: decompose -> chunked resolve. Pure parsing/chunking plus the cog path with the model mocked out."""
import asyncio
import json
from types import SimpleNamespace as N
from unittest.mock import AsyncMock

from discord_bot.cogs import ai_tools as at
from modules import ai_plan, ai_planner as P


def run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def test_ceiling_matches_the_plan_runner():
    assert P.MAX_TASKS == ai_plan.MAX_STEPS


def test_should_plan_only_for_many_part_requests():
    assert not P.should_plan("lock general and then make a role")
    assert not P.should_plan("create channels gallo, news, memes, staff, music, games, art and chat")      # ONE bulk command's names
    assert P.should_plan("make a category gallo. add 3 channels. lock general. create role mod. give mod to sam. rename the server")
    assert P.should_plan("1. make category gallo\n2. add channels\n3. lock general\n4. create role mod")
    assert P.should_plan("ban sam, kick joe, mute ann, lock chat, pin rules, make role mod, add emoji fox, set slowmode, rename lobby, hide logs, open news")                                                 # 10+ comma parts: plan it
    assert not P.should_plan("")


def test_parse_clean_json_renumbers_and_keeps_order():
    raw = json.dumps({"tasks": [{"id": 7, "task": "create category gallo"},
                                {"id": 9, "task": "create channels a, b in gallo", "depends_on": [7]}]})
    tasks, info = P.parse_tasks(raw)
    assert [t["id"] for t in tasks] == [1, 2] and tasks[1]["depends_on"] == [1]


def test_parse_survives_fences_prose_and_bare_lists():
    raw = "Sure!\n```json\n[\"lock general\", {\"task\": \"make role mod\"}]\n```"
    tasks, _ = P.parse_tasks(raw)
    assert [t["task"] for t in tasks] == ["lock general", "make role mod"]
    assert P.parse_tasks("no json here") == ([], {"dropped_over_limit": 0, "duplicates": 0, "bad": 0})
    assert P.parse_tasks(None)[0] == []


def test_parse_drops_forward_unknown_and_self_dependencies():
    raw = json.dumps({"tasks": [{"id": 1, "task": "a", "depends_on": [2, 99, 1]}, {"id": 2, "task": "b", "depends_on": [1]}]})
    tasks, _ = P.parse_tasks(raw)
    assert tasks[0]["depends_on"] == [] and tasks[1]["depends_on"] == [1]          # no forward refs, so no cycles


def test_parse_bounds_duplicates_blanks_and_length():
    raw = json.dumps({"tasks": [{"task": f"task {i}"} for i in range(40)] + [{"task": "task 1"}, {"task": "  "}, 5]})
    tasks, info = P.parse_tasks(raw)
    assert len(tasks) == P.MAX_TASKS and info["dropped_over_limit"] == 15 and info["duplicates"] == 1 and info["bad"] == 2
    assert len(P.parse_tasks(json.dumps(["x" * 900]))[0][0]["task"]) == P.MAX_TASK_CHARS


def test_chunks_keep_order_and_cover_everything():
    tasks = [{"id": i, "task": f"t{i}", "depends_on": []} for i in range(1, 24)]
    chunks = P.chunk_tasks(tasks)
    assert [t["id"] for c in chunks for t in c] == list(range(1, 24)) and all(len(c) <= P.CHUNK_SIZE for c in chunks)


def test_chunk_prompt_names_tasks_and_earlier_work():
    text = P.chunk_prompt("do things", [{"id": 5, "task": "lock general"}], [{"id": 1, "task": "make category gallo"}])
    assert "5. lock general" in text and "do NOT call tools" in text and "1. make category gallo" in text


def test_unresolved_reports_chunks_with_no_calls():
    a, b = [{"id": 1, "task": "x"}], [{"id": 2, "task": "y"}]
    assert P.unresolved(a + b, [(a, 1), (b, 0)]) == b


# ── the cog path ────────────────────────────────────────────────────────────
def tool(name):
    return {"type": "function", "function": {"name": name, "description": f"{name} things", "parameters": {"type": "object", "properties": {}}}}


def make_cog(monkeypatch, tasks, replies):
    cog = at.AIToolsCog.__new__(at.AIToolsCog)
    monkeypatch.setattr(at.ai_planner, "decompose", AsyncMock(return_value=(tasks, {"dropped_over_limit": 0, "duplicates": 0, "bad": 0})))
    monkeypatch.setattr(at.ai_planner, "CHUNK_PAUSE_SECONDS", 0)
    monkeypatch.setattr(at.ai_planner, "TASK_PAUSE_SECONDS", 0)
    chat = AsyncMock(side_effect=replies)

    async def fake_resolve(prompt, tools, **_kw):            # the lean planner call; `replies` script what the model returns
        r = await chat(prompt, tools)
        return list(r.get("tool_calls") or []) if isinstance(r, dict) else []
    monkeypatch.setattr(at.ai_planner, "resolve_calls", fake_resolve)
    ctx = N(guild=N(id=1), followup=N(send=AsyncMock()))
    return cog, ctx, chat


def test_cog_resolves_each_chunk_separately_and_merges_in_order(monkeypatch):
    tasks = [{"id": i, "task": f"create role r{i}", "depends_on": []} for i in range(1, 10)]     # 9 tasks -> 3 chunks of 4,4,1
    replies = [{"tool_calls": [{"name": "role_create", "arguments": {"name": f"r{i}"}} for i in range(s, e)]} for s, e in ((1, 5), (5, 9), (9, 10))]
    cog, ctx, chat = make_cog(monkeypatch, tasks, replies)
    calls = run(cog._plan_and_resolve(1, "make roles", [tool("role_create")], ctx))
    assert [c["arguments"]["name"] for c in calls] == [f"r{i}" for i in range(1, 10)] and chat.await_count == 3


def test_cog_tells_the_person_which_tasks_found_no_command(monkeypatch):
    tasks = [{"id": 1, "task": "create role mod", "depends_on": []}, {"id": 2, "task": "create role admin", "depends_on": []}]
    cog, ctx, _ = make_cog(monkeypatch, tasks, ["just chat, no tools"] * 3)       # the chunk, then each task on its own
    assert run(cog._plan_and_resolve(1, "x", [tool("role_create")], ctx)) == []
    notes = " ".join(str(c.args[0]) for c in ctx.followup.send.await_args_list)
    assert "create role mod" in notes and "left them out" in notes


def test_cog_falls_back_when_planning_yields_too_little(monkeypatch):
    cog, ctx, chat = make_cog(monkeypatch, [{"id": 1, "task": "only one", "depends_on": []}], [])
    assert run(cog._plan_and_resolve(1, "x", [tool("role_create")], ctx)) == [] and chat.await_count == 0


def test_cog_never_raises_into_the_chat(monkeypatch):
    cog, ctx, _ = make_cog(monkeypatch, [], [])
    monkeypatch.setattr(at.ai_planner, "decompose", AsyncMock(side_effect=RuntimeError("boom")))
    assert run(cog._plan_and_resolve(1, "x", [tool("role_create")], ctx)) == []


def test_cog_tags_calls_with_task_links_only_when_one_call_per_task(monkeypatch):
    tasks = [{"id": 1, "task": "make channels category gallo", "depends_on": []},
             {"id": 2, "task": "add channels in gallo", "depends_on": [1]}]
    cog, ctx, _ = make_cog(monkeypatch, tasks, [{"tool_calls": [{"name": "channels_bulk_create", "arguments": {"kind": "category"}},
                                                               {"name": "channels_bulk_create", "arguments": {"names": "a"}}]}])
    calls = run(cog._plan_and_resolve(1, "x", [tool("channels_bulk_create")], ctx))
    assert [(c["task_id"], c["depends_on_tasks"]) for c in calls] == [("1", []), ("2", ["1"])]

    # too few calls for the chunk: every task is resolved on its OWN, so nothing is dropped and each step is linked to its task
    one = {"tool_calls": [{"name": "channels_bulk_create", "arguments": {"kind": "category"}}]}
    two = {"tool_calls": [{"name": "channels_bulk_create", "arguments": {"names": "a"}}]}
    cog, ctx, chat = make_cog(monkeypatch, tasks, [one, one, two])
    calls = run(cog._plan_and_resolve(1, "x", [tool("channels_bulk_create")], ctx))
    assert chat.await_count == 3 and [(c["task_id"], c["depends_on_tasks"]) for c in calls] == [("1", []), ("2", ["1"])]


def test_a_chunk_that_resolves_only_some_tasks_no_longer_drops_the_rest(monkeypatch):
    """The 14-task bug: the model answered 2 calls for a 4-task chunk and the other 2 tasks vanished without a word."""
    tasks = [{"id": i, "task": f"create role r{i}", "depends_on": []} for i in range(1, 5)]
    partial = {"tool_calls": [{"name": "role_create", "arguments": {"name": "r1"}}, {"name": "role_create", "arguments": {"name": "r3"}}]}
    singles = [{"tool_calls": [{"name": "role_create", "arguments": {"name": f"r{i}"}}]} for i in range(1, 5)]
    cog, ctx, _ = make_cog(monkeypatch, tasks, [partial] + singles)
    calls = run(cog._plan_and_resolve(1, "make roles", [tool("role_create")], ctx))
    assert [c["arguments"]["name"] for c in calls] == ["r1", "r2", "r3", "r4"]


def test_a_task_that_still_finds_no_command_is_reported_not_silently_dropped(monkeypatch):
    tasks = [{"id": i, "task": f"create role r{i}", "depends_on": []} for i in range(1, 4)]
    partial = {"tool_calls": [{"name": "role_create", "arguments": {"name": "r1"}}]}
    ok = lambda n: {"tool_calls": [{"name": "role_create", "arguments": {"name": n}}]}
    cog, ctx, _ = make_cog(monkeypatch, tasks, [partial, ok("r1"), "no tool for this", ok("r3")])
    calls = run(cog._plan_and_resolve(1, "x", [tool("role_create")], ctx))
    assert [c["arguments"]["name"] for c in calls] == ["r1", "r3"]
    notes = " ".join(str(c.args[0]) for c in ctx.followup.send.await_args_list)
    assert "create role r2" in notes and "left them out" in notes


# ── Phase 3 additions: chunking and dependency order for 20+ tasks ──
def _tasks(n, dep_on_prev=False):
    return [{"id": i, "task": f"task {i}", "depends_on": ([i - 1] if dep_on_prev and i > 1 else [])} for i in range(1, n + 1)]


def test_twenty_five_tasks_chunk_into_small_ordered_groups():
    chunks = P.chunk_tasks(_tasks(25))
    assert [len(c) for c in chunks] == [4, 4, 4, 4, 4, 4, 1]
    assert [t["id"] for c in chunks for t in c] == list(range(1, 26))


def test_a_task_never_lands_in_an_earlier_chunk_than_what_it_depends_on():
    tasks = _tasks(22, dep_on_prev=True)
    where = {t["id"]: n for n, c in enumerate(P.chunk_tasks(tasks, size=3)) for t in c}
    assert all(where[d] <= where[t["id"]] for t in tasks for d in t["depends_on"])


def test_chunk_prompt_names_earlier_tasks_but_only_the_last_few():
    tasks = _tasks(20)
    prompt = P.chunk_prompt("do all of it", tasks[12:16], tasks[:12])
    assert "13. task 13" in prompt and "16. task 16" in prompt
    assert "12. task 12" in prompt and "6. task 6" not in prompt         # only the last 6 earlier tasks are named


def test_parse_tasks_survives_a_25_task_answer_and_drops_forward_dependencies():
    items = [{"id": i, "task": f"make thing {i}", "depends_on": [i - 1, i + 5]} for i in range(1, 31)]
    tasks, info = P.parse_tasks(json.dumps({"tasks": items}))
    assert len(tasks) == P.MAX_TASKS == ai_plan.MAX_STEPS and info["dropped_over_limit"] == 5
    assert all(d < t["id"] for t in tasks for d in t["depends_on"])


# ── the model must never be allowed to INVENT a finished plan ──
BIG = ('Set up my test server. First create a category called "Test Hub". Then create 4 text channels inside Test Hub named '
       'announcements, rules, general-chat and bot-commands. Then create a category called "Voice Zone" and create 3 voice '
       'channels inside it named Lounge, Gaming and Music. Then create these roles: Admin Test, Mod Test, Member Test, VIP Test. '
       'Next lock the rules channel. Also set slowmode on general-chat to 10 seconds. Then post a welcome message in announcements. '
       'Finally show me the server stats.')


def test_the_big_test_prompt_goes_to_the_planner():
    assert P.should_plan(BIG)


def test_an_invented_done_list_is_detected_and_a_real_answer_is_not():
    fake = "\u2705 created category **Test Hub**\n\u2705 created roles: `A`, `B`\n\U0001F4CA Server stats: 1 member"
    assert P.claims_unrun_actions(fake)
    assert not P.claims_unrun_actions("Sure! I can create a category and channels. Want me to go ahead?")
    assert not P.claims_unrun_actions("\u2705 done")                              # one tick alone is ordinary chat
    assert "nothing was created" in P.UNRUN_NOTICE


def test_reply_path_plans_a_many_part_request_and_runs_it_on_the_guarded_path(monkeypatch):
    cog = at.AIToolsCog(N())
    calls = [{"name": "cat_make", "arguments": {}}]
    cog._plan_and_resolve = AsyncMock(return_value=calls)
    cog._run_tool_calls = AsyncMock()
    seen = {}

    async def fake_chat(*a, **k):
        seen["chat"] = True
        return "plain"

    monkeypatch.setattr(at, "ai_chat", fake_chat)
    src = open(at.__file__, encoding="utf-8").read()
    assert "planned = await self._plan_and_resolve(user_id, content, tools, proxy)" in src
    assert "real_interaction=False, origin_message=content)\n                return \"\", None" in src


# ── Groq rate limits (8000 tokens/minute): wait and retry, lean calls ──
def test_parse_retry_after_reads_groqs_hint():
    assert P.parse_retry_after("Please try again in 9.81s. Need more tokens?") == 9.81
    assert P.parse_retry_after("try again in 250ms") == 0.25
    assert abs(P.parse_retry_after("try again in 1m5.2s") - 65.2) < 1e-9
    assert P.parse_retry_after("something else") == 0.0 and P.parse_retry_after(None) == 0.0


def _body(*calls):
    msg = {"tool_calls": [{"function": {"name": n, "arguments": json.dumps(a)}} for n, a in calls]}
    return {"choices": [{"message": msg}]}


def test_resolve_calls_waits_out_a_429_then_succeeds(monkeypatch):
    from modules import ai_features as af
    post = AsyncMock(side_effect=[(429, None, "Please try again in 9.81s"), (429, None, "try again in 2s"),
                                  (200, _body(("role_create", {"name": "A", "color": None})), None)])
    monkeypatch.setattr(af, "_groq_post", post)
    slept = []

    async def nosleep(sec):
        slept.append(sec)

    calls = run(P.resolve_calls("p", [tool("role_create")], sleep=nosleep))
    assert calls == [{"name": "role_create", "arguments": {"name": "A"}}]          # null argument dropped
    assert post.await_count == 3 and abs(slept[0] - 10.31) < 1e-6 and slept[1] == 2.5


def test_resolve_calls_gives_up_after_a_bounded_number_of_waits(monkeypatch):
    from modules import ai_features as af
    post = AsyncMock(return_value=(429, None, "try again in 60s"))
    monkeypatch.setattr(af, "_groq_post", post)
    slept = []

    async def nosleep(sec):
        slept.append(sec)

    assert run(P.resolve_calls("p", [tool("role_create")], sleep=nosleep)) == []
    assert post.await_count == P.MAX_RATE_WAITS + 1 and max(slept) <= P.MAX_RATE_WAIT_SECONDS


def test_resolve_calls_request_is_lean_and_other_errors_do_not_loop(monkeypatch):
    from modules import ai_features as af
    post = AsyncMock(return_value=(500, None, "boom"))
    monkeypatch.setattr(af, "_groq_post", post)
    assert run(P.resolve_calls("task text", [tool("role_create")])) == []
    assert post.await_count == 1
    payload = post.await_args.args[0]
    assert [m["role"] for m in payload["messages"]] == ["system", "user"] and len(payload["messages"][0]["content"]) < 400
    assert payload["tools"] and payload["tool_choice"] == "auto"
