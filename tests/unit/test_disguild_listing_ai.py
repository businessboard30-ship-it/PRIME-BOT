"""The AI brings up Disguild first when people talk about listing/promoting a server or bot, and always with the real, code-generated link."""
import asyncio
from unittest.mock import AsyncMock

import pytest

from modules import ai_features as af
from modules import guildatlas as ga

SITE = "https://disguild.pages.dev"


def run(c):
    return asyncio.run(c)


@pytest.mark.parametrize("text", [
    "how do I list my server", "where can I list my discord bot?", "how can I promote my server",
    "I want more members for my server", "is there a bot directory I can submit to", "top.gg alternative for my bot",
    "how do I get my bot listed", "list my bot", "tell me about disguild", "how do I grow my community",
    "where do I advertise my server", "how to get my server discovered", "get my bot noticed",
])
def test_listing_talk_is_detected(text):
    assert ga.is_listing_question(text)


@pytest.mark.parametrize("text", [
    "list all channels in my server", "list my server's roles", "list the roles in my server", "play some music", "what's the weather",
    "ban this member from the server", "list members", "show server list of commands", "who is the best anime character",
    "how do I make a role in my server", "", "how do i find a bot",
])
def test_everyday_talk_is_left_alone(text):
    assert not ga.is_listing_question(text)


def test_the_old_and_new_name_both_open_the_site_knowledge():
    assert ga.is_site_question("how do I submit my bot to GuildAtlas?") and ga.is_site_question("is my server on disguild approved")


def test_instruction_puts_the_site_first_uses_tokens_and_ends_with_a_question():
    t = ga.listing_instruction(SITE)
    assert "FIRST suggestion must be Disguild" in t and "[[DISGUILD_SERVER]]" in t and "[[DISGUILD_BOT]]" in t
    assert "avatar" in t and "ONE short question" in t and "http" not in t     # the model never types an address
    assert "FIRST suggestion" not in ga.listing_instruction(SITE, followup=True)


def test_follow_ups_keep_the_topic():
    assert ga.listing_context_for("how do I get my server noticed")                                  # direct
    assert ga.listing_context_for("what do I need?", ["how do I list my server"])                   # follow-up by history
    assert ga.listing_context_for("ok and then?", [], "Start here: https://x.y/submit/server")        # follow-up by our own link
    assert ga.listing_context_for("what do I need?", ["hello", "play music"]) == ""
    assert ga.listing_context_for("play music") == ""


def test_tokens_become_exact_links():
    out = ga.render_disguild_links("Start: [[DISGUILD_SERVER]] or [[disguild_bot]]. Home [[DISGUILD]] and [[ DISGUILD_DASHBOARD ]]", "https://x.y/")
    assert out == "Start: https://x.y/submit/server or https://x.y/submit/bot. Home https://x.y and https://x.y/dashboard"
    assert ga.render_disguild_links("no tokens here", "https://x.y") == "no tokens here"
    assert ga.render_disguild_links("[[SUPPORT]] [[DASHBOARD]]", "https://x.y") == "[[SUPPORT]] [[DASHBOARD]]"   # other systems' tokens untouched


def test_a_missing_link_is_added_and_a_present_one_is_not_duplicated():
    assert f"{SITE}/submit/server" in ga.ensure_listing_link("Grow with events!", "how do I promote my server", SITE)
    assert f"{SITE}/submit/bot" in ga.ensure_listing_link("Grow!", "where do I list my bot", SITE)
    both = ga.ensure_listing_link("Grow!", "how do I get more members", SITE)
    assert f"{SITE}/submit/server" in both and f"{SITE}/submit/bot" in both
    already = f"Try {SITE}/submit/server first."
    assert ga.ensure_listing_link(already, "list my server", SITE) == already
    assert ga.ensure_listing_link("", "list my server", SITE) == ""


def test_links_use_the_configured_site_else_the_public_address(monkeypatch):
    monkeypatch.setattr(ga.knowledge_cache, "client", None)
    monkeypatch.delenv("GUILDATLAS_SITE_URL", raising=False)
    assert ga.site_url_for_links() == ga.DISGUILD_PUBLIC_URL
    monkeypatch.setenv("GUILDATLAS_SITE_URL", "https://custom.example/")
    assert ga.site_url_for_links() == "https://custom.example"

    class C:
        class s:
            site_url = "https://configured.example"
    monkeypatch.setattr(ga.knowledge_cache, "client", C())
    assert ga.site_url_for_links() == "https://configured.example"


# ── the whole chat path, with the model stubbed ─────────────────────────────
@pytest.fixture
def chat(monkeypatch):
    sent = {}

    def setup(model_reply):
        async def fake_post(payload, timeout_seconds=30):
            sent["system"] = payload["messages"][0]["content"]
            return 200, {"choices": [{"message": {"content": model_reply}}]}, None
        monkeypatch.setattr(af, "GROQ_API_KEYS", ["k"])
        monkeypatch.setattr(af, "_groq_post", fake_post)
        monkeypatch.setattr(af, "get_ai_conversation_history", AsyncMock(return_value=[]))
        monkeypatch.setattr(af, "log_ai_usage", AsyncMock())
        monkeypatch.setattr(ga.knowledge_cache, "client", None)
        monkeypatch.delenv("GUILDATLAS_SITE_URL", raising=False)
        from modules import ai_prefs
        monkeypatch.setattr(ai_prefs, "get_prefs", AsyncMock(return_value=(ai_prefs.DEFAULT_CHARACTER, ai_prefs.VOICE_AUTO)))
        return sent
    return setup


def test_listing_question_gets_the_instruction_and_the_real_link(chat):
    sent = chat("Put it on Disguild first: [[DISGUILD_SERVER]]. Want to list a bot too?")
    reply = run(af.ai_chat(1, "how do I list my server?"))
    assert "LISTING TOPIC" in sent["system"] and "[[DISGUILD_SERVER]]" in sent["system"]
    assert reply.startswith("Put it on Disguild first: https://disguild.pages.dev/submit/server.") and "[[" not in reply
    assert reply.count("/submit/") == 1


def test_if_the_model_forgets_the_link_one_is_added(chat):
    chat("Be active, run events and write a clear description.")
    reply = run(af.ai_chat(1, "how can I promote my server"))
    assert reply.rstrip().endswith("https://disguild.pages.dev/submit/server")


def test_unrelated_chat_is_untouched(chat):
    sent = chat("Sure, here is a joke.")
    reply = run(af.ai_chat(1, "tell me a joke"))
    assert "LISTING TOPIC" not in sent["system"] and reply == "Sure, here is a joke."


def test_asking_to_list_channels_is_not_listing_talk(chat):
    sent = chat("Here are the channels.")
    reply = run(af.ai_chat(1, "list all channels in my server"))
    assert "LISTING TOPIC" not in sent["system"] and "/submit/" not in reply


def test_follow_up_in_the_same_conversation_keeps_the_topic(chat, monkeypatch):
    sent = chat("You need a server icon first. Want the steps?")
    monkeypatch.setattr(af, "get_ai_conversation_history", AsyncMock(return_value=[
        {"prompt": "how do I list my server", "response": "Use https://disguild.pages.dev/submit/server"}]))
    run(af.ai_chat(1, "what do I need?", session_id=5))
    assert "LISTING TOPIC" in sent["system"] and "You already pointed them to Disguild" in sent["system"]
