import pytest
from modules import ai_features as af, ai_web


@pytest.mark.parametrize("msg", [
    "what's the latest news on the iphone", "search who won the game", "weather in lagos today",
    "bitcoin price right now", "google the release date of gta 6",
])
def test_fresh_info_questions_get_search(msg):
    assert af.wants_web_search(msg)


@pytest.mark.parametrize("msg", [
    "tell me a joke", "explain recursion", "write me a poem about cats", "how do I level up", "",
])
def test_plain_chat_does_not_search(msg):
    assert not af.wants_web_search(msg)


def test_long_messages_never_trigger_search():
    assert not af.wants_web_search("latest " + "x" * 1100)


def test_replies_fit_in_one_discord_message():
    out = af.trim_reply("This is a sentence. " * 300)
    assert len(out) <= 1900


def test_link_reader_limits_raised_but_bounded():
    assert ai_web.MAX_URLS == 3 and ai_web.MAX_CHARS_PER_PAGE == 6000


def test_prompt_allows_full_answers_and_search():
    assert "under about 900 characters" in af.BOT_RULES and "complete answer of up to about 1800" in af.BOT_RULES
    assert "web search tool" in af.BOT_RULES and "[FILE CONTENT]" in af.BOT_RULES
