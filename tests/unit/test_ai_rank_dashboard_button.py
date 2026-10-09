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
    assert "GLOBAL" not in t and "global leaderboard" not in t and "dashboard" in t and cog.calls["global"] == []
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


def test_button_shown_on_clone_and_hidden_without_url(cog, monkeypatch):
    monkeypatch.setattr(config, "DASH_PAGES_URL", "https://dash.example")
    cog.bot.clone_id = 9
    b = ai_tools.dashboard_button(cog.bot)
    assert b is not None and b.url == "https://dash.example/#/me/rank"
    cog.bot.clone_id = None
    monkeypatch.setattr(config, "DASH_PAGES_URL", "")
    assert ai_tools.dashboard_button(cog.bot) is None


def test_prompt_rule_keeps_numbers_to_fact_block():
    assert "12b." in af.BOT_RULES and "never invent a level" in af.BOT_RULES


# ---- the button can never make the AI reply vanish ----
def test_url_without_a_scheme_is_not_attached(cog, monkeypatch):
    monkeypatch.setattr(config, "DASH_PAGES_URL", "prime-bot-dash.pages.dev")
    assert ai_tools.dashboard_button(cog.bot) is None
    monkeypatch.setattr(config, "DASH_PAGES_URL", " https://dash.example/ ")
    assert ai_tools.dashboard_button(cog.bot).url == "https://dash.example/#/me/rank"


def test_dashboard_path_for_xp_dashboard_and_other_questions(cog):
    assert cog._dashboard_path("what is my level") == "/me/rank"
    assert cog._dashboard_path("where is the dashboard?") == "/me"
    assert cog._dashboard_path("tell me a joke") is None
    assert cog._dashboard_path(None) is None


class _View:
    def __init__(self, *children):
        self.children = list(children)

    def remove_item(self, item):
        self.children.remove(item)


def _http_error():
    return discord.HTTPException(types.SimpleNamespace(status=400, reason="Bad Request"), "Invalid Form Body")


def test_a_rejected_dashboard_button_is_dropped_and_the_reply_still_goes_out():
    btn, view, sent = object(), None, []
    view = _View(btn)

    async def send(**kw):
        sent.append(dict(kw))
        if btn in kw["view"].children:
            raise _http_error()
        return "ok"
    assert run(ai_tools.send_with_fallbacks(send, view, btn, None)) == "ok"
    assert len(sent) == 2 and view.children == []


def test_voice_file_is_dropped_last_and_everything_failing_raises():
    btn = object()
    view = _View(btn)
    calls = []

    async def send(**kw):
        calls.append(("file" in kw, btn in view.children))
        if "file" in kw:
            raise _http_error()
        return "ok"
    assert run(ai_tools.send_with_fallbacks(send, view, btn, "VOICE")) == "ok"
    assert calls == [(True, True), (True, False), (False, False)]

    async def always_fail(**kw):
        raise _http_error()
    with pytest.raises(discord.HTTPException):
        run(ai_tools.send_with_fallbacks(always_fail, _View(), None, None))


def test_plain_reply_with_no_view_sends_once_without_a_view_argument():
    seen = []

    async def send(**kw):
        seen.append(kw)
        return "ok"
    assert run(ai_tools.send_with_fallbacks(send, None, None, None)) == "ok" and seen == [{}]
