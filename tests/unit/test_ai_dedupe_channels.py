"""Duplicate-channel cleanup: keep ONE of each same-named channel, never touch protected ones."""
from modules import ai_bulk
from modules.ai_command_allowlist import AI_COMMANDS_BY_NAME, AI_COMMANDS
from modules.ai_tool_select import select_tools


def ch(i, key, kind="text", protected=False, has_children=False):
    return {"id": i, "kind": kind, "key": key, "protected": protected, "has_children": has_children}


def test_keeps_the_oldest_and_deletes_the_copies():
    plan = ai_bulk.plan_dedupe([ch(30, "general"), ch(10, "general"), ch(20, "general"), ch(5, "memes")])
    assert plan["delete"] == [20, 30]
    assert plan["groups"][0]["keep"] == [10]
    assert plan["more"] == 0 and plan["skipped"] == []


def test_same_name_different_type_is_not_a_duplicate():
    assert ai_bulk.plan_dedupe([ch(1, "chill", "text"), ch(2, "chill", "voice")])["delete"] == []


def test_protected_channel_is_the_one_kept_even_if_newer():
    plan = ai_bulk.plan_dedupe([ch(1, "rules"), ch(2, "rules", protected=True), ch(3, "rules")])
    assert plan["groups"][0]["keep"] == [2] and plan["delete"] == [1, 3]


def test_two_protected_copies_are_both_kept():
    plan = ai_bulk.plan_dedupe([ch(1, "x", protected=True), ch(2, "x", protected=True), ch(3, "x")])
    assert plan["delete"] == [3] and plan["groups"][0]["keep"] == [1, 2]


def test_duplicate_category_with_channels_is_never_deleted():
    plan = ai_bulk.plan_dedupe([ch(1, "info", "category"), ch(2, "info", "category", has_children=True),
                                ch(3, "info", "category")])
    assert plan["delete"] == [3]
    assert plan["skipped"] == [(2, "it still has channels in it")]


def test_cap_and_leftover_count():
    items = [ch(i, "a") for i in range(1, 15)]                    # 1 kept, 13 copies
    plan = ai_bulk.plan_dedupe(items)
    assert len(plan["delete"]) == ai_bulk.MAX_DELETE_CHANNELS and plan["more"] == 3
    assert all(i in plan["delete"] for g in plan["groups"] for i in g["delete"])


def test_only_names_limits_the_scope_and_blank_keys_are_ignored():
    items = [ch(1, "a"), ch(2, "a"), ch(3, "b"), ch(4, "b"), ch(5, ""), ch(6, "")]
    assert ai_bulk.plan_dedupe(items, only_keys=["B"])["delete"] == [4]
    assert ai_bulk.plan_dedupe(items)["delete"] == [2, 4]


def test_ai_is_offered_the_tool_for_the_exact_request_from_the_screenshot():
    spec = AI_COMMANDS_BY_NAME["setup_dedupe_channels"]
    assert spec.ai_only and "manage_channels" in spec.needs_perms
    tools = [{"type": "function", "function": {"name": s.name, "description": s.description, "parameters": {"properties": {}}}}
             for s in AI_COMMANDS]
    picked = select_tools(tools, "DELETE CHANNELS THAT ARE DUPLICATES I MEAN SAME NAMES MAINTAIN ONE")
    assert picked and picked[0]["function"]["name"] == "setup_dedupe_channels"


# --- Discord layer, with stand-in channel objects -------------------------------------------------------------
from types import SimpleNamespace
from unittest.mock import MagicMock

import discord

from discord_bot.cogs import _ai_bulk_ops as ops


def _chan(cls, cid, name, can_manage=True, children=None):
    c = MagicMock(spec=cls)
    c.id, c.name = cid, name
    c.permissions_for.return_value = SimpleNamespace(manage_channels=can_manage)
    if children is not None:
        c.channels = children
    return c


def _guild(channels, rules=None):
    g = MagicMock()
    g.channels = channels
    g.rules_channel, g.public_updates_channel, g.system_channel, g.afk_channel = rules, None, None, None
    return g


def test_dedupe_plan_end_to_end_with_fancy_names_and_permissions():
    a = _chan(discord.TextChannel, 10, "general")
    b = _chan(discord.TextChannel, 20, "\U0001d41f\U0001d428\U0001d428" )          # unrelated fancy name
    c = _chan(discord.TextChannel, 30, "General")                                    # same name, other case
    d = _chan(discord.TextChannel, 40, "general", can_manage=False)                   # asker can't manage it
    v = _chan(discord.VoiceChannel, 50, "general")                                    # other type: not a copy
    plan = ops.dedupe_plan(_guild([a, b, c, d, v]), [], asker=object(), current=None)
    assert [x.id for x in plan["delete"]] == [30]
    assert [(x.id, why) for x, why in plan["skipped"]] == [(40, "you don't have Manage Channels on it")]
    assert plan["groups"][0][0][0].id == 10


def test_dedupe_plan_keeps_the_rules_channel_and_the_current_channel():
    old = _chan(discord.TextChannel, 10, "rules")
    rules = _chan(discord.TextChannel, 20, "rules")
    here = _chan(discord.TextChannel, 30, "chat")
    dup_here = _chan(discord.TextChannel, 40, "chat")
    plan = ops.dedupe_plan(_guild([old, rules, here, dup_here], rules=rules), [], asker=object(), current=dup_here)
    assert sorted(x.id for x in plan["delete"]) == [10, 30]       # kept: the rules channel and the one we're in
