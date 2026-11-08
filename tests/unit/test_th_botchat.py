"""Bot-to-bot chat test: hard-capped, only harness clones answer each other in the test server, a bot turn never gets tools,
and the report is honest."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

import config
from discord_bot.cogs import ai_tools
from discord_bot.cogs import test_harness as cogmod
from discord_bot.cogs.test_harness import BotPickView, HarnessPanel, TestHarnessCog
from modules import th_botchat as bc

GUILD, OWNER = 111, 42
ME, PEER, OTHER = 100000000000000001, 100000000000000002, 100000000000000003      # real ids are 17-19 digits


def run(c):
    return asyncio.run(c)


# ── pure ─────────────────────────────────────────────────────────────────
def test_chain_depth_counts_consecutive_bot_messages_from_the_newest():
    assert bc.chain_depth([True, True, False, True]) == 2
    assert bc.chain_depth([False]) == 0 and bc.chain_depth([]) == 0 and bc.chain_depth([True] * 9) == 9


def test_cap_stops_the_chain_exactly_at_the_limit():
    assert [bc.may_answer(d) for d in range(1, 6)] == [True, True, True, False, False]
    assert bc.BOT_CHAIN_MAX == 4


@pytest.mark.parametrize("kw,expect", [
    (dict(), True),
    (dict(in_test_guild=False), False), (dict(this_bot_is_harness=False), False),
    (dict(author_id=ME), False), (dict(author_id=OTHER), False),
])
def test_peer_ok(kw, expect):
    base = dict(in_test_guild=True, this_bot_is_harness=True, author_id=PEER, my_id=ME, peer_ids={PEER})
    base.update(kw)
    assert bc.peer_ok(**base) is expect


@pytest.mark.parametrize("t,err", [("", True), (None, True), ("AI service error. Try again later.", True), ("hello there!", False)])
def test_looks_like_error(t, err):
    assert bc.looks_like_error(t) is err


def replies(*items):
    return [{"who": w, "secs": s, "text": t} for w, s, t in items]


def test_verdict_full_chain_passes():
    r = bc.verdict(expect_chain=True, replies=replies(("them", 2.0, "hi"), ("me", 5.0, "yo"), ("them", 8.0, "ok")), total_seen=4)
    assert r["pass"] and not r["gaps"]


@pytest.mark.parametrize("rep,total", [
    ([], 1),                                                                          # no answer at all
    (replies(("them", 2, "AI service error. Try again later.")), 2),                  # answered with an error
    (replies(("them", 2, "hi")), 2),                                                  # chain died early
    (replies(("them", 2, "hi"), ("me", 3, "a"), ("them", 4, "b")), 5),                # ran past the cap
])
def test_verdict_failures(rep, total):
    assert bc.verdict(expect_chain=True, replies=rep, total_seen=total)["pass"] is False


def test_non_peer_bot_only_needs_one_real_answer():
    assert bc.verdict(expect_chain=False, replies=replies(("them", 3, "hello")), total_seen=2)["pass"]
    assert not bc.verdict(expect_chain=False, replies=[], total_seen=1)["pass"]
    assert "not one of my test clones" in bc.format_report("X", bc.verdict(expect_chain=False, replies=[], total_seen=1), False)


# ── ai_tools: who may answer a bot, and what a bot turn can never do ────────────────────────
@pytest.fixture(autouse=True)
def cfg(monkeypatch):
    monkeypatch.setattr(config, "TEST_GUILD_ID", GUILD, raising=False)
    monkeypatch.setattr(config, "DISCORD_CLONE_ADMIN_IDS", {OWNER})
    monkeypatch.setattr(config, "TEST_CLONE_IDS", {11, 12})


_REG = {}


def msg(author_id, is_bot=True, parent=None, guild_id=GUILD):
    """A fake message; its reply parent is found through channel.fetch_message, like a real uncached reference."""
    ch = SimpleNamespace(fetch_message=None)

    async def fetch(mid):
        return _REG[mid]
    ch.fetch_message = fetch
    mid = len(_REG) + 1
    ref = SimpleNamespace(message_id=parent.id, resolved=None) if parent is not None else None
    m = SimpleNamespace(id=mid, author=SimpleNamespace(id=author_id, bot=is_bot), guild=SimpleNamespace(id=guild_id),
                        reference=ref, channel=ch)
    _REG[mid] = m
    return m


def chained(n_bots):
    """Newest message with n_bots consecutive bot-authored messages in its reply chain (itself included)."""
    cur = None
    for i in range(n_bots):
        cur = msg(PEER if i % 2 == 0 else ME, parent=cur)
    return cur


def make_me(clone_id=11, peers=(PEER,)):
    me = SimpleNamespace(bot=SimpleNamespace(clone_id=clone_id, user=SimpleNamespace(id=ME)))
    me._peer_ids = AsyncMock(return_value=set(peers))
    return me


def ok(me, message):
    return run(ai_tools.AIToolsCog._peer_chat_ok(me, message))


def test_harness_clone_answers_a_known_peer_in_the_test_server():
    assert ok(make_me(), msg(PEER)) is True


@pytest.mark.parametrize("me_kw,m_kw", [
    (dict(clone_id=13), {}),            # not a harness clone
    (dict(clone_id=None), {}),          # the main bot is not a harness bot while clones are listed
    ({}, dict(guild_id=222)),           # not the test server
    ({}, dict(author_id=OTHER)),        # not one of our peers
    ({}, dict(author_id=ME)),           # never itself
])
def test_everyone_else_stays_silent(me_kw, m_kw):
    author = m_kw.get("author_id", PEER)
    assert ok(make_me(**me_kw), msg(author, guild_id=m_kw.get("guild_id", GUILD))) is False


def test_dm_is_never_answered():
    m = msg(PEER)
    m.guild = None
    assert ok(make_me(), m) is False


def test_depth_cap_applies_through_the_reply_chain():
    assert ok(make_me(), chained(3)) is True            # 3 bot messages so far: the 4th (this answer) is allowed
    assert ok(make_me(), chained(4)) is False           # 4 already: stop
    assert ok(make_me(), chained(9)) is False


def test_a_human_in_the_chain_resets_the_depth():
    old = chained(3)
    human = msg(OWNER, is_bot=False, parent=old)
    assert ok(make_me(), msg(PEER, parent=human)) is True        # depth is 1 again, whatever came before the human


def test_unreadable_chain_means_no_answer_never_a_crash():
    m = msg(PEER, parent=msg(PEER))
    m.channel.fetch_message = AsyncMock(side_effect=RuntimeError("boom"))
    assert ok(make_me(), m) is False
    broken = make_me()
    broken._peer_ids = AsyncMock(side_effect=RuntimeError("db down"))
    assert ok(broken, msg(PEER)) is False


def test_a_bot_turn_never_builds_command_tools_or_runs_quick_writes(monkeypatch):
    me = MagicMock()
    me._build_command_tools = AsyncMock(side_effect=AssertionError("tools must never be built for a bot"))
    me._prefs_quick_answer = AsyncMock(side_effect=AssertionError("no preference writes for a bot"))
    me._memory_quick_answer = AsyncMock(side_effect=AssertionError("no memory writes for a bot"))
    me._quick_answer = MagicMock(side_effect=AssertionError("no quick answers for a bot"))
    me._is_premium = AsyncMock(return_value=False)
    me._command_context = AsyncMock(return_value="")
    me._with_choice_tool = MagicMock(return_value=([], ""))
    me._voice_setting = AsyncMock(return_value=None)
    seen = {}

    async def fake_ai_chat(*a, **k):
        seen.update(k)
        return "hello back"
    monkeypatch.setattr(ai_tools, "ai_chat", fake_ai_chat)
    monkeypatch.setattr(ai_tools, "check_reply_limit", AsyncMock(return_value=(True, "")))
    monkeypatch.setattr(ai_tools, "_message_attachments", AsyncMock(return_value=[]))
    monkeypatch.setattr(ai_tools, "ProxyInteraction", lambda *a, **k: object())
    m = SimpleNamespace(author=SimpleNamespace(id=PEER, bot=True), guild=SimpleNamespace(id=GUILD), attachments=[],
                        channel=SimpleNamespace(permissions_for=lambda u: None))
    text, view = run(ai_tools.AIToolsCog._run_reply_turn(me, m, "how are you today", [], peer_turn=True))
    assert text == "hello back" and not seen.get("tools")


# ── harness cog ──────────────────────────────────────────────────────────
class Chan:
    def __init__(self):
        self.id, self.sent = 9, []

    async def send(self, text, **kw):
        self.sent.append(text)
        return SimpleNamespace(id=100, channel=self)


def member(uid, bot=True, name="Other"):
    return SimpleNamespace(id=uid, bot=bot, display_name=name)


def inter(user_id=OWNER, guild_id=GUILD, clone_id=11):
    i = SimpleNamespace(guild_id=guild_id, guild=SimpleNamespace(id=guild_id, get_member=lambda x: member(x)),
                        user=SimpleNamespace(id=user_id), client=SimpleNamespace(clone_id=clone_id), channel=Chan())
    i.response = SimpleNamespace(is_done=lambda: False, send_message=AsyncMock(), defer=AsyncMock())
    i.followup = SimpleNamespace(send=AsyncMock())
    return i


def make_cog(script, peers=(PEER,)):
    """script: list of (author_id, text) the bots 'say' after the opener, or the string 'timeout'."""
    bot = MagicMock(clone_id=11)
    bot.user = SimpleNamespace(id=ME)
    it = iter(script)

    async def wait_for(event, check=None, timeout=None):
        try:
            step = next(it)
        except StopIteration:
            raise asyncio.TimeoutError
        if step == "timeout":
            raise asyncio.TimeoutError
        return SimpleNamespace(channel=SimpleNamespace(id=9), id=101, author=SimpleNamespace(id=step[0]), content=step[1])
    bot.wait_for = wait_for
    c = TestHarnessCog(bot)
    return c


@pytest.fixture(autouse=True)
def stubs(monkeypatch):
    monkeypatch.setattr(cogmod.modx, "log_action", AsyncMock())
    monkeypatch.setattr(cogmod.db, "get_harness_peer_ids", AsyncMock(return_value=[PEER]), raising=False)


def test_full_chain_with_a_test_clone_passes_and_posts_a_report():
    c, i = make_cog([(PEER, "hi!"), (ME, "yo"), (PEER, "ok"), "timeout"]), inter()
    run(c._bot_chat(i, member(PEER)))
    assert i.channel.sent[0].startswith(f"<@{PEER}> ")                    # the mention opener
    assert "PASS" in i.channel.sent[-1]


def test_chain_that_runs_past_the_cap_fails():
    c, i = make_cog([(PEER, "a"), (ME, "b"), (PEER, "c"), (ME, "d extra")]), inter()
    run(c._bot_chat(i, member(PEER)))
    assert "FAIL" in i.channel.sent[-1]


def test_silent_bot_fails():
    c, i = make_cog(["timeout"]), inter()
    run(c._bot_chat(i, member(PEER)))
    assert "FAIL" in i.channel.sent[-1]


def test_third_party_bot_passes_with_one_real_answer():
    c, i = make_cog([(OTHER, "hello human"), "timeout"]), inter()
    run(c._bot_chat(i, member(OTHER)))
    assert "PASS" in i.channel.sent[-1] and "not one of my test clones" in i.channel.sent[-1]


@pytest.mark.parametrize("target", [member(OWNER, bot=False), member(ME), None])
def test_people_and_itself_are_refused(target):
    c, i = make_cog([]), inter()
    run(c._bot_chat(i, target))
    assert i.channel.sent == []


@pytest.mark.parametrize("kw", [dict(guild_id=222), dict(user_id=99), dict(clone_id=13), dict(clone_id=None)])
def test_gates_apply_to_bot_chat(kw):
    c, i = make_cog([]), inter(**kw)
    run(c._bot_chat(i, member(PEER)))
    assert i.channel.sent == []


def test_only_one_chat_at_a_time():
    c, i = make_cog([]), inter()
    c._chat_busy = {GUILD}
    run(c._bot_chat(i, member(PEER)))
    assert i.channel.sent == []


def test_no_bot_named_opens_the_one_tap_picker():
    c, i = make_cog([]), inter()
    run(c.harness_chat.callback(c, i, None))
    assert isinstance(i.response.send_message.await_args.kwargs["view"], BotPickView)
    assert i.channel.sent == []


def test_a_mention_string_is_parsed_to_an_id():
    c, i = make_cog([(PEER, "hi"), (ME, "yo"), (PEER, "ok"), "timeout"]), inter()
    run(c.harness_chat.callback(c, i, f"<@{PEER}>"))
    assert i.channel.sent[0].startswith(f"<@{PEER}> ")


def test_picker_is_owner_only():
    v = BotPickView(make_cog([]), OWNER)
    assert run(v.interaction_check(inter(user_id=99))) is False and run(v.interaction_check(inter())) is True


def test_panel_has_the_bot_chat_button():
    v = HarnessPanel(make_cog([]), OWNER)
    assert "Bot chat" in [b.label for b in v.walk_children() if isinstance(b, discord.ui.Button)]
