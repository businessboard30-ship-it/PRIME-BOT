"""Filter screen card: rendering, search-term privacy, fallback, and how the card screen redraws."""

import asyncio
import io
from types import SimpleNamespace

import discord
import pytest
from PIL import Image

import discord_bot.cogs._views_catch_collection as views
from modules import catch_filter_card as fc
from modules.catch_card import W
from modules.catch_collection import CollectionFilter as F
from modules.catch_game import ELEMENTS, RARITIES
from modules.catch_i18n import text
from tests.unit.test_catch_collection_card import rows_of
from tests.unit.test_catch_interaction_timing import Recorder, make_interaction

PNG = b"\x89PNG\r\n\x1a\n"
REAL_RENDER = fc.filter_card_png  # kept so spies can call through


def run(coro):
    return asyncio.run(coro)


def png(flt=None):
    return run(fc.filter_card_png(flt or F()))


def img(flt=None):
    return Image.open(io.BytesIO(png(flt))).convert("RGB")


def crop(flt, box):
    return img(flt).crop(box).tobytes()


# Regions of the 720x290 card (see _draw_filter): rarity row, element row, and the three toggle chips.
RARITY_ROW = (24, 80, 696, 116)
ELEMENT_ROW = (24, 146, 696, 210)
CHIPS = {"shiny": (24, 226, 242, 268), "favorite": (251, 226, 469, 268), "search": (479, 226, 697, 268)}


@pytest.fixture(autouse=True)
def fresh_cache():
    fc._cached.cache_clear()
    yield
    fc._cached.cache_clear()


@pytest.fixture
def flat_colours(monkeypatch):
    """Same backdrop for every element, so only the part a guard controls can differ."""
    monkeypatch.setattr(fc.catch_theme, "element_color", lambda e: discord.Colour(0x3366CC))


# ---------------------------------------------------------------- rendering

def test_card_is_a_real_png_of_the_documented_size():
    data = png()
    assert data.startswith(PNG) and Image.open(io.BytesIO(data)).size == (W, fc.FILTER_H)


def test_every_rarity_element_and_toggle_changes_the_card():
    base = png()
    seen = {base}
    for r in RARITIES:
        seen.add(png(F(r.key)))
    for e in ELEMENTS:
        seen.add(png(F(None, e)))
    seen.update({png(F(None, None, True)), png(F(None, None, False, True)), png(F(None, None, False, False, "x"))})
    assert len(seen) == 1 + len(RARITIES) + len(ELEMENTS) + 3


def test_bad_values_are_cleaned_before_drawing():
    assert png(F("bogus", "nope")) == png(F())


def test_the_search_term_never_reaches_the_renderer_or_the_image():
    assert fc._key(F(search="cin")) == fc._key(F(search="a totally different term")) == (None, None, False, False, True)
    assert png(F(search="cin")) == png(F(search="\u202e<@everyone>")) != png(F())
    assert "cin" not in repr(fc._key(F(search="cin")))


def test_renders_are_cached_by_what_is_drawn():
    png(F(search="one"))
    png(F(search="two"))
    assert fc._cached.cache_info().currsize == 1


def test_each_send_gets_a_fresh_file():
    a, b = fc.filter_file(png()), fc.filter_file(png())
    assert a is not b and a.filename == fc.FILTER_FILE == "filter.png"


def test_header_counts_the_active_filters():
    assert fc._count(None, None, False, False, False) == 0
    assert fc._count("rare", "tide", True, True, True) == 5
    assert fc._count("rare", None, False, True, False) == 2
    assert crop(F(), (400, 10, 696, 54)) != crop(F("rare"), (400, 10, 696, 54))


# ---------------------------------------------------------------- only the part a control owns changes

def test_a_rarity_only_changes_the_rarity_row_and_the_header(flat_colours):
    assert crop(F(), RARITY_ROW) != crop(F("rare"), RARITY_ROW) != crop(F("epic"), RARITY_ROW)
    assert crop(F(), ELEMENT_ROW) == crop(F("rare"), ELEMENT_ROW)
    for box in CHIPS.values():
        assert crop(F(), box) == crop(F("rare"), box)


def test_an_element_only_changes_the_element_row_and_the_header(flat_colours):
    # With an element chosen the backdrop tints to it (flat here), so compare two chosen elements.
    assert crop(F(None, "tide"), ELEMENT_ROW) != crop(F(None, "umbra"), ELEMENT_ROW)
    assert crop(F(None, "tide"), RARITY_ROW) == crop(F(None, "umbra"), RARITY_ROW)
    for box in CHIPS.values():
        assert crop(F(None, "tide"), box) == crop(F(None, "umbra"), box)
    assert crop(F(None, "tide"), (400, 10, 696, 54)) == crop(F(None, "umbra"), (400, 10, 696, 54))


