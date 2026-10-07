"""Release: SQL contract, refusals, buddy clearing, confirm screen, timing, double-tap guard."""
import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

import discord_bot.cogs._views_catch_creature as views
from modules import catch_emoji as emoji
from modules import catch_release as rel
from modules.catch_i18n import text
from tests.unit.test_catch_interaction_timing import Recorder, assert_response_first, make_interaction, slow
from tests.unit.test_catch_phase3 import LOCALE, allow, detail, edits_to, make_view, sent_to
from tests.unit.notice_helpers import body


def owned(**over):
    base = {"id": 11, "species_id": 1, "level": 9, "nickname": None, "shiny": False, "special": False,
            "favorite": False, "locked": False}
    base.update(over)
    return base


class FakeDb:
    def __init__(self, *, player=None, creature=None, deleted=11, updated=1):
        self.player, self.creature, self.deleted, self.updated = player, creature, deleted, updated
        self.queries, self.executed = [], []

    async def fetchrow(self, sql, *args):
        self.queries.append((sql, args))
        return self.player if "catch_players" in sql else self.creature

    async def fetchval(self, sql, *args):
        self.queries.append((sql, args))
        return self.deleted if sql.startswith("DELETE") else self.updated

    async def execute(self, sql, *args):
        self.executed.append((sql, args))


def patch_db(monkeypatch, db):
    @asynccontextmanager
    async def fake(conn=None):
        yield db

    monkeypatch.setattr(rel.catch_db, "transaction", fake)


def run(coro):
    return asyncio.run(coro)


def test_release_locks_player_then_creature_scopes_sql_and_audits(monkeypatch):
    db = FakeDb(player={"buddy_id": 11}, creature=owned())
    patch_db(monkeypatch, db)
    res = run(rel.release_creature(11, 7, None, guild_id=5))
    assert res.ok and res.was_buddy and res.coins == rel.RELEASE_COINS
    first, second = db.queries[0][0], db.queries[1][0]
    assert "catch_players" in first and "FOR UPDATE" in first
    assert "catch_owned" in second and "FOR UPDATE" in second
    for sql, _ in db.queries:
        if "catch_owned" in sql:
            assert "user_id = $2" in sql and "clone_key = $3" in sql
    audit_sql, audit_args = db.executed[0]
    assert "'release'" in audit_sql
    detail_json = json.loads(audit_args[-1])
    assert detail_json["owned_id"] == 11 and detail_json["was_buddy"] is True


def test_release_of_a_non_buddy_leaves_buddy_alone(monkeypatch):
    db = FakeDb(player={"buddy_id": 99}, creature=owned())
    patch_db(monkeypatch, db)
    res = run(rel.release_creature(11, 7, None))
    assert res.ok and not res.was_buddy
    assert "CASE WHEN buddy_id = $3" in db.queries[-1][0]


@pytest.mark.parametrize("over,reason", [({"favorite": True}, "favorite"), ({"locked": True}, "locked")])
def test_favourite_and_locked_are_refused_without_deleting(monkeypatch, over, reason):
    db = FakeDb(player={"buddy_id": None}, creature=owned(**over))
    patch_db(monkeypatch, db)
    res = run(rel.release_creature(11, 7, None))
    assert not res.ok and res.reason == reason
    assert not any(sql.startswith("DELETE") for sql, _ in db.queries) and not db.executed


def test_missing_creature_or_player_is_refused(monkeypatch):
    patch_db(monkeypatch, FakeDb(player={"buddy_id": None}, creature=None))
    assert run(rel.release_creature(11, 7, None)).reason == "not_found"
    db = FakeDb(player=None, creature=owned())
    patch_db(monkeypatch, db)
    assert run(rel.release_creature(11, 7, None)).reason == "no_player"
    assert not any(sql.startswith("DELETE") for sql, _ in db.queries)


def test_lost_delete_race_releases_nothing(monkeypatch):
    db = FakeDb(player={"buddy_id": None}, creature=owned(), deleted=None)
    patch_db(monkeypatch, db)
    assert run(rel.release_creature(11, 7, None)).reason == "not_found"
    assert not db.executed


def test_missing_player_row_on_update_raises_so_the_delete_rolls_back(monkeypatch):
    patch_db(monkeypatch, FakeDb(player={"buddy_id": None}, creature=owned(), updated=None))
    with pytest.raises(RuntimeError):
        run(rel.release_creature(11, 7, None))


def test_release_pays_no_coins_by_default():
    assert rel.RELEASE_COINS == 0


# ---------------------------------------------------------------- screens
def test_confirm_embed_warns_about_buddy_and_escapes_markdown():
    plain = views.release_confirm_embed(detail(nickname="*x*"))
    assert "\\*x\\*" in plain.title and text("release.buddy_warning") not in plain.description
    buddy = views.release_confirm_embed(detail(is_buddy=True))
    assert text("release.buddy_warning") in buddy.description
    assert emoji.mark("ui", "release") in buddy.title


def test_release_locale_keys_exist():
    for key in ("btn_release",):
        assert f"catch.creature.{key}" in LOCALE
    assert sum(1 for k in LOCALE if k.startswith("catch.release.")) >= 12


def test_release_button_is_danger_and_present():
    view = make_view(None)
    btn = next(c for c in view.children if c.label == LOCALE["catch.creature.btn_release"])
    assert btn.row == 1 and btn.style.name == "danger"


