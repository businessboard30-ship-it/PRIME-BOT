"""Drawn creature card: rendering, caching, and how the creature screen uses it."""

import asyncio
import io
from types import SimpleNamespace

import pytest
from PIL import Image

import discord_bot.cogs._views_catch_collection as collection_views
import discord_bot.cogs._views_catch_creature as views
from modules import catch_card
from modules.catch_game import ELEMENTS, LEVEL_MAX, RARITIES
from tests.unit.test_catch_interaction_timing import Recorder, make_interaction, slow
from tests.unit.test_catch_phase3 import detail, edits_to, make_view, ready_preview, row

PNG = b"\x89PNG\r\n\x1a\n"


def png(d) -> bytes:
    return asyncio.run(catch_card.creature_card_png(d))


def allow(monkeypatch, rec):
    monkeypatch.setattr(views, "check_player_allowed", slow(rec, "gate", SimpleNamespace(allowed=True, reason=None)))


def test_card_is_a_real_png_of_the_documented_size():
    data = png(detail())
    assert data.startswith(PNG)
    assert Image.open(io.BytesIO(data)).size == (catch_card.W, catch_card.H)


def test_every_element_and_rarity_renders_with_and_without_a_second_type():
    elements = [e if isinstance(e, str) else e.key for e in ELEMENTS]
    rarities = [r.key if hasattr(r, "key") else r for r in (RARITIES.values() if hasattr(RARITIES, "values") else RARITIES)]
    assert len(elements) >= 9 and len(rarities) >= 5
    for el in elements:
        assert png(detail(element=el, element2=None)).startswith(PNG)
        assert png(detail(element=el, element2=elements[0])).startswith(PNG)
    for rar in rarities:
        assert png(detail(rarity=rar)).startswith(PNG)


@pytest.mark.parametrize("over", [
    dict(shiny=True), dict(special=True), dict(level=1, xp=0), dict(level=LEVEL_MAX, xp=0),
    dict(stats={}), dict(stats={n: 9999 for n in ("vigor", "power", "guard", "speed", "spirit")}),
    dict(xp=10**9), dict(name="A" * 80),
])
def test_edge_values_do_not_crash(over):
    assert png(detail(**over)).startswith(PNG)


def test_card_changes_with_what_it_shows_and_is_cached():
    base = png(detail())
    assert png(detail(level=17)) != base
    assert png(detail(shiny=True)) != base
    assert png(detail(rarity="epic")) != base
    assert png(detail(stats={"vigor": 1, "power": 1, "guard": 1, "speed": 1, "spirit": 1})) != base
    assert catch_card._cached(catch_card._key(detail())) is catch_card._cached(catch_card._key(detail()))


def test_nickname_is_not_drawn_so_odd_characters_cannot_break_the_card():
    assert png(detail(nickname="\u202e\U0001f525<>@x")) == png(detail())


def test_creature_message_attaches_the_card_and_drops_the_plain_stat_field():
    msg = asyncio.run(views.creature_message(detail(), ready_preview()))
    embed, files = msg["embed"], msg["attachments"]
    assert len(files) == 1 and files[0].filename == views.CARD_FILE
    assert embed.image.url == f"attachment://{views.CARD_FILE}"
    plain = views.creature_embed(detail(), ready_preview())
    assert len(embed.fields) == len(plain.fields) - 1
    assert [f.name for f in embed.fields] == [f.name for f in plain.fields[1:]]


def test_creature_message_falls_back_to_the_text_embed_when_drawing_fails(monkeypatch):
    async def boom(_d):
        raise RuntimeError("no pillow")

    monkeypatch.setattr(views, "creature_card_png", boom)
    msg = asyncio.run(views.creature_message(detail(), ready_preview()))
    plain = views.creature_embed(detail(), ready_preview())
    assert msg["attachments"] == [] and msg["embed"].image.url is None
    assert [f.name for f in msg["embed"].fields] == [f.name for f in plain.fields]


def test_open_creature_detail_shows_the_card(monkeypatch):
    rec = Recorder()
    allow(monkeypatch, rec)

    async def load(*_a, **_k):
        return detail()

    async def prev(*_a, **_k):
        return ready_preview()

    monkeypatch.setattr(views, "load_creature", load)
    monkeypatch.setattr(views, "preview", prev)
    inter = make_interaction(rec)
    edits = edits_to(inter)
    asyncio.run(views.open_creature_detail(inter, 7, None, 11, back=lambda i: None))
    assert len(edits) == 1 and len(edits[0]["attachments"]) == 1 and edits[0]["embed"].image.url


def test_refresh_after_an_action_redraws_the_card(monkeypatch):
    rec = Recorder()

    async def load(*_a, **_k):
        return detail(level=17)

    async def prev(*_a, **_k):
        return ready_preview()

    monkeypatch.setattr(views, "load_creature", load)
    monkeypatch.setattr(views, "preview", prev)
    view = make_view(rec)
    inter = make_interaction(rec)
    edits = edits_to(inter)
    asyncio.run(view._redraw(inter, "done"))
    assert edits[0]["content"] == "done" and len(edits[0]["attachments"]) == 1


def test_release_and_evolve_confirm_screens_clear_the_card(monkeypatch):
    rec = Recorder()
    allow(monkeypatch, rec)

    async def prev(*_a, **_k):
        return ready_preview()

    monkeypatch.setattr(views, "preview", prev)
    for call in ("_release", "_evolve"):
        view = make_view(rec)
        inter = make_interaction(rec)
        edits = edits_to(inter)
        asyncio.run(getattr(view, call)(inter))
        # Visual pass 8: the confirm screens now carry their OWN card; the creature card must not linger.
        names = [f.filename for f in edits[0]["attachments"]]
        assert len(edits) == 1 and names == [{"_release": "release.png", "_evolve": "evolve.png"}[call]]
        assert "creature.png" not in names


def test_back_to_the_list_clears_the_card(monkeypatch):
    async def fake_list(user_id, clone_id, **kw):
        return [row(5)], 1, 0

    monkeypatch.setattr(collection_views, "list_owned", fake_list)
    box = collection_views.CollectionBoxView(7, None, rows=[row(5)], total=1)
    inter = make_interaction(Recorder())
    edits = edits_to(inter)
    asyncio.run(box._back_to_list(inter))
    assert edits[0]["attachments"] == []
