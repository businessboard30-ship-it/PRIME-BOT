"""AI level/rank answers: asker's own global numbers + Open dashboard link button."""
import asyncio
import types

import discord
import pytest

import config
from discord_bot.cogs import ai_tools
from modules import ai_features as af


def run(c):
    return asyncio.run(c)


class FakeGuild:
    id = 55

    def get_member(self, uid):
        return types.SimpleNamespace(display_name=f"m{uid}")


@pytest.fixture()
def cog(monkeypatch):
    c = ai_tools.AIToolsCog(types.SimpleNamespace(clone_id=None, application_id=1, user=types.SimpleNamespace(id=1)))
    calls = {"global": []}

    async def get_xp(gid, uid, clone_id=None):
        return {"total_xp": 500}

    async def get_xp_rank(gid, uid, clone_id=None):
        return {"rank": 3, "total_players": 40}

    async def get_global(uid):
        calls["global"].append(uid)
        return {"total_xp": 2000, "rank": 7, "total_players": 900}
    monkeypatch.setattr(ai_tools.db, "get_xp", get_xp, raising=False)
    monkeypatch.setattr(ai_tools.db, "get_xp_rank", get_xp_rank, raising=False)
    monkeypatch.setattr(ai_tools.db, "get_global_xp_rank", get_global, raising=False)
    c.calls = calls
    return c


def test_facts_include_asker_global_rank_and_server_numbers(cog):
    t = run(cog._xp_facts("what's my level and rank", 10, FakeGuild()))
    assert "GLOBAL" in t and "global rank #7 of 900" in t
    assert "THIS SERVER" in t and "rank #3 of 40" in t
    assert "dashboard" in t
    assert cog.calls["global"] == [10]


def test_mentioned_member_gets_server_stats_only_never_global(cog):
    t = run(cog._xp_facts("compare my rank with <@222>", 10, FakeGuild()))
    assert "m222" in t
    assert cog.calls["global"] == [10]            # only the asker's global lookup


def test_dm_answers_with_global_numbers(cog):
    t = run(cog._xp_facts("what is my rank", 10, None))
    assert "global rank #7 of 900" in t and "ask this in a server" not in t


def test_no_xp_user_is_not_ranked_yet(cog, monkeypatch):
    async def none(uid):
        return None
    monkeypatch.setattr(ai_tools.db, "get_global_xp_rank", none, raising=False)
    assert "not ranked yet" in run(cog._xp_facts("my xp?", 10, None))


def test_clone_has_no_global_numbers_and_dm_falls_back(cog):
    cog.bot.clone_id = 4
    t = run(cog._xp_facts("my level", 10, FakeGuild()))
    assert "GLOBAL" not in t and "dashboard" not in t and cog.calls["global"] == []
    assert "ask this in a server" in run(cog._xp_facts("my level", 10, None))


def test_unrelated_message_has_no_facts_and_no_button(cog):
    assert run(cog._xp_facts("tell me a joke", 10, FakeGuild())) is None
    assert cog._is_xp_question("tell me a joke") is False
    assert cog._is_xp_question("what's my rank?") is True


def test_button_is_link_to_rank_page(cog, monkeypatch):
    monkeypatch.setattr(config, "DASH_PAGES_URL", "https://dash.example")
    b = ai_tools.dashboard_button(cog.bot)
    assert b.style == discord.ButtonStyle.link and b.label == "Open dashboard"
    assert b.url == "https://dash.example/#/me/rank"


def test_button_hidden_on_clone_or_without_url(cog, monkeypatch):
    monkeypatch.setattr(config, "DASH_PAGES_URL", "https://dash.example")
    cog.bot.clone_id = 9
    assert ai_tools.dashboard_button(cog.bot) is None
    cog.bot.clone_id = None
    monkeypatch.setattr(config, "DASH_PAGES_URL", "")
    assert ai_tools.dashboard_button(cog.bot) is None


def test_prompt_rule_keeps_numbers_to_fact_block():
    assert "12b." in af.BOT_RULES and "never invent a level" in af.BOT_RULES
