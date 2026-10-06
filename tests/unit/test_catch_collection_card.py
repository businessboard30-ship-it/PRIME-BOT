"""Drawn collection box: rendering, nickname safety, fallbacks and the new list view."""

import asyncio
import io
from types import SimpleNamespace

import discord
from PIL import Image

import discord_bot.cogs._views_catch_collection as views
from modules import catch_collection_card as cards
from modules.catch_i18n import text
from tests.unit.test_catch_interaction_timing import Recorder, assert_response_first, make_interaction, slow
from tests.unit.test_catch_phase3 import edits_to, row

PNG = b"\x89PNG\r\n\x1a\n"


def run(coro):
    return asyncio.run(coro)


def size(data: bytes):
    return Image.open(io.BytesIO(data)).size


def rows_of(n, **over):
    return [row(100 + i, species_id=1 + (i % 6), **over) for i in range(n)]


def box(rows, page=0, pages=3, total=None):
    return cards._cached_box(cards.tiles_for(rows), page, pages, len(rows) if total is None else total)


# ---------------------------------------------------------------- rendering

def test_card_height_follows_the_number_of_rows_and_stays_a_png():
    assert cards._height(5) < cards._height(6) == cards._height(10)  # one row is shorter than two
    assert size(box(rows_of(5)))[1] == cards._height(5) and size(box(rows_of(10)))[1] == cards._height(10)
    for n in (1, 5, 6, 10):
        data = box(rows_of(n))
        assert data.startswith(PNG) and size(data) == (cards.W, cards._height(n))


def test_every_flag_level_and_long_value_renders():
    loud = rows_of(10, shiny=True, special=True, favorite=True, locked=True, level=100)
    assert box(loud).startswith(PNG)
    long_name = [row(10**9, species_id=1, name="Extraordinarily Long Species Name", level=100)]
    assert size(box(long_name)) == (cards.W, cards._height(1))
    assert box(rows_of(3), page=99, pages=1, total=0).startswith(PNG)  # nonsense paging never errors


def test_card_changes_with_each_thing_it_shows():
    base = box(rows_of(4))
    for over in (dict(favorite=True), dict(locked=True), dict(shiny=True), dict(special=True), dict(level=77)):
        assert box(rows_of(4, **over)) != base
    assert box([row(999, species_id=1)] + rows_of(3)) != base          # owned id is drawn
    assert box(rows_of(4), page=1) != base and box(rows_of(4), total=9) != base


def test_element_comes_from_the_species_table_with_a_neutral_fallback():
    assert cards.tiles_for([row(1, species_id=6)])[0][3] == "tide"
    assert cards.tiles_for([row(1, species_id=424242)])[0][3] == "stone"


def test_nicknames_never_reach_the_card():
    plain = rows_of(3)
    nicked = [row(100 + i, species_id=1 + (i % 6), nickname="\u202e\U0001f525<@everyone>") for i in range(3)]
    assert cards.tiles_for(plain) == cards.tiles_for(nicked)
    assert "everyone" not in repr(cards.tiles_for(nicked)) and box(plain) == box(nicked)


# ---------------------------------------------------------------- helpers

def test_collection_art_attaches_the_card_or_leaves_the_embed_alone(monkeypatch):
    embed = discord.Embed(title="t")
    file = run(cards.collection_art(embed, rows_of(3), 0, 3))
    assert file.filename == cards.FILE and embed.image.url == f"attachment://{cards.FILE}"
    empty = discord.Embed(title="t")
    assert run(cards.collection_art(empty, [], 0, 0)) is None and empty.image.url is None

    def boom(*_a):
        raise RuntimeError("no pillow")

    monkeypatch.setattr(cards, "_cached_box", boom)
    failed = discord.Embed(title="t")
    assert run(cards.collection_art(failed, rows_of(3), 0, 3)) is None and failed.image.url is None


def test_kwargs_helpers():
    file = discord.File(io.BytesIO(b"x"), filename="a.png")
    assert cards.edit_kwargs(file) == {"attachments": [file]} and cards.edit_kwargs(None) == {"attachments": []}
    assert cards.send_kwargs(file) == {"file": file} and cards.send_kwargs(None) == {}


# ---------------------------------------------------------------- views

def fake_list(rows, total=None, page=None):
    async def list_owned(user_id, clone_id, **kw):
        return rows, len(rows) if total is None else total, kw.get("page", 0) if page is None else page
    return list_owned


def card_view(rows=None, total=25, **kw):
    rows = rows_of(10) if rows is None else rows
    return views.CollectionCardView(7, None, rows=rows, total=total, **kw)


