import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from modules import scam_check as sc
from modules import scam_shield as ss


def run(c):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(c)


# ── trigger words and cooldown ────────────────────────────────────────────

@pytest.mark.parametrize("text", ["scam shield", "<@999> scam shield", "<@!999> Scam Shield please", "scamshield this",
                                  "scam check", "<@999> scam shield?", "SCAM  SHIELD!!"])
def test_trigger_phrases(text):
    assert sc.is_check_request(text)


@pytest.mark.parametrize("text", ["", "<@999>", "hello", "is this a scam", "what is scam shield and how does it work for me",
                                  "<@999> tell me about cats", "scam shield is great"])
def test_normal_chat_is_not_a_trigger(text):
    assert not sc.is_check_request(text)


def test_cooldown_per_member_and_per_server():
    cd = sc.Cooldown()
    assert cd.wait_seconds(1, 10, now=100.0) == 0
    assert 0 < cd.wait_seconds(1, 10, now=105.0) <= 15            # same member, too soon
    assert cd.wait_seconds(1, 11, now=105.0) == 0                  # another member is fine
    assert cd.wait_seconds(1, 10, now=125.0) == 0                  # after the cooldown
    cd = sc.Cooldown()
    for uid in range(sc.GUILD_PER_MINUTE):
        assert cd.wait_seconds(2, uid, now=50.0) == 0
    assert cd.wait_seconds(2, 99, now=51.0) > 0                    # the server is capped per minute
    assert cd.wait_seconds(3, 99, now=51.0) == 0                   # other servers unaffected


def test_answers_never_say_safe_and_never_name_the_rule():
    assert "does **not** mean it is safe" in sc.clean_text(False)
    assert "picture" in sc.clean_text(True)
    assert "fatowin" not in sc.match_text("domain")


# ── the cog ───────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def rules():
    ss._c.words, ss._c.domains, ss._c.images = [(1, "fatowin")], [(2, "fatowin.com")], []
    ss._c.enabled, ss._c.loaded_at = True, 10 ** 12


BOT = SimpleNamespace(id=999)


def member(uid, staff=False):
    m = MagicMock(spec=discord.Member)
    m.id, m.bot = uid, False
    m.guild_permissions = SimpleNamespace(administrator=False, manage_guild=False, manage_messages=staff)
    m.__str__ = lambda s: f"user{uid}"
    return m


def make(content_target="hello", target_author=None, req_text="<@999> scam shield", mention=True, reply=True, attachments=()):
    guild = SimpleNamespace(id=555, owner_id=1, get_channel=lambda cid: None, get_member=lambda uid: None, me=object(), text_channels=[])
    tgt = MagicMock(spec=discord.Message)
    tgt.guild, tgt.author, tgt.content, tgt.embeds, tgt.attachments = guild, target_author or member(42), content_target, [], list(attachments)
    tgt.id, tgt.jump_url, tgt.channel = 5, "https://discord.com/channels/555/7/5", SimpleNamespace(id=7, mention="#c")
    req = MagicMock()
    req.guild, req.author, req.content = guild, member(11), req_text
    req.mentions = [BOT] if mention else []
    req.reference = SimpleNamespace(message_id=5, resolved=tgt) if reply else None
    req.channel = SimpleNamespace(id=7)
    req.reply = AsyncMock()
    return req, tgt


@pytest.fixture()
def cog(monkeypatch):
    from discord_bot.cogs import scam_shield as mod
    c = mod.ScamShieldCog.__new__(mod.ScamShieldCog)
    c.bot = MagicMock(); c.bot.user = BOT
    monkeypatch.setattr(mod, "_check_cooldown", sc.Cooldown())
    monkeypatch.setattr(mod, "_reported", {})
    monkeypatch.setattr(mod.ss, "load", AsyncMock())
    monkeypatch.setattr(mod.ss, "guild_settings", AsyncMock(return_value={"enabled": True, "allowed_domains": ()}))
    monkeypatch.setattr(mod.sv, "available", lambda: False)
    c._can_post = lambda ch, g: True
    c.mod = mod
    return c


def answer(req):
    kw = req.reply.await_args.kwargs
    return kw["embed"].description, kw


def test_a_confident_scam_is_deleted_and_the_answer_says_so(cog):
    req, tgt = make("free $3500 at fatowin.com use GIFT")
    run(cog._check_request(req))
    text, kw = answer(req)
    tgt.delete.assert_awaited_once()
    assert "I deleted it" in text and kw["delete_after"] == sc.ANSWER_SECONDS
    assert kw.get("view") is None                                    # nothing left to report once it is gone
    assert "fatowin" not in text                                     # the matched rule is not revealed


def test_when_the_bot_cannot_delete_it_says_exactly_why_and_keeps_the_report_button(cog):
    req, tgt = make("free $3500 at fatowin.com use GIFT")
    tgt.channel.permissions_for = lambda member: SimpleNamespace(manage_messages=False)
    run(cog._check_request(req))
    text, kw = answer(req)
    tgt.delete.assert_not_called()
    assert "could not delete it" in text and "Manage Messages" in text and kw["view"] is not None
    assert "fatowin" not in text


