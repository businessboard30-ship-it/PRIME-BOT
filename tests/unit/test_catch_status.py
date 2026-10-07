"""Status: scoped read-only SQL, zero-state, embed, timing, gate, and hub routing."""
import asyncio
import re
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import discord

import discord_bot.cogs._views_catch_status as views
import discord_bot.cogs.catch as catch
from modules import catch_status as status
from modules.catch_items import DAILY_COOLDOWN_SECONDS
from tests.unit.test_catch_interaction_timing import Recorder, assert_response_first, make_interaction, slow
from tests.unit.notice_helpers import body

NOW = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)


class FakeDb:
    def __init__(self, *, player=None, owned=None, dex=()):
        self.player, self.owned, self.dex, self.queries = player, owned, list(dex), []

    async def fetchrow(self, sql, *args):
        self.queries.append((sql, args))
        return self.player if "catch_players" in sql else self.owned

    async def fetch(self, sql, *args):
        self.queries.append((sql, args))
        return self.dex


def patch_db(monkeypatch, db):
    @asynccontextmanager
    async def fake(conn=None):
        yield db

    monkeypatch.setattr(status.catch_db, "connection", fake)
    import modules.catch_collection as collection
    monkeypatch.setattr(collection.catch_db, "connection", fake)


def test_reads_are_scoped_by_user_and_clone_and_never_write(monkeypatch):
    db = FakeDb(player={"coins": 5, "total_catches": 2, "catch_streak": 1, "best_streak": 3, "daily_streak": 4,
                        "last_daily_at": None},
                owned={"owned": 7, "shinies": 2, "favourites": 1})
    patch_db(monkeypatch, db)
    result = asyncio.run(status.load_status(7, 3))
    assert (result.coins, result.total_catches, result.best_streak, result.daily_streak) == (5, 2, 3, 4)
    assert (result.owned, result.shinies, result.favourites) == (7, 2, 1)
    assert result.dex_total > 0 and result.dex_caught == 0
    assert len(db.queries) == 3
    for sql, args in db.queries:
        assert "user_id=$1" in sql and "clone_key=$2" in sql
        assert args == (7, 3)
        assert sql.lstrip().upper().startswith("SELECT")


def test_player_with_no_row_reads_as_zeros_and_can_claim_daily(monkeypatch):
    patch_db(monkeypatch, FakeDb())
    result = asyncio.run(status.load_status(7, None))
    assert result.coins == 0 and result.owned == 0 and result.daily_ready_at is None


def test_daily_ready_at():
    assert status.daily_ready_at(None, NOW) is None
    assert status.daily_ready_at(NOW - timedelta(seconds=DAILY_COOLDOWN_SECONDS), NOW) is None
    soon = NOW - timedelta(seconds=DAILY_COOLDOWN_SECONDS - 60)
    assert status.daily_ready_at(soon, NOW) == NOW + timedelta(seconds=60)


def test_embed_shows_numbers_and_daily_state():
    ready = views.status_embed(status.PlayerStatus(coins=1234, owned=5, shinies=1, dex_caught=3, dex_seen=4, dex_total=40))
    text_ = " ".join(f"{f.name} {f.value}" for f in ready.fields)
    assert "1,234" in text_ and catch.text("status.daily_ready") in text_ and "40" in text_ and len(ready) <= 6000
    waiting = views.status_embed(status.PlayerStatus(daily_ready_at=NOW))
    assert f"<t:{int(NOW.timestamp())}:R>" in " ".join(f.value for f in waiting.fields)


def sent_to(inter):
    sent = []

    async def followup_send(*args, **kwargs):
        sent.append((args, kwargs))

    inter.followup = SimpleNamespace(send=followup_send)
    return sent


def test_open_status_responds_before_gate_and_db_and_is_ephemeral(monkeypatch):
    r = Recorder()
    monkeypatch.setattr(views, "check_player_allowed", slow(r, "gate", SimpleNamespace(allowed=True, reason=None)))
    monkeypatch.setattr(views, "load_status", slow(r, "load", status.PlayerStatus()))
    inter = make_interaction(r)
    sent = sent_to(inter)
    asyncio.run(views.open_status(inter))
    assert_response_first(r)
    assert r.calls.index("gate") < r.calls.index("load")
    assert sent[0][1]["ephemeral"] is True and isinstance(sent[0][1]["embed"], discord.Embed)


def test_open_status_refuses_when_game_is_off(monkeypatch):
    r = Recorder()
    monkeypatch.setattr(views, "check_player_allowed", slow(r, "gate", SimpleNamespace(allowed=False, reason="game_disabled")))
    monkeypatch.setattr(views, "load_status", slow(r, "load", status.PlayerStatus()))
    inter = make_interaction(r)
    sent = sent_to(inter)
    asyncio.run(views.open_status(inter))
    assert body(sent[0]) == catch.text("catch.unavailable", reason="game_disabled")
    assert "load" not in r.calls


def test_load_failure_is_reported_politely(monkeypatch):
    r = Recorder()
    monkeypatch.setattr(views, "check_player_allowed", slow(r, "gate", SimpleNamespace(allowed=True, reason=None)))

    async def boom(*args, **kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(views, "load_status", boom)
    inter = make_interaction(r)
    sent = sent_to(inter)
    asyncio.run(views.open_status(inter))
    assert body(sent[0]) == catch.text("status.error")


def test_status_button_opens_the_real_screen_live_and_after_restart(monkeypatch):
    assert catch.REAL_ACTIONS["status"] is views.open_status
    buttons = {b.label: b for b in catch.CatchHubView(category="info").children if isinstance(b, discord.ui.Button)}
    assert buttons["Status"].callback is views.open_status
    assert buttons["Status"].custom_id == "catch:hub:info:status"
    seen = []

    async def mark(interaction):
        seen.append("status")

    monkeypatch.setitem(catch.REAL_ACTIONS, "status", mark)
    pattern = catch.CatchHubDynamicButton.__discord_ui_compiled_template__.pattern
    match = re.fullmatch(pattern, "catch:hub:info:status")
    item = asyncio.run(catch.CatchHubDynamicButton.from_custom_id(None, discord.ui.Button(custom_id="catch:hub:info:status"), match))
    asyncio.run(item.callback(SimpleNamespace()))
    assert seen == ["status"]