def test_card_view_message_has_the_card_and_keeps_the_text_list():
    view = card_view()
    edit, send = run(view.message(edit=True)), run(view.message(edit=False))
    plain = views._collection_embed(view.rows, view.total, view.page, view.sort, view.flt)
    assert len(edit["attachments"]) == 1 and send["file"].filename == cards.FILE
    assert edit["embed"].description == plain.description and edit["embed"].footer.text == plain.footer.text
    old = views.CollectionBoxView(7, None, rows=view.rows, total=25)
    assert [(type(c), c.row) for c in view.children] == [(type(c), c.row) for c in old.children]


def test_sort_page_and_favourite_defer_first_then_redraw_the_card(monkeypatch):
    for name, data in (("_next", None), ("_prev", None), ("_sort", {"values": ["level"]}), ("_favorite", {"values": ["101"]})):
        rec = Recorder()
        new_rows = rows_of(4, level=42)
        monkeypatch.setattr(views, "list_owned", slow(rec, "db", (new_rows, 4, 0)))
        monkeypatch.setattr(views, "set_favorite", slow(rec, "fav", 101))
        view = card_view(page=1)
        inter = make_interaction(rec)
        inter.data = data
        edits = edits_to(inter)
        run(getattr(view, name)(inter))
        assert_response_first(rec)
        assert len(edits) == 1 and edits[0]["view"] is view
        assert edits[0]["attachments"][0].fp.getvalue() == box(new_rows, page=view.page, pages=1, total=4), name


def test_back_to_list_redraws_the_card_and_clears_content(monkeypatch):
    rec = Recorder()
    rows = rows_of(7)
    monkeypatch.setattr(views, "list_owned", slow(rec, "db", (rows, 7, 0)))
    view = card_view(total=7)
    inter = make_interaction(rec)
    edits = edits_to(inter)
    run(view._back_to_list(inter))
    assert edits[0]["content"] is None and len(edits[0]["attachments"]) == 1
    assert edits[0]["attachments"][0].fp.getvalue() == box(rows, page=0, pages=1, total=7)


def test_back_to_list_with_a_database_error_edits_nothing(monkeypatch):
    async def boom(*_a, **_k):
        raise RuntimeError("db down")

    monkeypatch.setattr(views, "list_owned", boom)
    view = card_view()
    inter = make_interaction(Recorder())
    edits = edits_to(inter)
    sent = []

    async def send(*a, **kw):
        sent.append(a[0])

    inter.followup = SimpleNamespace(send=send)
    run(view._back_to_list(inter))
    assert edits == [] and sent == [text("collection.error")]


def test_an_empty_filter_result_is_the_plain_empty_screen_with_the_card_cleared(monkeypatch):
    monkeypatch.setattr(views, "list_owned", fake_list([], total=0))
    view = card_view()
    inter = make_interaction(Recorder())
    edits = edits_to(inter)
    run(view._back_to_list(inter))
    assert edits[0]["attachments"] == [] and edits[0]["embed"].image.url is None


def test_opening_the_filter_screen_responds_first_and_clears_the_card():
    # Changed with the filter card: the screen now defers first, then swaps the collection card for the
    # filter card (it used to edit_message with attachments=[]). The collection card must still not linger.
    rec = Recorder()
    view = card_view()
    inter = make_interaction(rec)
    edits = edits_to(inter)
    run(view._open_filters(inter))
    assert rec.calls == ["response"]
    assert isinstance(edits[0]["view"], views.CollectionFilterView)
    assert [a.filename for a in edits[0]["attachments"]] == ["filter.png"]
    assert cards.FILE not in [a.filename for a in edits[0]["attachments"]]


def test_open_collection_sends_the_card_view_after_gate_and_db(monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(views, "check_player_allowed", slow(rec, "gate", SimpleNamespace(allowed=True, reason=None)))
    monkeypatch.setattr(views, "list_owned", slow(rec, "db", (rows_of(5), 5, 0)))
    inter = make_interaction(rec)
    sent = []

    async def send(*a, **kw):
        sent.append(kw)

    inter.followup = SimpleNamespace(send=send)
    run(views.open_collection(inter))
    assert_response_first(rec)
    assert rec.calls.index("gate") < rec.calls.index("db")
    assert isinstance(sent[0]["view"], views.CollectionCardView) and sent[0]["file"].filename == cards.FILE


def test_card_failure_falls_back_to_the_plain_list(monkeypatch):
    def boom(*_a):
        raise RuntimeError("no pillow")

    monkeypatch.setattr(cards, "_cached_box", boom)
    msg = run(card_view().message(edit=True))
    assert msg["attachments"] == [] and msg["embed"].image.url is None and msg["embed"].description
