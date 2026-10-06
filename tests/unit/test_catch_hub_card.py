"""Hub menu card: rendering, fallback, and how /catch and the category select use it."""

import asyncio
import io
from types import SimpleNamespace

import pytest
from PIL import Image

import discord_bot.cogs.catch as catch
from modules import catch_hub_card as hc
from modules.catch_card import W
from tests.unit.test_catch_interaction_timing import Recorder, assert_response_first, make_interaction, slow

PNG = b"\x89PNG\r\n\x1a\n"


def png(key="play", cats=None):
    return asyncio.run(hc.hub_card_png(cats or catch.CATEGORIES, key))


def edits_to(inter):
    edits = []

    async def edit(*args, **kwargs):
        edits.append(kwargs)

    inter.edit_original_response = edit
    return edits


def sends_to(inter):
    sent = []

    async def send(*args, **kwargs):
        sent.append(dict(kwargs, _args=args))
        return SimpleNamespace(id=99)

    inter.followup.send = send
    return sent


def test_card_is_a_real_png_of_the_documented_size():
    data = png()
    assert data.startswith(PNG) and Image.open(io.BytesIO(data)).size == (W, hc.HUB_H)


def test_every_category_draws_and_each_card_differs():
    cards = {c.key: png(c.key) for c in catch.CATEGORIES}
    assert len(set(cards.values())) == len(catch.CATEGORIES)


def test_unknown_selected_key_falls_back_to_the_first_category():
    assert png("nope") == png(catch.CATEGORIES[0].key)


def test_unknown_category_keys_and_long_text_do_not_crash():
    odd = (SimpleNamespace(key="zzz", label="L" * 60, description="D" * 200,
                           actions=(("A" * 50, "B" * 200), ("x", "y"), ("p", "q"))),)
    assert png("zzz", odd).startswith(PNG)
    assert hc._element("zzz") == "stone"


def test_actions_appear_in_the_picture():
    base = png("play")
    changed = tuple(
        SimpleNamespace(key=c.key, label=c.label, description=c.description,
                        actions=(("Renamed", "Something else"),) + tuple(c.actions[1:]) if c.key == "play" else c.actions)
        for c in catch.CATEGORIES
    )
    assert png("play", changed) != base


def test_no_categories_raises_so_callers_fall_back():
    with pytest.raises(ValueError):
        asyncio.run(hc.hub_card_png((), "play"))


def test_cached_and_cache_clears():
    hc.clear_hub_cache()
    png("info")
    hits = hc._cached.cache_info().hits
    png("info")
    assert hc._cached.cache_info().hits == hits + 1
    hc.clear_hub_cache()
    assert hc._cached.cache_info().currsize == 0


def test_hub_file_is_fresh_each_time():
    a, b = hc.hub_file(b"x"), hc.hub_file(b"x")
    assert a is not b and a.filename == "hub.png"


# ---------------------------------------------------------------- parts

def test_parts_set_the_image_and_drop_only_the_plain_fields():
    plain = catch.build_hub_embed("economy")
    embed, file = asyncio.run(catch.hub_parts("economy"))
    assert file.filename == "hub.png" and embed.image.url == "attachment://hub.png"
    assert len(plain.fields) == 2 and embed.fields == []
    assert embed.title == plain.title and embed.description == plain.description
    assert embed.footer.text == plain.footer.text


