"""AI bulk commands: create channels (in a font), delete channels, delete one member's messages --
and the guards that stop a confused model from flooding the Discord API."""
import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from modules import ai_bulk
from modules.ai_command_allowlist import AI_COMMANDS_BY_NAME, is_command_allowed, needs_confirmation


def run(coro):
    return asyncio.run(coro)


# ── pure planning ─────────────────────────────────────────────────────────

def test_split_names_handles_separators_hashes_and_repeats():
    assert ai_bulk.split_names("general, #Memes ; staff-chat\nmemes|  ") == ["general", "Memes", "staff-chat"]
    assert ai_bulk.split_names(None) == [] and ai_bulk.split_names("  , ;") == []
    assert ai_bulk.split_names(["a", "b"]) == ["a", "b"]


def test_slug_text_voice_and_category():
    assert ai_bulk.slug("My Cool Channel!!") == "my-cool-channel"
    assert ai_bulk.slug("  Staff   Lounge ", voice=True) == "staff lounge"
    assert ai_bulk.slug("Game Night", category=True) == "Game Night"
    assert ai_bulk.slug("!!!") == ""


def test_plan_creation_caps_dedupes_and_flags_unusable_names():
    names = ",".join(f"chan{i}" for i in range(20)) + ",General,???"
    plan = ai_bulk.plan_creation(names, existing=["general"])
    assert len(plan["create"]) == ai_bulk.MAX_CREATE
    assert plan["duplicates"] == ["general"] and plan["invalid"] == ["???"]
    assert len(plan["over_cap"]) == 20 - ai_bulk.MAX_CREATE
    assert ai_bulk.plan_creation("a, A, a", []) ["create"] == ["a"]


def test_clamp_messages():
    assert ai_bulk.clamp_messages(5000) == ai_bulk.MAX_USER_MESSAGES
    assert ai_bulk.clamp_messages(0) == 1 and ai_bulk.clamp_messages("x") == 50


# ── one job per server, short cooldown, bot-wide cap ──────────────────────

def test_job_guard_lock_cooldown_and_global_cap():
    g = ai_bulk.JobGuard()
    assert g.try_start(1, now=100) is None
    assert "already running" in g.try_start(1, now=101)
    g.finish(1, now=110)
    assert "moment" in g.try_start(1, now=111)
    assert g.try_start(1, now=110 + ai_bulk.JOB_COOLDOWN_SECONDS + 1) is None
    g2 = ai_bulk.JobGuard()
    for gid in range(ai_bulk.MAX_RUNNING_JOBS):
        assert g2.try_start(gid, now=0) is None
    assert "busy" in g2.try_start(99, now=0)


def test_call_deduper_drops_identical_repeats_only_inside_the_window():
    d = ai_bulk.CallDeduper(window=15)
    args = {"names": "a,b", "kind": "text"}
    assert not d.is_duplicate(1, "setup_bulk_create", args, now=0)
    assert d.is_duplicate(1, "setup_bulk_create", dict(reversed(list(args.items()))), now=5)   # key order irrelevant
    assert not d.is_duplicate(2, "setup_bulk_create", args, now=5)                             # other user
    assert not d.is_duplicate(1, "setup_bulk_create", {"names": "c"}, now=5)                   # other args
    assert not d.is_duplicate(1, "setup_bulk_create", args, now=20)                            # window over


def test_only_the_first_tool_call_is_ever_picked():
    calls = [{"name": "setup_bulk_create", "arguments": {}}, {"name": "kick"}, {"name": "ban"}, "junk", {}]
    call, dropped = ai_bulk.pick_tool_call(calls)
    assert call["name"] == "setup_bulk_create" and dropped == 2
    assert ai_bulk.pick_tool_call([]) == (None, 0) and ai_bulk.pick_tool_call(None) == (None, 0)


def test_pick_tool_calls_keeps_up_to_ten_distinct_and_merges_repeats():
    calls = [{"name": f"cmd{i}", "arguments": {"a": i}} for i in range(12)] + [{"name": "cmd0", "arguments": {"a": 0}}, "junk"]
    picked, dropped = ai_bulk.pick_tool_calls(calls)
    assert [c["name"] for c in picked] == [f"cmd{i}" for i in range(10)] and dropped == 2
    assert ai_bulk.pick_tool_calls(None) == ([], 0)


# ── allowlist entries ─────────────────────────────────────────────────────

@pytest.mark.parametrize("name,perm", [("setup_bulk_create", "manage_channels"), ("setup_bulk_delete", "manage_channels"),
                                       ("messages_user_purge", "manage_messages")])
