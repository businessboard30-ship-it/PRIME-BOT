import pytest
from modules import gif_replies as g


@pytest.mark.parametrize("msg,kind", [
    ("so can u be my girlfriend", "love_no"), ("marry me", "love_no"),
    ("are you real?", "real"), ("are u a bot", "real"), ("r u human",  "real"),
    ("you are so dumb", "insult"), ("u suck", "insult"), ("shut up", "insult"),
    ("send nudes", "no"), ("give me premium for free", "no"),
    ("do you agree?", "agree"), ("are we cool", "agree"),
])
def test_triggers(msg, kind):
    assert g.gif_kind(msg) == kind
    assert g.gif_for(msg) == g.GIFS[kind]


@pytest.mark.parametrize("msg", [
    "what is today's date", "thanks for the help", "how do I level up",
    "shut up and take my money", "is the shop real money", "that's not bad at all",
])
def test_normal_messages_do_not_trigger(msg):
    assert g.gif_kind(msg) is None


def test_view_link_is_not_embedded():
    pytest.importorskip("discord")
    from discord_bot.cogs.ai_tools import _gif_embed, _gif_is_link
    assert _gif_embed(g.GIFS["real"]) is None and _gif_is_link(g.GIFS["real"])
    e = _gif_embed(g.GIFS["agree"])
    assert e is not None and e.image.url == g.GIFS["agree"] and not _gif_is_link(g.GIFS["agree"])
    assert _gif_embed("hello") is None
