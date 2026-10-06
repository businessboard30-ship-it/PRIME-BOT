"""Collection box: filters, search, page jump (query layer, filter screen, modals, timing)."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import discord_bot.cogs._views_catch_collection as views
from modules import catch_collection as cc
from modules import catch_emoji
from tests.unit.test_catch_collection import FakeDb, owned_rec, patch_db
from tests.unit.test_catch_interaction_timing import Recorder, assert_response_first, make_interaction, slow
from tests.unit.test_catch_phase3 import edits_to, row, sent_to

LOCALE = json.loads((Path(__file__).resolve().parent.parent.parent / "locales" / "en.json").read_text(encoding="utf-8"))
F = cc.CollectionFilter


def with_modal(rec):
    inter = make_interaction(rec)
    sent = []

    async def send_modal(modal):
        rec.add("response")
        sent.append(modal)

    inter.response.send_modal = send_modal
    return inter, sent


def allow(monkeypatch, rec=None):
    monkeypatch.setattr(views, "check_player_allowed", slow(rec or Recorder(), "gate", SimpleNamespace(allowed=True, reason=None)))


# ---- filter model and SQL -------------------------------------------------

def test_filter_clean_drops_unknown_values_and_trims_search():
    flt = F("legendary", "plasma", 1, 0, "  Cin   drop " + "x" * 80).clean()
    assert flt.rarity is None and flt.element is None and flt.shiny is True and flt.favorite is False
    assert flt.search.startswith("Cin drop") and len(flt.search) == cc.SEARCH_MAX
    assert not F().active and F(rarity="rare").active and F(search="a").active


def test_filter_sql_is_parameterised_and_numbered_from_start():
    sql, params = cc.filter_sql(F("rare", "ember", True, True, "cin"), 5)
    assert params == [2, "ember", "%cin%"]
    assert "$5::smallint" in sql and "$6" in sql and "$7" in sql and "$8" not in sql
    assert "o.shiny" in sql and "o.favorite" in sql and "rare" not in sql and "cin" not in sql
    assert cc.filter_sql(None, 3) == ("", []) and cc.filter_sql(F(), 3) == ("", [])


def test_filter_sql_escapes_like_wildcards_and_never_interpolates_input():
    sql, params = cc.filter_sql(F(search="50%_\\'; DROP TABLE x;--"), 3)
    assert "DROP" not in sql and "'" not in sql.replace("ESCAPE '\\'", "")
    assert params[0].startswith("%50\\%\\_\\\\") and params[0].endswith("%")
    sql, params = cc.filter_sql(F(rarity="x'; DROP", element="y"), 3)
    assert (sql, params) == ("", [])


def test_filter_summary():
    assert cc.filter_summary(None) == "" and cc.filter_summary(F()) == ""
    assert cc.filter_summary(F("epic", "tide", True, True, "wave")) == 'Epic, Tide, Shiny, Favourite, "wave"'


def test_list_owned_without_filter_keeps_old_query_shape(monkeypatch):
    db = FakeDb(owned=[owned_rec(1)], total=1)
    patch_db(monkeypatch, db)
    asyncio.run(cc.list_owned(42, 3))
    assert [len(a) for _, a in db.queries] == [2, 4]
    assert db.queries[-1][1][2:] == (cc.COLLECTION_PAGE_SIZE, 0)


def test_list_owned_with_filter_applies_it_to_count_and_list(monkeypatch):
    db = FakeDb(owned=[owned_rec(1)], total=1)
    patch_db(monkeypatch, db)
    asyncio.run(cc.list_owned(42, None, flt=F("rare", search="cin"), page=0))
    (count_sql, count_args), (list_sql, list_args) = db.queries
    assert count_args == (42, -1, 2, "%cin%") and "$3::smallint" in count_sql and "$4" in count_sql
    assert list_args == (42, -1, 10, 0, 2, "%cin%") and "$5::smallint" in list_sql and "$6" in list_sql
    assert "o.user_id=$1 AND o.clone_key=$2" in count_sql and "o.user_id=$1 AND o.clone_key=$2" in list_sql


# ---- views ----------------------------------------------------------------

def test_box_view_is_the_browse_view_plus_filter_and_jump_and_fits_discord():
    rows = [row(i) for i in range(1, 11)]
    base = views.CollectionBrowseView(7, None, rows=rows, total=25)
    box = views.CollectionBoxView(7, None, rows=rows, total=25)
    assert [(type(c), c.row) for c in box.children[:len(base.children)]] == [(type(c), c.row) for c in base.children]
    extra = box.children[len(base.children):]
    assert [c.label for c in extra] == [LOCALE["catch.collection.filter.button"], LOCALE["catch.collection.jump.button"]]
    assert all(not c.disabled for c in extra)
    per_row = {}
    for c in box.children:
        per_row[c.row] = per_row.get(c.row, 0) + 1
    assert max(per_row) <= 4 and all(n <= 5 for n in per_row.values()) and len(box.to_components()) <= 5
    one_page = views.CollectionBoxView(7, None, rows=rows[:3], total=3)
    assert one_page.children[-1].disabled is True and one_page.children[-2].disabled is False


def test_filter_embed_and_empty_message_name_the_filters():
    flt = F("rare", shiny=True)
    empty = views._collection_embed([], 0, 0, "recent", flt)
    assert "Rare, Shiny" in empty.description
    assert views._collection_embed([], 0, 0, "recent").description == LOCALE["catch.collection.empty"]
    filled = views._collection_embed([row()], 1, 0, "recent", flt)
    assert "Filter: Rare, Shiny" in filled.footer.text
    assert "Filter" not in views._collection_embed([row()], 1, 0, "recent").footer.text


def test_open_filters_responds_with_the_screen_and_no_db(monkeypatch):
    rec = Recorder()
    box = views.CollectionBoxView(7, None, rows=[row()], total=1)
    inter = make_interaction(rec)
    asyncio.run(box._open_filters(inter))
    assert rec.calls == ["response"]


def test_filter_screen_selects_and_toggles_update_state_without_db():
    rec = Recorder()
    box = views.CollectionBoxView(7, None, rows=[row()], total=1)
    screen = views.CollectionFilterView(box)
    assert len(screen.children[0].options) == 6 and len(screen.children[1].options) == 10
    assert all(o.value in ("any", "common", "uncommon", "rare", "epic", "mythic") for o in screen.children[0].options)
    assert screen.children[-2].disabled is True  # Clear is off with nothing to clear
    for data, cb, check in [
        ({"values": ["epic"]}, screen._rarity, lambda f: f.rarity == "epic"),
        ({"values": ["tide"]}, screen._element, lambda f: f.element == "tide"),
        ({}, screen._shiny, lambda f: f.shiny),
        ({}, screen._favorite, lambda f: f.favorite),
    ]:
        inter = make_interaction(rec)
        inter.data = data
        asyncio.run(cb(inter))
        assert check(screen.flt)
    assert screen.children[-2].disabled is False
    inter = make_interaction(rec)
    inter.data = {"values": ["bogus"]}
    asyncio.run(screen._rarity(inter))
    assert screen.flt.rarity is None  # unknown value is dropped, not trusted
    inter = make_interaction(rec)
    inter.data = {"values": ["any"]}
    asyncio.run(screen._element(inter))
    assert screen.flt.element is None
    inter = make_interaction(rec)
    asyncio.run(screen._clear(inter))
    assert not screen.flt.active and set(rec.calls) == {"response"}


def test_filter_screen_only_accepts_its_owner():
    rec = Recorder()
    screen = views.CollectionFilterView(views.CollectionBoxView(7, None, rows=[], total=0))
    other = make_interaction(rec)
    other.user = SimpleNamespace(id=8)
    assert asyncio.run(screen.interaction_check(other)) is False
    assert asyncio.run(screen.interaction_check(make_interaction(rec))) is True


def test_show_results_defers_first_then_gates_then_loads_page_one(monkeypatch):
    rec = Recorder()
    allow(monkeypatch, rec)
    seen = {}

    async def fake_list(user_id, clone_id, **kw):
        rec.add("db")
        seen.update(kw)
        return [row(4, rarity="rare")], 1, 0

    monkeypatch.setattr(views, "list_owned", fake_list)
    box = views.CollectionBoxView(7, 3, rows=[row()], total=30, page=2)
    screen = views.CollectionFilterView(box)
    screen.flt = F("rare", shiny=True)
    inter = make_interaction(rec)
    edits = edits_to(inter)
    asyncio.run(screen._apply(inter))
    assert_response_first(rec)
    assert rec.calls.index("gate") < rec.calls.index("db")
    assert seen["flt"] == F("rare", shiny=True) and seen["page"] == 0
    assert box.flt.rarity == "rare" and box.page == 0 and edits[0]["view"] is box
    assert "Filter: Rare, Shiny" in edits[0]["embed"].footer.text


def test_show_results_is_refused_by_the_gate_and_busy_taps_are_ignored(monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(views, "check_player_allowed", slow(rec, "gate", SimpleNamespace(allowed=False, reason="off")))
    monkeypatch.setattr(views, "list_owned", slow(rec, "db", ([], 0, 0)))
    screen = views.CollectionFilterView(views.CollectionBoxView(7, None, rows=[], total=0))
    inter = make_interaction(rec)
    sent = sent_to(inter)
    asyncio.run(screen._apply(inter))
    assert "db" not in rec.calls and "off" in sent[0][0][0]
    screen._busy = True
    inter = make_interaction(rec)
    sent = sent_to(inter)
    asyncio.run(screen._apply(inter))
    assert sent[0][0][0] == LOCALE["catch.collection.busy"]


def test_back_from_a_creature_keeps_the_active_filter(monkeypatch):
    rec = Recorder()
    seen = {}

    async def fake_list(user_id, clone_id, **kw):
        seen.update(kw)
        return [row(5)], 1, 0

    monkeypatch.setattr(views, "list_owned", fake_list)
    box = views.CollectionBoxView(7, None, rows=[row(5)], total=1, flt=F("epic"))
    inter = make_interaction(rec)
    edits_to(inter)
    asyncio.run(box._back_to_list(inter))
    assert seen["flt"] == F("epic") and seen["sort"] == "recent"


def test_sort_and_paging_reuse_the_filter(monkeypatch):
    rec = Recorder()
    seen = []

    async def fake_list(user_id, clone_id, **kw):
        seen.append(kw)
        return [row()], 30, kw["page"]

    monkeypatch.setattr(views, "list_owned", fake_list)
    box = views.CollectionBoxView(7, None, rows=[row()], total=30, flt=F(favorite=True))
    inter = make_interaction(rec)
    edits_to(inter)
    asyncio.run(box._next(inter))
    assert_response_first(rec)
    assert seen[0]["flt"] == F(favorite=True) and seen[0]["page"] == 1


def test_search_button_opens_a_modal_and_submit_updates_the_screen_without_db():
    rec = Recorder()
    screen = views.CollectionFilterView(views.CollectionBoxView(7, None, rows=[], total=0))
    inter, modals = with_modal(rec)
    asyncio.run(screen._search(inter))
    assert rec.calls == ["response"] and isinstance(modals[0], views.SearchModal)
    assert modals[0].term.max_length == cc.SEARCH_MAX
    modals[0].term._value = "  cin  drop "
    inter2 = make_interaction(rec)
    edits = edits_to(inter2)
    asyncio.run(modals[0].on_submit(inter2))
    assert screen.flt.search == "cin drop" and edits[0]["view"] is screen
    assert "cin drop" in edits[0]["embed"].description


def test_page_jump_modal_validates_clamps_and_checks_the_gate(monkeypatch):
    rec = Recorder()
    allow(monkeypatch, rec)
    pages_seen = []

    async def fake_list(user_id, clone_id, **kw):
        pages_seen.append(kw["page"])
        return [row()], 25, min(kw["page"], 2)

    monkeypatch.setattr(views, "list_owned", fake_list)
    box = views.CollectionBoxView(7, None, rows=[row()], total=25)
    inter, modals = with_modal(rec)
    asyncio.run(box._open_jump(inter))
    modal = modals[0]
    assert isinstance(modal, views.PageJumpModal) and "3" in modal.number.label
    for text_in, expect_page in [("2", 1), ("99", 98), ("0", 0), ("-4", 0)]:
        modal.number._value = text_in
        i = make_interaction(rec)
        edits_to(i)
        asyncio.run(modal.on_submit(i))
        assert pages_seen[-1] == expect_page
    n = len(pages_seen)
    modal.number._value = "abc"
    i = make_interaction(rec)
    sent = sent_to(i)
    asyncio.run(modal.on_submit(i))
    assert len(pages_seen) == n and "1 and 3" in sent[0][0][0]


def test_new_emoji_keys_exist_and_old_ones_are_untouched():
    for key in ("filter", "jump", "clear"):
        assert catch_emoji.mark("ui", key) != catch_emoji.FALLBACK
    for key in ("evolve", "profile", "dex", "collection", "back", "ok", "coins"):
        assert key in catch_emoji.UI