def test_specs_are_allowed_confirmed_and_permission_gated(name, perm):
    spec = is_command_allowed(name)
    assert spec is not None and spec.requires_confirmation and perm in spec.needs_perms
    assert needs_confirmation(spec, {"names": "x"})
    assert "ONE call" in AI_COMMANDS_BY_NAME[name].description or name == "messages_user_purge"


# ── channel ops: sequential, paced, stop at first failure ─────────────────

@pytest.fixture
def ops():
    from discord_bot.cogs import _ai_bulk_ops
    return _ai_bulk_ops


def _fake_guild(creator):
    return SimpleNamespace(create_text_channel=creator, create_voice_channel=creator,
                           create_category=lambda name, **kw: creator(name, **kw))


def test_create_channels_is_sequential_with_pauses(ops, monkeypatch):
    sleeps, in_flight, peak = [], 0, 0
    real_sleep = asyncio.sleep

    async def fake_sleep(s):
        sleeps.append(s)

    async def creator(name, **kw):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await real_sleep(0)
        in_flight -= 1
        return SimpleNamespace(name=name, mention=f"#{name}")

    monkeypatch.setattr(ops.asyncio, "sleep", fake_sleep)
    made, stop = run(ops.create_channels(_fake_guild(creator), ["a", "b", "c"], "text", None, "r"))
    assert [c.name for c in made] == ["a", "b", "c"] and stop is None
    assert peak == 1                                    # never two Discord calls at once
    assert sleeps == [ai_bulk.PAUSE_SECONDS] * 2        # a pause between calls, none before the first


def test_create_channels_stops_at_the_first_failure(ops, monkeypatch):
    monkeypatch.setattr(ops.asyncio, "sleep", AsyncMock())
    calls = []

    async def creator(name, **kw):
        calls.append(name)
        if name == "b":
            raise discord.Forbidden(MagicMock(status=403), "nope")
        return SimpleNamespace(name=name, mention=name)

    made, stop = run(ops.create_channels(_fake_guild(creator), ["a", "b", "c", "d"], "text", None, "r"))
    assert calls == ["a", "b"] and len(made) == 1 and "Manage Channels" in stop