def test_release_button_responds_first_gates_then_shows_confirm_and_deletes_nothing(monkeypatch):
    rec = Recorder()
    allow(monkeypatch, rec)
    called = []
    monkeypatch.setattr(views, "release_creature", lambda *a, **k: called.append(1))
    view = make_view(rec)
    inter = make_interaction(rec)
    edits = edits_to(inter)
    asyncio.run(view._release(inter))
    assert_response_first(rec)
    assert rec.calls.index("gate") > 0 and not called
    assert isinstance(edits[0]["view"], views.ReleaseConfirmView)


@pytest.mark.parametrize("over,key", [({"favorite": True}, "favorite"), ({"locked": True}, "locked")])
def test_release_button_refuses_favourite_and_locked_before_confirm(monkeypatch, over, key):
    rec = Recorder()
    allow(monkeypatch, rec)
    view = make_view(rec, detail=detail(**over))
    inter = make_interaction(rec)
    sent, edits = sent_to(inter), edits_to(inter)
    asyncio.run(view._release(inter))
    assert not edits
    assert sent and body(sent[0]) == text(f"release.refused_{key}", name="Cindrop")


def confirm_view(rec):
    return views.ReleaseConfirmView(7, None, make_view(rec))


def test_confirm_releases_once_tells_the_player_and_returns_to_the_list(monkeypatch):
    rec = Recorder()
    allow(monkeypatch, rec)
    monkeypatch.setattr(views, "release_creature", slow(rec, "release", rel.ReleaseResult(True, None, "Cindrop", True, 0)))
    cv = make_view(rec)
    confirm = views.ReleaseConfirmView(7, None, cv)
    inter = make_interaction(rec)
    sent = sent_to(inter)
    asyncio.run(confirm._confirm(inter))
    assert_response_first(rec)
    assert rec.calls.index("gate") < rec.calls.index("release")
    assert "released" in sent[0][0][0] and text("release.buddy_cleared") in sent[0][0][0]
    assert cv.back_calls == [1]


@pytest.mark.parametrize("reason", ["favorite", "locked"])
def test_confirm_refusal_redraws_the_creature_with_the_reason(monkeypatch, reason):
    rec = Recorder()
    allow(monkeypatch, rec)
    monkeypatch.setattr(views, "release_creature", slow(rec, "release", rel.ReleaseResult(False, reason, "Cindrop")))
    monkeypatch.setattr(views, "load_creature", slow(rec, "load", detail()))
    monkeypatch.setattr(views, "preview", slow(rec, "preview", None))
    confirm = confirm_view(rec)
    inter = make_interaction(rec)
    edits = edits_to(inter)
    sent_to(inter)
    asyncio.run(confirm._confirm(inter))
    assert edits and "not released" in edits[0]["content"]


def test_confirm_gone_creature_says_nothing_released_and_goes_back(monkeypatch):
    rec = Recorder()
    allow(monkeypatch, rec)
    monkeypatch.setattr(views, "release_creature", slow(rec, "release", rel.ReleaseResult(False, "not_found")))
    cv = make_view(rec)
    confirm = views.ReleaseConfirmView(7, None, cv)
    inter = make_interaction(rec)
    sent = sent_to(inter)
    asyncio.run(confirm._confirm(inter))
    assert body(sent[0]) == text("release.gone") and cv.back_calls == [1]


def test_confirm_refuses_without_releasing_when_the_game_is_off(monkeypatch):
    rec = Recorder()
    allow(monkeypatch, rec, allowed=False)
    called = []
    monkeypatch.setattr(views, "release_creature", lambda *a, **k: called.append(1))
    inter = make_interaction(rec)
    sent_to(inter)
    asyncio.run(confirm_view(rec)._confirm(inter))
    assert not called


def test_confirm_error_says_nothing_was_released_and_frees_the_screen(monkeypatch):
    rec = Recorder()
    allow(monkeypatch, rec)

    async def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(views, "release_creature", boom)
    confirm = confirm_view(rec)
    inter = make_interaction(rec)
    sent = sent_to(inter)
    asyncio.run(confirm._confirm(inter))
    assert body(sent[0]) == text("release.error") and confirm._busy is False


def test_double_confirm_releases_only_once(monkeypatch):
    rec = Recorder()
    allow(monkeypatch, rec)
    calls = []

    async def slow_release(*a, **k):
        calls.append(1)
        await asyncio.sleep(0.05)
        return rel.ReleaseResult(True, None, "Cindrop")

    monkeypatch.setattr(views, "release_creature", slow_release)
    confirm = confirm_view(rec)

    async def both():
        a, b = make_interaction(rec), make_interaction(rec)
        sent_to(a), sent_to(b)
        await asyncio.gather(confirm._confirm(a), confirm._confirm(b))

    asyncio.run(both())
    assert calls == [1]


def test_cancel_keeps_the_creature_and_redraws(monkeypatch):
    rec = Recorder()
    called = []
    monkeypatch.setattr(views, "release_creature", lambda *a, **k: called.append(1))
    monkeypatch.setattr(views, "load_creature", slow(rec, "load", detail()))
    monkeypatch.setattr(views, "preview", slow(rec, "preview", None))
    confirm = confirm_view(rec)
    inter = make_interaction(rec)
    edits = edits_to(inter)
    asyncio.run(confirm._cancel(inter))
    assert_response_first(rec)
    assert not called and edits[0]["content"] == text("release.cancelled")


def test_confirm_view_is_owner_only():
    sent = []

    async def send_message(*a, **k):
        sent.append(k)

    other = SimpleNamespace(user=SimpleNamespace(id=999), response=SimpleNamespace(send_message=send_message))
    assert asyncio.run(confirm_view(None).interaction_check(other)) is False and sent[0]["ephemeral"] is True
