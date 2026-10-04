"""Collection + Dex: queries, formatting, ownership, interaction timing, hub wiring."""
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import discord

import discord_bot.cogs._views_catch_collection as views
import discord_bot.cogs.catch as catch
from modules import catch_collection as cc
from tests.unit.test_catch_interaction_timing import Recorder, assert_response_first, make_interaction, slow


class FakeDb:
    def __init__(self, *, owned=(), dex=(), total=None, update_result=7):
        self.owned, self.dex, self.total, self.update_result = list(owned), list(dex), total, update_result
        self.queries = []

    async def fetchval(self, sql, *args):
        self.queries.append((sql, args))
        if sql.startswith("UPDATE"):
            return self.update_result
        return len(self.owned) if self.total is None else self.total

    async def fetch(self, sql, *args):
        self.queries.append((sql, args))
        return self.dex if "catch_dex" in sql else self.owned


def patch_db(monkeypatch, db):
    @asynccontextmanager
    async def fake(conn=None):
        yield db

    monkeypatch.setattr(cc.catch_db, "connection", fake)
    monkeypatch.setattr(cc.catch_db, "transaction", fake)


def owned_rec(i, species_id=1, **kw):
    base = dict(id=i, species_id=species_id, level=5, nickname=None, shiny=False, special=False, favorite=False, locked=False)
    base.update(kw)
    return base


def test_page_math_clamps():
    assert cc.page_count(0, 10) == 1 and cc.page_count(10, 10) == 1 and cc.page_count(11, 10) == 2
    assert cc.clamp_page(-3, 25, 10) == 0 and cc.clamp_page(99, 25, 10) == 2


def test_list_owned_scopes_to_user_and_clone_and_whitelists_sort(monkeypatch):
    db = FakeDb(owned=[owned_rec(1)], total=1)
    patch_db(monkeypatch, db)
    rows, total, page = asyncio.run(cc.list_owned(42, 3, sort="'; DROP TABLE catch_owned; --"))
    sql, args = db.queries[-1]
    assert args[:2] == (42, 3) and "ORDER BY o.caught_at DESC" in sql and "DROP" not in sql
    assert total == 1 and page == 0 and rows[0].id == 1 and rows[0].name


def test_list_owned_main_bot_uses_clone_key_minus_one(monkeypatch):
    db = FakeDb(owned=[], total=0)
    patch_db(monkeypatch, db)
    asyncio.run(cc.list_owned(42, None))
    assert db.queries[-1][1][1] == -1


def test_list_owned_clamps_out_of_range_page(monkeypatch):
    db = FakeDb(owned=[owned_rec(1)], total=11)
    patch_db(monkeypatch, db)
    _, _, page = asyncio.run(cc.list_owned(1, None, page=50))
    assert page == 1 and db.queries[-1][1][3] == 10


def test_set_favorite_revalidates_ownership(monkeypatch):
    db = FakeDb(update_result=None)
    patch_db(monkeypatch, db)
    assert asyncio.run(cc.set_favorite(9, 42, None)) is None
    sql, args = db.queries[-1]
    assert "user_id=$2" in sql and "clone_key=$3" in sql and args == (9, 42, -1)


def test_set_favorite_returns_new_value(monkeypatch):
    patch_db(monkeypatch, FakeDb(update_result=True))
    assert asyncio.run(cc.set_favorite(9, 42, 1)) is True


def test_format_owned_line_shows_flags_and_nickname():
    row = cc.OwnedRow(5, 1, "Cindrop", "rare", 12, "Sparky", True, False, True, True)
    line = cc.format_owned_line(row)
    assert "Sparky (Cindrop)" in line and "Lv.12" in line and "✨" in line and "❤️" in line and "🔒" in line