def test_parts_fall_back_to_the_full_plain_embed(monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("no pillow today")

    monkeypatch.setattr(catch, "hub_card_png", boom)
    embed, file = asyncio.run(catch.hub_parts("collect"))
    assert file is None and embed.image.url is None
    assert embed.to_dict() == catch.build_hub_embed("collect").to_dict()


def test_parts_pass_the_normalised_category(monkeypatch):
    seen = []

    async def spy(cats, key):
        seen.append(key)
        return b"\x89PNG"

    monkeypatch.setattr(catch, "hub_card_png", spy)
    asyncio.run(catch.hub_parts("nope"))
    asyncio.run(catch.hub_parts("battle"))
    assert seen == ["play", "battle"]


# ---------------------------------------------------------------- screens

def test_catch_command_sends_the_card_view_with_a_file(monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(catch, "check_player_allowed", slow(rec, "gate", SimpleNamespace(allowed=True, reason=None)))
    inter = make_interaction(rec)
    sent = sends_to(inter)
    asyncio.run(catch.CatchCog.catch.callback(SimpleNamespace(), inter))
    assert isinstance(sent[0]["view"], catch.CatchHubCardView) and sent[0]["ephemeral"] is True
    assert sent[0]["file"].filename == "hub.png" and sent[0]["embed"].image.url == "attachment://hub.png"


def test_catch_command_sends_no_file_key_when_drawing_fails(monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("x")

    rec = Recorder()
    monkeypatch.setattr(catch, "hub_card_png", boom)
    monkeypatch.setattr(catch, "check_player_allowed", slow(rec, "gate", SimpleNamespace(allowed=True, reason=None)))
    inter = make_interaction(rec)
    sent = sends_to(inter)
    asyncio.run(catch.CatchCog.catch.callback(SimpleNamespace(), inter))
    assert "file" not in sent[0] and sent[0]["embed"].image.url is None and len(sent[0]["embed"].fields) == 2


def test_category_select_defers_first_then_redraws_the_card():
    rec = Recorder()
    inter = make_interaction(rec)
    inter.data = {"values": ["collect"]}
    edits = edits_to(inter)
    view = catch.CatchHubCardView()
    asyncio.run(view._select_category(inter))
    assert_response_first(rec)
    assert view.category.key == "collect" and edits[0]["view"] is view
    assert [f.filename for f in edits[0]["attachments"]] == ["hub.png"]
    assert edits[0]["embed"].image.url == "attachment://hub.png"
    default = [o.value for o in view.children[0].options if o.default]
    assert default == ["collect"]


def test_home_returns_to_play_and_redraws():
    rec = Recorder()
    inter = make_interaction(rec)
    edits = edits_to(inter)
    view = catch.CatchHubCardView(category="info")
    asyncio.run(view._home(inter))
    assert_response_first(rec)
    assert view.category.key == "play" and [f.filename for f in edits[0]["attachments"]] == ["hub.png"]


def test_redraw_clears_the_old_card_when_drawing_fails(monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("x")

    monkeypatch.setattr(catch, "hub_card_png", boom)
    inter = make_interaction(Recorder())
    inter.data = {"values": ["social"]}
    edits = edits_to(inter)
    asyncio.run(catch.CatchHubCardView()._select_category(inter))
    assert edits[0]["attachments"] == [] and edits[0]["embed"].image.url is None and len(edits[0]["embed"].fields) == 2


def test_failed_edit_tells_the_player(monkeypatch):
    inter = make_interaction(Recorder())
    inter.data = {"values": ["social"]}
    sent = sends_to(inter)

    async def bad_edit(*a, **k):
        raise RuntimeError("discord said no")

    inter.edit_original_response = bad_edit
    asyncio.run(catch.CatchHubCardView()._select_category(inter))
    assert len(sent) == 1 and sent[0]["ephemeral"] is True
    assert sent[0]["_args"] == (catch.text("hub.error"),)


def test_card_view_keeps_the_same_buttons_as_the_plain_hub():
    plain, card = catch.CatchHubView(category="economy"), catch.CatchHubCardView(category="economy")
    assert [(type(c), getattr(c, "label", None), c.custom_id) for c in plain.children] == \
        [(type(c), getattr(c, "label", None), c.custom_id) for c in card.children]
    assert catch.component_count(card) == 8


def test_restart_safe_select_clears_a_lingering_card():
    edits = []

    async def edit_message(**kwargs):
        edits.append(kwargs)

    inter = SimpleNamespace(data={"values": ["collect"]}, response=SimpleNamespace(edit_message=edit_message))
    asyncio.run(catch.CatchHubDynamicSelect().callback(inter))
    assert edits[0]["attachments"] == [] and edits[0]["view"].category.key == "collect"


def test_selected_tab_is_lit(monkeypatch):
    """Isolate the highlight: fixed colours and dots, so only the tab row can differ between selections."""
    import random as real

    monkeypatch.setattr(hc.catch_theme, "element_color", lambda e: __import__("discord").Colour(0x336699))
    monkeypatch.setattr(hc, "random", SimpleNamespace(Random=lambda seed: real.Random(0)))

    def row(key):
        img = Image.open(io.BytesIO(png(key)))
        return img.crop((0, 90, W, 182)).tobytes()

    hc.clear_hub_cache()
    assert row("play") == row("play")
    assert row("play") != row("collect") != row("info")
    hc.clear_hub_cache()