@pytest.mark.parametrize("name,flt", [
    ("shiny", F(None, None, True)), ("favorite", F(None, None, False, True)), ("search", F(None, None, False, False, "x")),
])
def test_each_toggle_only_changes_its_own_chip(name, flt, flat_colours):
    for other, box in CHIPS.items():
        if other == name:
            assert crop(F(), box) != crop(flt, box)
        else:
            assert crop(F(), box) == crop(flt, box)
    assert crop(F(), RARITY_ROW) == crop(flt, RARITY_ROW) and crop(F(), ELEMENT_ROW) == crop(flt, ELEMENT_ROW)


def test_a_lit_chip_is_brighter_than_an_unlit_one(flat_colours):
    def brightness(flt, box):
        return sum(img(flt).crop(box).convert("L").tobytes())
    assert brightness(F(None, None, True), CHIPS["shiny"]) > brightness(F(), CHIPS["shiny"])


def test_every_element_tile_draws_its_sigil(flat_colours):
    # Unlit tiles are flat dark panels, so a drawn sigil shows up as a wide brightness spread in its centre.
    tile_w = (W - 48 - 6 * 9) / 10
    for i, element in enumerate(ELEMENTS, start=1):
        cx = 24 + i * (tile_w + 6) + tile_w / 2
        spread = img().convert("L").crop((int(cx - 12), 160, int(cx + 12), 184))
        lo, hi = spread.getextrema()
        assert hi - lo > 40, f"no sigil drawn for {element}"


def test_a_lit_toggle_shows_a_tick_and_an_unlit_one_does_not(flat_colours):
    def lum(flt, x, y):
        return img(flt).convert("L").getpixel((x, y))
    on, off = F(None, None, True), F()
    cx, cy = 24 + 26, 247  # centre of the shiny chip's indicator
    assert lum(on, cx + 6, cy + 4) - lum(on, cx - 1, cy + 4) > 80   # dark tick stroke inside a bright disc
    assert abs(lum(off, cx + 6, cy + 4) - lum(off, cx - 1, cy + 4)) < 15


# ---------------------------------------------------------------- the wrapper

def test_filter_art_sets_the_image_and_returns_a_file():
    embed = discord.Embed(title="t")
    file = run(fc.filter_art(embed, F("rare")))
    assert file.filename == "filter.png" and embed.image.url == "attachment://filter.png"


def test_filter_art_failure_returns_none_and_leaves_the_embed_alone(monkeypatch):
    async def boom(flt):
        raise RuntimeError("no font")

    monkeypatch.setattr(fc, "filter_card_png", boom)
    embed = discord.Embed(title="t")
    assert run(fc.filter_art(embed, F())) is None and embed.image.url is None


# ---------------------------------------------------------------- the card screen

def box_view():
    return views.CollectionCardView(7, None, rows=rows_of(5), total=5)


def spy_edits(inter, rec):
    edits = []

    async def edit(*args, **kwargs):
        rec.add("edit_original")
        edits.append(kwargs)

    inter.edit_original_response = edit
    return edits


def spy_render(monkeypatch, rec):
    seen = []

    async def spy(flt):
        rec.add("render")
        seen.append(flt)
        await asyncio.sleep(0)
        return await REAL_RENDER(flt)

    monkeypatch.setattr(fc, "filter_card_png", spy)
    return seen


def names(edit):
    return [a.filename for a in edit["attachments"]]


def test_opening_defers_first_then_draws_then_edits(monkeypatch):
    rec = Recorder()
    seen = spy_render(monkeypatch, rec)
    view = views.CollectionCardView(7, None, rows=rows_of(5), total=5, flt=F("epic", "tide"))
    inter = make_interaction(rec)
    edits = spy_edits(inter, rec)
    run(view._open_filters(inter))
    assert rec.calls == ["response", "render", "edit_original"]
    assert isinstance(edits[0]["view"], views.CollectionFilterCardView) and names(edits[0]) == ["filter.png"]
    assert edits[0]["embed"].image.url == "attachment://filter.png" and edits[0]["content"] is None
    assert seen[0] == F("epic", "tide")  # the real active filter reaches the renderer


def test_a_failed_render_still_opens_the_plain_screen_and_clears_the_collection_card(monkeypatch):
    async def boom(flt):
        raise RuntimeError("x")

    monkeypatch.setattr(fc, "filter_card_png", boom)
    rec = Recorder()
    inter = make_interaction(rec)
    edits = spy_edits(inter, rec)
    run(box_view()._open_filters(inter))
    assert edits[0]["attachments"] == [] and edits[0]["embed"].image.url is None
    assert edits[0]["embed"].title == text("collection.filter.title")


def test_a_failed_edit_tells_the_player_instead_of_raising():
    rec = Recorder()
    inter = make_interaction(rec)
    sent = []

    async def edit(*a, **kw):
        raise RuntimeError("gone")

    async def send(*a, **kw):
        sent.append((a, kw))

    inter.edit_original_response = edit
    inter.followup = SimpleNamespace(send=send)
    run(box_view()._open_filters(inter))
    assert sent[0][0][0] == text("collection.error") and sent[0][1]["ephemeral"] is True