def test_dex_hides_unseen_and_exclusive_species(monkeypatch):
    species = {
        1: {"id": 1, "name": "Cindrop", "rarity": "common", "element": "ember"},
        2: {"id": 2, "name": "Blazeling", "rarity": "uncommon", "element": "ember"},
        3: {"id": 3, "name": "Secretling", "rarity": "mythic", "element": "void", "exclusive_to": "event"},
    }
    monkeypatch.setattr(cc, "all_species", lambda: species)
    db = FakeDb(dex=[{"species_id": 1, "seen": True, "caught_count": 2, "shiny_caught": 1}])
    patch_db(monkeypatch, db)
    entries = asyncio.run(cc.load_dex(42, None))
    assert [e.species_id for e in entries] == [1, 2]  # exclusive species hidden until seen
    assert cc.dex_summary(entries) == (1, 1, 2)
    assert "✅" in cc.format_dex_line(entries[0]) and "×2" in cc.format_dex_line(entries[0])
    assert cc.format_dex_line(entries[1]).endswith("???") and "Blazeling" not in cc.format_dex_line(entries[1])


def test_dex_shows_exclusive_species_once_seen(monkeypatch):
    species = {3: {"id": 3, "name": "Secretling", "rarity": "mythic", "element": "void", "exclusive_to": "event"}}
    monkeypatch.setattr(cc, "all_species", lambda: species)
    db = FakeDb(dex=[{"species_id": 3, "seen": True, "caught_count": 0, "shiny_caught": 0}])
    patch_db(monkeypatch, db)
    entries = asyncio.run(cc.load_dex(1, None))
    assert len(entries) == 1 and "👀" in cc.format_dex_line(entries[0])


def test_dex_page_clamps():
    entries = [cc.DexEntry(i, f"S{i}", "common", "ember", False, 0, 0) for i in range(1, 30)]
    chunk, page = cc.dex_page(entries, 99)
    assert page == 2 and [e.species_id for e in chunk] == [25, 26, 27, 28, 29]


# ---- views --------------------------------------------------------------------------------------

def test_real_dex_covers_every_species_in_the_shipped_file(monkeypatch):
    db = FakeDb(dex=[])
    patch_db(monkeypatch, db)
    entries = asyncio.run(cc.load_dex(1, None))
    assert len(entries) >= 1
    assert all(len(cc.format_dex_line(e)) <= 120 for e in entries)


def test_collection_view_component_limits_and_rows():
    rows = [cc.OwnedRow(i, 1, "Cindrop", "common", 5, None, False, False, False, False) for i in range(1, 11)]
    view = views.CollectionView(7, None, page=0, rows=rows, total=25)
    assert len(view.children) == 4 and len(view.children[1].options) == 10
    assert {c.row for c in view.children} == {0, 1, 2}
    prev_btn, next_btn = view.children[2], view.children[3]
    assert prev_btn.disabled and not next_btn.disabled
    assert len(view.to_components()) == 3  # three action rows, well inside Discord's limit of five


def test_empty_collection_has_no_favorite_select():
    view = views.CollectionView(7, None, rows=[], total=0)
    assert all(not isinstance(c, discord.ui.Select) or c.row == 0 for c in view.children)
    assert all(b.disabled for b in view.children if isinstance(b, discord.ui.Button))


def test_collection_embed_fits_discord_limits():
    rows = [cc.OwnedRow(10**9 + i, 1, "Verylongcreaturename", "mythic", 100, "N" * 24, True, True, True, True) for i in range(10)]
    embed = views._collection_embed(rows, 10, 0, "recent")
    assert len(embed.description) <= 4096 and len(embed) <= 6000


def test_views_reject_other_users():
    sent = []

    async def send_message(*a, **k):
        sent.append(k)

    other = SimpleNamespace(user=SimpleNamespace(id=999), response=SimpleNamespace(send_message=send_message))
    assert asyncio.run(views.CollectionView(7, None).interaction_check(other)) is False
    assert asyncio.run(views.DexView(7, []).interaction_check(other)) is False
    assert sent and all(s.get("ephemeral") for s in sent)
    mine = SimpleNamespace(user=SimpleNamespace(id=7), response=SimpleNamespace(send_message=send_message))
    assert asyncio.run(views.DexView(7, []).interaction_check(mine)) is True