def test_a_hung_discord_call_stops_the_job(ops, monkeypatch):
    monkeypatch.setattr(ops.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(ops.ai_bulk, "CALL_TIMEOUT", 0.01)

    async def slow(name, **kw):
        await asyncio.Event().wait()

    made, stop = run(ops.create_channels(_fake_guild(slow), ["a", "b"], "voice", None, "r"))
    assert made == [] and "rate-limiting" in stop


def _chan(cls, name, cid, children=()):
    ch = MagicMock(spec=cls)
    ch.name, ch.id, ch.channels = name, cid, list(children)
    ch.delete = AsyncMock()
    return ch


def _guild_with(*chans, **special):
    g = MagicMock()
    g.channels = list(chans)
    g.get_channel = lambda i: next((c for c in chans if c.id == i), None)
    for attr in ("rules_channel", "public_updates_channel", "system_channel", "afk_channel"):
        setattr(g, attr, special.get(attr))
    return g


def test_resolve_channels_never_guesses(ops):
    a = _chan(discord.TextChannel, "memes", 1)
    b1, b2 = _chan(discord.TextChannel, "dup", 2), _chan(discord.VoiceChannel, "dup", 3)
    rules = _chan(discord.TextChannel, "rules", 4)
    here = _chan(discord.TextChannel, "chat", 5)
    g = _guild_with(a, b1, b2, rules, here, rules_channel=rules)
    res = ops.resolve_channels(g, ["#Memes", "dup", "ghost", "rules", "chat", "<#1>"], current=here)
    assert res["found"] == [a]                                       # the repeat mention of memes is not doubled
    assert res["missing"] == ["ghost"] and res["ambiguous"] == ["dup"]
    assert {c.name for c, _w in res["protected"]} == {"rules", "chat"}


def test_resolve_matches_fancy_font_names_and_protects_non_empty_categories(ops):
    fancy = _chan(discord.TextChannel, "\U0001d41e\U0001d42c\U0001d42c\U0001d422", 1)   # bold "essi"? plain-name fallback
    inner = _chan(discord.TextChannel, "inside", 2)
    cat = _chan(discord.CategoryChannel, "Stuff", 3, children=[inner])
    g = _guild_with(fancy, inner, cat)
    res = ops.resolve_channels(g, ["Stuff"])
    assert res["found"] == [] and res["protected"][0][0] is cat
    res = ops.resolve_channels(g, ["Stuff", "inside"])
    assert {c.name for c in res["found"]} == {"Stuff", "inside"}


def test_delete_channels_deletes_children_before_their_category_one_at_a_time(ops, monkeypatch):
    monkeypatch.setattr(ops.asyncio, "sleep", AsyncMock())
    order = []
    inner = _chan(discord.TextChannel, "inside", 2)
    cat = _chan(discord.CategoryChannel, "Stuff", 3, children=[inner])
    inner.delete = AsyncMock(side_effect=lambda **k: order.append("inside"))
    cat.delete = AsyncMock(side_effect=lambda **k: order.append("Stuff"))
    gone, stop = run(ops.delete_channels([cat, inner], "r"))
    assert order == ["inside", "Stuff"] and gone == ["inside", "Stuff"] and stop is None


def test_styled_name_applies_font_and_bracket(ops):
    plain = ops.styled_name("general", None, None, "text")
    assert plain == "general"
    bold = ops.styled_name("general", "bold", None, "text")
    assert bold != "general" and len(bold) <= 100
    wrapped = ops.styled_name("general", None, "cjk", "text")
    assert wrapped.startswith("\u3010") and wrapped.endswith("\u3011")


# ── the cog commands ──────────────────────────────────────────────────────

def _inter(manage_channels=True, bot_manage=True, channels=(), channel=None):
    guild = _guild_with(*channels)
    guild.categories = [c for c in channels if isinstance(c, discord.CategoryChannel)]
    guild.id = 7
    guild.me = SimpleNamespace(guild_permissions=SimpleNamespace(manage_channels=bot_manage))
    user = SimpleNamespace(id=42, guild_permissions=SimpleNamespace(manage_channels=manage_channels), __str__=lambda s: "u")
    return SimpleNamespace(guild=guild, user=user, channel=channel, client=SimpleNamespace(clone_id=None),
                           response=SimpleNamespace(is_done=lambda: False, defer=AsyncMock()), followup=SimpleNamespace(send=AsyncMock()))


@pytest.fixture
def setup_cog(monkeypatch):
    from discord_bot.cogs import setup_channels, style
    monkeypatch.setattr(style, "_premium_flags", AsyncMock(return_value=(True, True)))
    return setup_channels.SetupChannelsCog(MagicMock())


def test_bulk_create_plan_checks_permissions_before_any_confirm_button(setup_cog):
    err, _ = run(setup_cog._create_plan(_inter(manage_channels=False), "a,b"))
    assert "Manage Channels" in err
    err, _ = run(setup_cog._create_plan(_inter(bot_manage=False), "a,b"))
    assert "I don't have" in err
    err, _ = run(setup_cog._create_plan(_inter(), "a,b", kind="forum"))
    assert "Kind must be" in err
    err, _ = run(setup_cog._create_plan(_inter(), "a,b", font="not-a-font"))
    assert "font" in err
    err, _ = run(setup_cog._create_plan(_inter(), "a", category="Nope"))
    assert "couldn't find a category" in err


def test_bulk_create_plan_builds_the_exact_styled_names(setup_cog):
    existing = _chan(discord.TextChannel, "general", 1)
    err, plan = run(setup_cog._create_plan(_inter(channels=[existing]), "general, memes, Staff Chat",
                                           font="bold", bracket="cjk"))
    assert err is None and plan["duplicates"] == ["general"]
    assert plan["create"] == ["memes", "staff-chat"] and len(plan["final"]) == 2
    assert all(n.startswith("\u3010") and n.endswith("\u3011") for n in plan["final"])
    assert "Create **2** text channel(s)" in setup_cog._create_summary(plan)


def test_bulk_create_refuses_a_batch_over_the_channel_limit(setup_cog, ops=None):
    from discord_bot.cogs import _ai_bulk_ops
    inter = _inter()
    inter.guild.channels = [SimpleNamespace(name='x')] * (_ai_bulk_ops.GUILD_CHANNEL_LIMIT - 1)
    err, _ = run(setup_cog._create_plan(inter, "a,b,c"))
    assert "limit" in err


def test_bulk_create_runs_one_job_and_releases_the_lock(setup_cog, monkeypatch):
    from discord_bot.cogs import setup_channels as sc
    monkeypatch.setattr(sc.modx, "log_action", AsyncMock())
    seen = {}

    async def fake_create(guild, names, kind, category, reason):
        seen["locked"] = sc._ai_bulk.JOBS.is_running(guild.id)
        return [SimpleNamespace(name=n, mention=f"#{n}") for n in names], None

    monkeypatch.setattr(sc._ai_bulk_ops, "create_channels", fake_create)
    monkeypatch.setattr(sc._ai_bulk, "JOBS", ai_bulk.JobGuard())
    inter = _inter()
    plan = {"final": ["a", "b"], "kind": "text", "category": None}
    run(setup_cog._finish_create(inter, plan))
    assert seen["locked"] is True and not sc._ai_bulk.JOBS.is_running(7)
    assert "Created 2/2" in inter.followup.send.await_args.args[0]


def test_a_second_bulk_job_in_the_same_server_is_turned_away(setup_cog, monkeypatch):
    from discord_bot.cogs import setup_channels as sc
    guard = ai_bulk.JobGuard()
    guard.try_start(7)
    monkeypatch.setattr(sc._ai_bulk, "JOBS", guard)
    monkeypatch.setattr(sc._ai_bulk_ops, "create_channels", AsyncMock(side_effect=AssertionError("must not run")))
    inter = _inter()
    run(setup_cog._finish_create(inter, {"final": ["a"], "kind": "text", "category": None}))
    assert "already running" in inter.followup.send.await_args.args[0]


def test_bulk_delete_plan_caps_the_list_and_asks_for_names(setup_cog):
    err, _ = run(setup_cog._delete_plan(_inter(), ""))
    assert "which channels" in err.lower()
    err, _ = run(setup_cog._delete_plan(_inter(), ",".join(f"c{i}" for i in range(ai_bulk.MAX_DELETE_CHANNELS + 1))))
    assert "at most" in err


def test_bulk_delete_plan_lists_exactly_what_will_go_and_what_is_left_alone(setup_cog):
    a = _chan(discord.TextChannel, "old-news", 1)
    a.permissions_for = lambda u: SimpleNamespace(manage_channels=True)
    here = _chan(discord.TextChannel, "chat", 2)
    here.permissions_for = a.permissions_for
    err, res = run(setup_cog._delete_plan(_inter(channels=[a, here], channel=here), "old-news, chat, ghost"))
    assert err is None and res["found"] == [a]
    text = setup_cog._delete_summary(res)
    assert "old-news" in text and "ghost" in text and "chat" in text and "can't be undone" in text


# ── delete one member's messages ──────────────────────────────────────────

@pytest.fixture
def mod_cog(monkeypatch):
    from discord_bot.cogs import moderation
    monkeypatch.setattr(moderation.modx, "log_action", AsyncMock())
    monkeypatch.setattr(moderation, "_post_clear_log", AsyncMock())
    monkeypatch.setattr(moderation._ai_bulk, "JOBS", ai_bulk.JobGuard())
    return moderation.ModerationCog(MagicMock())


USER = SimpleNamespace(id=42)


def _text_channel(user_manage=True, bot_manage=True, bot_history=True):
    ch = MagicMock(spec=discord.TextChannel)
    ch.mention = "#room"
    ch.id = 9
    ch.permissions_for = lambda who: (SimpleNamespace(manage_messages=user_manage) if who is USER
                                      else SimpleNamespace(manage_messages=bot_manage, read_message_history=bot_history))
    return ch


def _mod_inter(channel):
    return SimpleNamespace(guild=SimpleNamespace(id=7, me="bot"), user=USER, channel=channel,
                           response=SimpleNamespace(is_done=lambda: True),
                           followup=SimpleNamespace(send=AsyncMock()))


def test_user_purge_plan_needs_member_and_both_permissions(mod_cog):
    member = SimpleNamespace(id=5, mention="@x")
    assert "Which member" in mod_cog._user_purge_plan(_mod_inter(_text_channel()), None, None, 10)[0]
    assert "You need" in mod_cog._user_purge_plan(_mod_inter(_text_channel(user_manage=False)), member, None, 10)[0]
    assert "I need" in mod_cog._user_purge_plan(_mod_inter(_text_channel(bot_history=False)), member, None, 10)[0]
    err, (m, ch, n) = mod_cog._user_purge_plan(_mod_inter(_text_channel()), member, None, 99999)
    assert err is None and n == ai_bulk.MAX_USER_MESSAGES


def test_user_purge_only_touches_that_member_unpinned_and_in_one_bounded_sweep(mod_cog):
    member = SimpleNamespace(id=5, mention="@x")
    ch = _text_channel()
    captured = {}

    async def fake_purge(**kw):
        captured.update(kw)
        msgs = ([SimpleNamespace(author=SimpleNamespace(id=5), pinned=False) for _ in range(5)]
                + [SimpleNamespace(author=SimpleNamespace(id=6), pinned=False),
                   SimpleNamespace(author=SimpleNamespace(id=5), pinned=True)])
        return [m for m in msgs if kw["check"](m)]

    ch.purge = fake_purge
    inter = _mod_inter(ch)
    run(mod_cog._finish_user_purge(inter, member, ch, 3))
    assert captured["limit"] == ai_bulk.MAX_SCAN_MESSAGES                       # bounded scan, not "everything"
    assert captured["bulk"] is True and captured["oldest_first"] is False       # newest first
    assert discord.utils.utcnow() - captured["after"] < timedelta(days=14)      # young enough for bulk delete
    assert "Deleted 3 message(s)" in inter.followup.send.await_args.args[0]     # capped at the amount asked for


def test_user_purge_releases_the_lock_even_when_discord_refuses(mod_cog):
    from discord_bot.cogs import moderation
    member = SimpleNamespace(id=5, mention="@x")
    ch = _text_channel()
    ch.purge = AsyncMock(side_effect=discord.Forbidden(MagicMock(status=403), "no"))
    inter = _mod_inter(ch)
    run(mod_cog._finish_user_purge(inter, member, ch, 5))
    assert not moderation._ai_bulk.JOBS.is_running(7)
    assert "permission" in inter.followup.send.await_args.args[0]


# ── AI dispatch: one command per reply, no stacked confirms ───────────────

def _ai_cog():
    from discord_bot.cogs.ai_tools import AIToolsCog
    cog = object.__new__(AIToolsCog)
    cog._dispatch_tool_call = AsyncMock()
    return cog


def test_ai_runs_several_tool_calls_as_one_batch_up_to_ten_and_says_what_was_skipped():
    cog = _ai_cog()
    cog._run_command_batch = AsyncMock()
    ctx = SimpleNamespace(followup=SimpleNamespace(send=AsyncMock()))
    calls = [{"name": f"cmd{i}", "arguments": {"n": i}} for i in range(12)]
    run(cog._run_tool_calls(ctx, calls, real_interaction=True))
    cog._dispatch_tool_call.assert_not_awaited()
    assert len(cog._run_command_batch.await_args.args[1]) == 10
    assert "skipped 2" in ctx.followup.send.await_args.args[0]


def test_ai_one_tool_call_still_goes_the_single_command_way():
    cog = _ai_cog()
    cog._run_command_batch = AsyncMock()
    ctx = SimpleNamespace(followup=SimpleNamespace(send=AsyncMock()))
    run(cog._run_tool_calls(ctx, [{"name": "kick", "arguments": {}}], real_interaction=True))
    cog._dispatch_tool_call.assert_awaited_once()
    cog._run_command_batch.assert_not_awaited()
    ctx.followup.send.assert_not_awaited()


def test_ai_clips_runaway_string_arguments():
    cog = _ai_cog()
    ctx = SimpleNamespace(followup=SimpleNamespace(send=AsyncMock()))
    run(cog._run_tool_calls(ctx, [{"name": "setup_bulk_create", "arguments": {"names": "x" * 5000, "n": 3}}],
                            real_interaction=True))
    sent = cog._dispatch_tool_call.await_args.args[2]
    assert len(sent["names"]) == 1000 and sent["n"] == 3
    ctx.followup.send.assert_not_awaited()


def test_ai_does_not_stack_a_second_confirm_for_an_identical_request(monkeypatch):
    from discord_bot.cogs import ai_tools
    from modules.ai_command_allowlist import AI_COMMANDS_BY_NAME
    cog = object.__new__(ai_tools.AIToolsCog)
    spec = AI_COMMANDS_BY_NAME["setup_bulk_delete"]
    command = SimpleNamespace(binding=None)
    monkeypatch.setattr(ai_tools, "resolve_and_check", AsyncMock(return_value=(spec, command, None, False)))
    cog._resolve_tool_args = lambda ctx, command, args: dict(args)
    monkeypatch.setattr(ai_tools.ai_bulk, "DEDUPER", ai_bulk.CallDeduper())
    ctx = SimpleNamespace(user=SimpleNamespace(id=1), followup=SimpleNamespace(send=AsyncMock()))
    run(cog._dispatch_tool_call(ctx, "setup_bulk_delete", {"names": "a,b"}, real_interaction=True))
    assert ctx.followup.send.await_args.kwargs.get("view") is not None            # first time: a confirm prompt
    run(cog._dispatch_tool_call(ctx, "setup_bulk_delete", {"names": "a,b"}, real_interaction=True))
    assert "already asked" in ctx.followup.send.await_args.args[0]                # repeat: no second prompt
    assert ctx.followup.send.await_count == 2