def test_clean_message_is_not_called_safe(cog):
    req, _ = make("anyone up for a game tonight?")
    run(cog._check_request(req))
    text, kw = answer(req)
    assert "no known scam pattern" in text and "does **not** mean it is safe" in text and kw["view"] is None


def test_staff_messages_are_not_judged(cog):
    req, _ = make("fatowin.com free money", target_author=member(7, staff=True))
    run(cog._check_request(req))
    assert "server staff" in answer(req)[0] and "scam" not in answer(req)[0].split("staff")[0]


def test_reply_to_the_bots_own_message_is_left_to_ai_chat(cog):
    req, _ = make("hi", target_author=member(999))
    run(cog._check_request(req))
    req.reply.assert_not_awaited()


@pytest.mark.parametrize("kw", [{"mention": False}, {"reply": False}, {"req_text": "<@999> what do you think of cats"}])
def test_needs_reply_and_mention_and_the_words(cog, kw):
    req, _ = make("fatowin.com", **kw)
    run(cog._check_request(req))
    req.reply.assert_not_awaited()


def test_a_server_that_switched_scam_shield_off_is_respected(cog):
    cog.mod.ss.guild_settings.return_value = {"enabled": False, "allowed_domains": ()}
    req, _ = make("fatowin.com")
    run(cog._check_request(req))
    assert "switched off" in answer(req)[0]


def test_allowed_domains_of_the_server_are_respected(cog):
    ss._c.words = []                                             # only the domain rule applies here
    cog.mod.ss.guild_settings.return_value = {"enabled": True, "allowed_domains": ("fatowin.com",)}
    req, _ = make("see https://fatowin.com/page")
    run(cog._check_request(req))
    assert "no known scam pattern" in answer(req)[0]


def test_second_check_too_soon_is_asked_to_wait(cog):
    req, _ = make("hello"); run(cog._check_request(req))
    req2, _ = make("hello"); run(cog._check_request(req2))
    assert "try again" in answer(req2)[0]


def test_unreadable_reply_target(cog):
    req, _ = make("x")
    req.reference = SimpleNamespace(message_id=5, resolved=None)
    req.channel = SimpleNamespace(id=7, fetch_message=AsyncMock(side_effect=discord.NotFound(MagicMock(status=404), "gone")))
    run(cog._check_request(req))
    assert "couldn't find" in answer(req)[0]


# ── the report button ─────────────────────────────────────────────────────

def test_report_goes_to_the_modlog_once_and_only_the_asker_can_press(cog, monkeypatch):
    log = MagicMock(); log.send = AsyncMock()
    req, tgt = make("fatowin.com free")
    tgt.guild.get_channel = lambda cid: log
    monkeypatch.setattr(cog.mod.db, "get_automod_config", AsyncMock(return_value={"log_channel_id": 88}), raising=False)
    reporter = member(11)
    assert run(cog._report_to_staff(reporter, tgt, "domain", None)) is True
    log.send.assert_awaited_once()
    assert run(cog._report_to_staff(reporter, tgt, "domain", None)) is True       # same message again: no second flag
    log.send.assert_awaited_once()

    view = cog.mod._ReportView(cog, 11, tgt, "domain", None)
    other = SimpleNamespace(user=SimpleNamespace(id=12), response=SimpleNamespace(send_message=AsyncMock()))
    assert run(view.interaction_check(other)) is False
    mine = SimpleNamespace(user=SimpleNamespace(id=11), response=SimpleNamespace(send_message=AsyncMock()))
    assert run(view.interaction_check(mine)) is True


def test_ai_chat_knows_the_reply_and_mention_method():
    from modules import ai_features as af
    assert "scam shield" in af.BOT_RULES and "reply" in af.BOT_RULES and "never deletes" in af.BOT_RULES


# ── AI chat must never answer a scam-check request ────────────────────────

def test_ai_chat_stays_out_even_when_the_replied_message_cannot_be_loaded(monkeypatch):
    from discord_bot.cogs import ai_tools
    chat = ai_tools.AIToolsCog.__new__(ai_tools.AIToolsCog) if hasattr(ai_tools, "AIToolsCog") else None
    if chat is None:
        cls = next(v for v in vars(ai_tools).values() if isinstance(v, type) and hasattr(v, "on_bot_chat") and v.__module__ == ai_tools.__name__)
        chat = cls.__new__(cls)
    chat.bot = MagicMock(); chat.bot.user = BOT
    chat._run_reply_turn = AsyncMock(return_value=("hi", None))
    chat._roast_active_in = AsyncMock(return_value=False)
    req, _ = make("x")
    req.reference = SimpleNamespace(message_id=5, resolved=None)          # not cached
    req.channel = SimpleNamespace(id=7, fetch_message=AsyncMock(side_effect=discord.NotFound(MagicMock(status=404), "gone")),
                                  typing=MagicMock())
    req.channel.fetch_message = AsyncMock(side_effect=discord.NotFound(MagicMock(status=404), "gone"))
    req.author.bot = False
    run(chat.on_bot_chat(req))
    chat._run_reply_turn.assert_not_awaited()                              # no AI answer