def test_favorite_callback_blocks_unowned_creature(monkeypatch):
    rec = Recorder()
    calls = []

    async def fake_fav(owned_id, user_id, clone_id):
        calls.append((owned_id, user_id, clone_id))
        return None

    monkeypatch.setattr(views, "set_favorite", fake_fav)
    monkeypatch.setattr(views, "list_owned", slow(rec, "db", ([], 0, 0)))
    view = views.CollectionView(7, 2, rows=[cc.OwnedRow(5, 1, "Cindrop", "common", 5, None, False, False, False, False)], total=1)
    inter = make_interaction(rec)
    inter.data = {"values": ["5"]}
    asyncio.run(view._favorite(inter))
    assert_response_first(rec)
    assert calls == [(5, 7, 2)] and "followup" in rec.calls


def test_open_collection_responds_before_gate_and_db(monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(views, "check_player_allowed", slow(rec, "gate", SimpleNamespace(allowed=True, reason=None)))
    monkeypatch.setattr(views, "list_owned", slow(rec, "db", ([], 0, 0)))
    asyncio.run(views.open_collection(make_interaction(rec)))
    assert_response_first(rec)
    assert rec.calls.index("gate") < rec.calls.index("db")


def test_open_dex_responds_before_gate_and_db(monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(views, "check_player_allowed", slow(rec, "gate", SimpleNamespace(allowed=True, reason=None)))
    monkeypatch.setattr(views, "load_dex", slow(rec, "db", []))
    asyncio.run(views.open_dex(make_interaction(rec)))
    assert_response_first(rec)
    assert rec.calls.index("gate") < rec.calls.index("db")


def test_blocked_gate_does_no_db_work(monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(views, "check_player_allowed", slow(rec, "gate", SimpleNamespace(allowed=False, reason="game_disabled")))
    monkeypatch.setattr(views, "list_owned", slow(rec, "db", ([], 0, 0)))
    monkeypatch.setattr(views, "load_dex", slow(rec, "db2", []))
    asyncio.run(views.open_collection(make_interaction(rec)))
    asyncio.run(views.open_dex(make_interaction(rec)))
    assert "db" not in rec.calls and "db2" not in rec.calls


def test_db_failure_shows_friendly_error(monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(views, "check_player_allowed", slow(rec, "gate", SimpleNamespace(allowed=True, reason=None)))

    async def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(views, "list_owned", boom)
    asyncio.run(views.open_collection(make_interaction(rec)))
    assert rec.calls[-1] == "followup"


def test_dex_page_buttons_edit_in_place():
    entries = [cc.DexEntry(i, f"S{i}", "common", "ember", False, 0, 0) for i in range(1, 30)]
    view = views.DexView(7, entries)
    edits = []

    async def edit_message(**kw):
        edits.append(kw)

    inter = SimpleNamespace(user=SimpleNamespace(id=7), response=SimpleNamespace(edit_message=edit_message))
    asyncio.run(view._next(inter))
    assert view.page == 1 and edits[0]["embed"].footer.text.endswith("Page 2/3")
    assert not view.children[0].disabled


# ---- hub wiring ---------------------------------------------------------------------------------

def test_hub_collection_and_dex_buttons_are_real_handlers():
    hub = catch.CatchHubView(category="collect")
    callbacks = {b.label: b.callback for b in hub.children if isinstance(b, discord.ui.Button)}
    assert callbacks["Collection"] is views.open_collection
    assert callbacks["Dex"] is views.open_dex


def test_dynamic_button_routes_collection_and_dex_after_restart(monkeypatch):
    seen = []

    async def fake_collection(interaction):
        seen.append("collection")

    async def fake_dex(interaction):
        seen.append("dex")

    monkeypatch.setitem(catch.REAL_ACTIONS, "collection", fake_collection)
    monkeypatch.setitem(catch.REAL_ACTIONS, "dex", fake_dex)
    for action in ("collection", "dex"):
        button = catch.CatchHubDynamicButton(discord.ui.Button(custom_id=f"catch:hub:collect:{action}"), action=action, category="collect")
        asyncio.run(button.callback(SimpleNamespace()))
    assert seen == ["collection", "dex"]