@pytest.mark.parametrize("callback,data,check", [
    ("_rarity", {"values": ["epic"]}, lambda f: f.rarity == "epic"),
    ("_element", {"values": ["tide"]}, lambda f: f.element == "tide"),
    ("_shiny", {}, lambda f: f.shiny),
    ("_favorite", {}, lambda f: f.favorite),
])
def test_every_change_defers_first_then_redraws_with_the_new_filter(monkeypatch, callback, data, check):
    rec = Recorder()
    seen = spy_render(monkeypatch, rec)
    screen = views.CollectionFilterCardView(box_view())
    inter = make_interaction(rec)
    inter.data = data
    edits = spy_edits(inter, rec)
    run(getattr(screen, callback)(inter))
    assert rec.calls == ["response", "render", "edit_original"]
    assert check(screen.flt) and check(seen[0]) and edits[0]["view"] is screen and names(edits[0]) == ["filter.png"]


def test_clear_redraws_an_empty_filter(monkeypatch):
    rec = Recorder()
    seen = spy_render(monkeypatch, rec)
    screen = views.CollectionFilterCardView(views.CollectionCardView(7, None, rows=rows_of(2), total=2, flt=F("rare")))
    inter = make_interaction(rec)
    edits = spy_edits(inter, rec)
    run(screen._clear(inter))
    assert not screen.flt.active and not seen[0].active and names(edits[0]) == ["filter.png"]


def test_the_search_button_opens_the_card_modal_and_submit_redraws(monkeypatch):
    rec = Recorder()
    seen = spy_render(monkeypatch, rec)
    screen = views.CollectionFilterCardView(box_view())
    modals = []

    async def send_modal(modal):
        rec.add("response")
        modals.append(modal)

    inter = make_interaction(rec)
    inter.response.send_modal = send_modal
    run(screen._search(inter))
    assert isinstance(modals[0], views.CardSearchModal) and rec.calls == ["response"]
    modals[0].term._value = "  cin  drop "
    rec.calls.clear()
    inter2 = make_interaction(rec)
    edits = spy_edits(inter2, rec)
    run(modals[0].on_submit(inter2))
    assert rec.calls == ["response", "render", "edit_original"]
    assert screen.flt.search == "cin drop" and seen[0].search == "cin drop"
    assert "cin drop" in edits[0]["embed"].description  # the term stays as text; the card only shows "on"
    assert names(edits[0]) == ["filter.png"] and edits[0]["view"] is screen


def test_a_failed_redraw_tells_the_player(monkeypatch):
    screen = views.CollectionFilterCardView(box_view())
    rec = Recorder()
    inter = make_interaction(rec)
    sent = []

    async def edit(*a, **kw):
        raise RuntimeError("gone")

    async def send(*a, **kw):
        sent.append(a[0])

    inter.edit_original_response = edit
    inter.followup = SimpleNamespace(send=send)
    run(screen._shiny(inter))
    assert sent == [text("collection.error")]


def test_the_card_screen_has_the_same_controls_and_owner_check_as_the_plain_one():
    plain = views.CollectionFilterView(views.CollectionBoxView(7, None, rows=[], total=0))
    card = views.CollectionFilterCardView(views.CollectionBoxView(7, None, rows=[], total=0))
    sig = lambda v: [(type(c).__name__, getattr(c, "label", None), getattr(c, "placeholder", None)) for c in v.children]  # noqa: E731
    assert sig(card) == sig(plain)
    other = make_interaction(Recorder())
    other.user = SimpleNamespace(id=8)
    assert run(card.interaction_check(other)) is False and run(card.interaction_check(make_interaction(Recorder()))) is True


def test_show_results_leaves_the_filter_card_behind(monkeypatch):
    async def allow(*a, **kw):
        return SimpleNamespace(allowed=True, reason=None)

    async def listing(rows, total):
        async def fake(user_id, clone_id, **kw):
            return rows, total, 0
        return fake

    monkeypatch.setattr(views, "check_player_allowed", allow)
    for rows, total, expected in [(rows_of(3), 3, ["box.png"]), ([], 0, [])]:
        monkeypatch.setattr(views, "list_owned", run(listing(rows, total)))
        screen = views.CollectionFilterCardView(box_view())
        screen.flt = F("rare")
        inter = make_interaction(Recorder())
        edits = []

        async def edit(*a, _e=edits, **kw):
            _e.append(kw)

        inter.edit_original_response = edit
        run(screen._apply(inter))
        assert names(edits[0]) == expected and "filter.png" not in names(edits[0])


def test_the_plain_box_still_opens_the_plain_filter_screen():
    rec = Recorder()
    inter = make_interaction(rec)
    seen = []

    async def edit_message(**kw):
        rec.add("response")
        seen.append(kw)

    inter.response.edit_message = edit_message
    run(views.CollectionBoxView(7, None, rows=rows_of(2), total=2)._open_filters(inter))
    assert rec.calls == ["response"] and type(seen[0]["view"]) is views.CollectionFilterView and "attachments" not in seen[0]
