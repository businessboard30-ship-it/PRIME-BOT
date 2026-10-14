"""The AI's "what can you do?" panel: when it triggers, who sees which commands, and that the panel is locked to the asker."""
import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import discord
import pytest

from modules import ai_capabilities as caps
from modules import ai_command_guard as guard
from modules.ai_command_allowlist import AI_COMMANDS, AICommandSpec


@pytest.mark.parametrize("text", [
    "what can you do", "What can you do?", "show me your commands", "list the commands", "what are the AI commands",
    "how do i use ai", "what features do you have", "what can I ask you",
])
def test_capability_questions_are_detected(text):
    assert caps.is_capabilities_question(text), text


@pytest.mark.parametrize("text", [
    "how do i ban someone", "make a role for each team", "what can i do about spam", "hello", "recommend an anime", "",
])
def test_ordinary_messages_are_not(text):
    assert not caps.is_capabilities_question(text), text


def _spec(name, module="moderation", ai_only=False, desc="Does a thing. More detail here."):
    return AICommandSpec(name, f"discord_bot.cogs.{module}", False, desc, path=name, ai_only=ai_only)


def test_pages_only_contain_the_specs_given_and_group_them():
    pages = caps.build_pages([_spec("kick"), _spec("setup bulk-create", "setup_channels", ai_only=True)])
    assert set(pages) == {"mod", "setup"}
    assert "`kick`" in pages["mod"] and "kick" not in pages["setup"]
    assert "🤖" in pages["setup"] and "🤖" not in pages["mod"]       # AI-only is flagged
    assert "More detail" not in pages["mod"]                          # one short sentence per command


def test_every_real_spec_gets_a_category_and_a_page_fits_discords_limit():
    pages = caps.build_pages(AI_COMMANDS)
    assert pages and all(len(t) < 3200 for t in pages.values())
    assert {caps.category_of(s) for s in AI_COMMANDS} <= {k for k, _, _ in caps.CATEGORIES}


def test_long_category_is_summarised_not_cut_mid_line():
    many = [_spec(f"cmd{i:03d}", desc="x" * 100) for i in range(80)]
    text = caps.build_pages(many)["mod"]
    assert "more. Just ask me." in text and len(text) < 3200


def _qualifying(perms, monkeypatch, cap_ok=True):
    monkeypatch.setattr(guard, "_resolve_command", lambda bot, path: SimpleNamespace_cmd())
    monkeypatch.setattr(guard, "check_real_permission", AsyncMock(return_value=(True, None)))
    monkeypatch.setattr(guard, "check_ai_command_cap", AsyncMock(return_value=(cap_ok, None)))
    inter = NS(guild=object(), user=NS(id=1), client=object(), permissions=perms)
    return inter


def SimpleNamespace_cmd():
    return NS(binding=None)


def test_a_member_without_manage_roles_is_not_shown_the_role_commands(monkeypatch):
    inter = _qualifying(discord.Permissions.none(), monkeypatch)
    paths = {s.command_path for s in asyncio.run(guard.get_qualifying_commands(inter))}
    assert "role bulk-create" not in paths and "role create" not in paths
    inter = _qualifying(discord.Permissions(manage_roles=True), monkeypatch)
    paths = {s.command_path for s in asyncio.run(guard.get_qualifying_commands(inter))}
    assert {"role create", "role bulk-create", "role bulk-delete"} <= paths


def test_display_can_ignore_the_daily_cap_but_running_still_respects_it(monkeypatch):
    inter = _qualifying(discord.Permissions(administrator=True), monkeypatch, cap_ok=False)
    assert asyncio.run(guard.get_qualifying_commands(inter)) == []
    assert asyncio.run(guard.get_qualifying_commands(inter, ignore_cap=True))


def _view(specs, asker=1, in_server=True):
    from discord_bot.cogs._views_ai_capabilities import CapabilitiesView
    return CapabilitiesView(specs, asker_id=asker, in_server=in_server)


def test_panel_builds_with_a_picker_and_stays_within_component_limits():
    v = _view(AI_COMMANDS)
    assert isinstance(v, discord.ui.LayoutView)
    assert len(list(v.walk_children())) <= 40
    select = next(c for c in v.walk_children() if isinstance(c, discord.ui.Select))
    assert [o.value for o in select.options][0] == "overview"
    for opt in select.options:                                   # every page renders and fits the 4000-char text limit
        v.render(opt.value)
        total = sum(len(c.content) for c in v.walk_children() if isinstance(c, discord.ui.TextDisplay))
        assert total < 4000, opt.value


def test_dm_panel_has_no_command_list_or_picker():
    v = _view([], in_server=False)
    assert not any(isinstance(c, discord.ui.Select) for c in v.walk_children())


def test_only_the_asker_can_use_the_panel():
    v = _view([_spec("kick")], asker=1)
    other = NS(user=NS(id=2), response=NS(send_message=AsyncMock()))
    assert asyncio.run(v.interaction_check(other)) is False
    other.response.send_message.assert_awaited_once()
    assert other.response.send_message.await_args.kwargs["ephemeral"] is True
    mine = NS(user=NS(id=1), response=NS(send_message=AsyncMock()))
    assert asyncio.run(v.interaction_check(mine)) is True
