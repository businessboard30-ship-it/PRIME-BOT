"""Drawn Dex page and species card: rendering, spoiler rules, fallbacks and the new views."""

import asyncio
import io
from types import SimpleNamespace

import discord
from PIL import Image

import discord_bot.cogs._views_catch_collection as views
from modules import catch_dex as dx
from modules import catch_dex_card as cards
from modules.catch_collection import DexEntry, dex_page, dex_summary
from modules.catch_i18n import text
from modules.catch_species import all_species
from tests.unit.test_catch_dex_info import entry, full_dex
from tests.unit.test_catch_interaction_timing import Recorder, assert_response_first, make_interaction, slow
from tests.unit.test_catch_phase3 import edits_to
from tests.unit.notice_helpers import shown

PNG = b"\x89PNG\r\n\x1a\n"
MIX = {1: (True, 3, 1), 2: (True, 1, 0), 3: (True, 0, 0), 6: (True, 2, 0), 7: (True, 0, 0), 12: (True, 5, 2)}


def run(coro):
    return asyncio.run(coro)


def size(data: bytes):
    return Image.open(io.BytesIO(data)).size


def page_bytes(entries, page=0):
    chunk, page = dex_page(entries, page)
    progress = tuple((p.rarity, p.caught, p.total) for p in dx.rarity_completion(entries))
    pages = max(1, -(-len(entries) // 12))
    return cards._cached_dex(cards.tiles_for(chunk), dex_summary(entries), progress, page, pages)


# ---------------------------------------------------------------- spoiler rules

def test_tiles_blank_everything_the_player_has_not_discovered():
    tiles = cards.tiles_for(full_dex(MIX)[:8])
    by_id = {t[0]: t for t in tiles}
    assert by_id[1][1:] == ("caught", "common", "ember", True)
    assert by_id[3][1] == "seen" and by_id[3][2] == "" and by_id[3][3] == "ember"  # seen: element yes, rarity no
    assert by_id[4] == (4, "unknown", "", "", False)  # never seen: nothing but the number


def test_an_undiscovered_species_cannot_change_the_card():
    base = DexEntry(4, "Hidden", "mythic", "umbra", False, 0, 0)
    other = DexEntry(4, "Other", "common", "frost", False, 0, 0)
    assert cards.tiles_for([base]) == cards.tiles_for([other])
    assert cards._cached_dex(cards.tiles_for([base]), (0, 0, 1), (), 0, 1) == \
        cards._cached_dex(cards.tiles_for([other]), (0, 0, 1), (), 0, 1)


def test_seen_species_is_drawn_dimmer_than_a_caught_one_and_unknown_differs_from_both():
    states = [cards._cached_dex(cards.tiles_for([e]), (0, 0, 1), (), 0, 1) for e in (
        entry(1, seen=True, caught=1), entry(1, seen=True), entry(1))]
    assert len(set(states)) == 3


def test_species_card_never_carries_stats_for_a_species_that_is_not_caught():
    seen = dx.species_info(full_dex({3: (True, 0, 0)}), 3)
    caught = dx.species_info(full_dex({3: (True, 2, 1)}), 3)
    assert cards.species_key(seen)[6] is None and cards.species_key(caught)[6] is not None
    assert cards.species_key(seen)[7:] == (0, 0)
    assert cards._cached_species(cards.species_key(seen)) != cards._cached_species(cards.species_key(caught))


# ---------------------------------------------------------------- rendering

def test_dex_card_renders_for_every_shape_of_dex():
    shapes = [
        [], full_dex(), full_dex(MIX), full_dex({i: (True, 1, 1) for i in all_species()}),
        full_dex({i: (True, 0, 0) for i in all_species()}), full_dex(MIX)[:5], full_dex(MIX)[:30],
    ]
    for entries in shapes:
        for page in (0, 1, 99):  # an out-of-range page is clamped, never an error
            data = page_bytes(entries, page)
            assert data.startswith(PNG) and size(data) == (cards.W, cards.DEX_H)


def test_dex_card_changes_with_progress_page_and_shiny():
    base = page_bytes(full_dex(MIX))
    assert page_bytes(full_dex({**MIX, 1: (True, 3, 0)})) != base       # shiny sparkle gone
    assert page_bytes(full_dex({**MIX, 4: (True, 1, 0)})) != base       # one more caught
    assert page_bytes(full_dex(MIX), 1) != base


def test_species_card_renders_for_every_species_caught_and_seen():
    for sid in list(all_species())[::5]:
        for state in ((True, 2, 1), (True, 0, 0)):
            info = dx.species_info(full_dex({sid: state}), sid)
            data = cards._cached_species(cards.species_key(info))
            assert data.startswith(PNG) and size(data) == (cards.W, cards.SPECIES_H)


# ---------------------------------------------------------------- helpers

def test_dex_art_attaches_the_card_and_drops_only_the_completion_field():
    embed = discord.Embed(title="t")
    embed.add_field(name="Completion", value="x")
    embed.add_field(name="Keep", value="y")
    file = run(cards.dex_art(embed, full_dex(MIX), 0, drop_field="Completion"))
    assert file.filename == cards.DEX_FILE and embed.image.url == f"attachment://{cards.DEX_FILE}"
    assert [f.name for f in embed.fields] == ["Keep"]


def test_dex_art_and_species_art_failures_leave_the_embed_untouched(monkeypatch):
    def boom(*_a):
        raise RuntimeError("no pillow")

    monkeypatch.setattr(cards, "_cached_dex", boom)
    monkeypatch.setattr(cards, "_cached_species", boom)
    embed = discord.Embed(title="t")
    embed.add_field(name="Completion", value="x")
    assert run(cards.dex_art(embed, full_dex(MIX), 0, drop_field="Completion")) is None
    info = dx.species_info(full_dex(MIX), 1)
    assert run(cards.species_art(embed, info, drop_field="Completion")) is None
    assert embed.image.url is None and len(embed.fields) == 1


def test_kwargs_helpers():
    file = discord.File(io.BytesIO(b"x"), filename="a.png")
    assert cards.edit_kwargs(file) == {"attachments": [file]} and cards.edit_kwargs(None) == {"attachments": []}
    assert cards.send_kwargs(file) == {"file": file} and cards.send_kwargs(None) == {}


# ---------------------------------------------------------------- views

def test_card_view_message_has_the_card_and_no_plain_completion_field():
    view = views.DexCardView(7, full_dex(MIX))
    edit, send = run(view.message(edit=True)), run(view.message(edit=False))
    assert len(edit["attachments"]) == 1 and "file" in send
    assert text("dex.completion") not in [f.name for f in edit["embed"].fields]
    assert text("dex.completion") in [f.name for f in views.DexBrowseView(7, full_dex(MIX)).embed().fields]  # old view unchanged
    assert isinstance(view, views.DexBrowseView) and len(view.children) == len(views.DexBrowseView(7, full_dex(MIX)).children)


def test_page_turn_defers_first_then_redraws_the_next_page():
    rec = Recorder()
    view = views.DexCardView(7, full_dex(MIX))
    inter = make_interaction(rec)
    edits = edits_to(inter)
    run(view._next(inter))
    assert_response_first(rec)
    assert view.page == 1 and len(edits) == 1 and edits[0]["view"] is view and len(edits[0]["attachments"]) == 1
    assert edits[0]["attachments"][0].fp.getvalue() == page_bytes(full_dex(MIX), 1)
    inter = make_interaction(rec)
    edits = edits_to(inter)
    run(view._prev(inter))
    assert view.page == 0 and edits[0]["attachments"][0].fp.getvalue() == page_bytes(full_dex(MIX), 0)


def test_species_pick_defers_first_and_shows_the_species_card_then_back_returns_to_the_page():
    rec = Recorder()
    view = views.DexCardView(7, full_dex(MIX), page=0)
    inter = make_interaction(rec)
    inter.data = {"values": ["1"]}
    edits = edits_to(inter)
    run(view._open_species(inter))
    assert_response_first(rec)
    assert isinstance(edits[0]["view"], views.DexCardInfoView) and edits[0]["view"].dex is view
    assert edits[0]["attachments"][0].filename == cards.SPECIES_FILE
    assert text("dex.info.stats") not in [f.name for f in edits[0]["embed"].fields]
    back = make_interaction(rec)
    back_edits = edits_to(back)
    run(edits[0]["view"]._back(back))
    assert back_edits[0]["view"] is view and back_edits[0]["attachments"][0].filename == cards.DEX_FILE


def test_species_pick_of_a_seen_only_species_has_no_stats_to_drop():
    rec = Recorder()
    view = views.DexCardView(7, full_dex(MIX))
    inter = make_interaction(rec)
    inter.data = {"values": ["3"]}
    edits = edits_to(inter)
    run(view._open_species(inter))
    assert len(edits[0]["attachments"]) == 1 and edits[0]["embed"].description == text("dex.info.locked")


def test_species_pick_refuses_undiscovered_ids_without_deferring():
    view = views.DexCardView(7, full_dex(MIX))
    sent = []
    for value in ("4", "9999", "x"):
        rec = Recorder()
        inter = make_interaction(rec)
        inter.data = {"values": [value]}

        async def send_message(*a, _s=sent, **kw):
            _s.append(shown(a, kw))

        inter.response.send_message = send_message
        run(view._open_species(inter))
    assert sent == [text("dex.info.not_found")] * 3


def test_card_views_are_still_owner_only():
    view = views.DexCardView(7, [])
    other, mine = make_interaction(Recorder()), make_interaction(Recorder())
    other.user.id, mine.user.id = 8, 7
    assert run(view.interaction_check(other)) is False and run(view.interaction_check(mine)) is True
    assert run(views.DexCardInfoView(view).interaction_check(other)) is False


def test_open_dex_sends_the_card_view(monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(views, "check_player_allowed", slow(rec, "gate", SimpleNamespace(allowed=True, reason=None)))
    monkeypatch.setattr(views, "load_dex", slow(rec, "db", full_dex(MIX)))
    inter = make_interaction(rec)
    sent = []

    async def send(*a, **kw):
        sent.append(kw)

    inter.followup = SimpleNamespace(send=send)
    run(views.open_dex(inter))
    assert_response_first(rec)
    assert isinstance(sent[0]["view"], views.DexCardView) and sent[0]["file"].filename == cards.DEX_FILE


def test_card_failure_falls_back_to_the_plain_dex(monkeypatch):
    def boom(*_a):
        raise RuntimeError("no pillow")

    monkeypatch.setattr(cards, "_cached_dex", boom)
    view = views.DexCardView(7, full_dex(MIX))
    edit = run(view.message(edit=True))
    assert edit["attachments"] == [] and edit["embed"].image.url is None
    assert text("dex.completion") in [f.name for f in edit["embed"].fields]


def test_species_key_hides_stats_and_counts_even_if_a_not_caught_info_carries_them():
    leaky = dx.SpeciesInfo(1, "Cindrop", "common", "ember", None, False, 5, 2, "land",
                           {"vigor": 9, "power": 9, "guard": 9, "speed": 9, "spirit": 9}, None, None, None, None, False)
    key = cards.species_key(leaky)
    assert key[6] is None and key[7:] == (0, 0)
    clean = dx.SpeciesInfo(1, "Cindrop", "common", "ember", None, False, 0, 0, None, None, None, None, None, None, False)
    assert cards._cached_species(key) == cards._cached_species(cards.species_key(clean))
