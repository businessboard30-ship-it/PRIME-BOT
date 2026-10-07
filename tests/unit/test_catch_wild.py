"""Wild zone: SQL scoping, screen, timing, and restart-safe hub routing."""
import asyncio
import re
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import discord

import discord_bot.cogs._views_catch_wild as views
import discord_bot.cogs.catch as catch
from modules import catch_wild as wild
from tests.unit.test_catch_interaction_timing import Recorder, assert_response_first, make_interaction, slow
from tests.unit.notice_helpers import shown

NOW = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)


class FakeDb:
    def __init__(self, *, total=0, rows=()):
        self.total, self.rows, self.queries = total, list(rows), []

    async def fetchval(self, sql, *args):
        self.queries.append((sql, args))
        return self.total

    async def fetch(self, sql, *args):
        self.queries.append((sql, args))
        return self.rows


def patch_db(monkeypatch, db):
    @asynccontextmanager
    async def fake(conn=None):
        yield db

    monkeypatch.setattr(wild.catch_db, "connection", fake)


def species_id(rarity="rare"):
    from modules.catch_species import all_species
    return next(i for i, sp in all_species().items() if sp["rarity"] == rarity)


def rec(i=1, **over):
    base = {"id": i, "channel_id": 6, "message_id": 70 + i, "species_id": species_id(), "level": 12, "shiny": False,
            "owner_user_id": None, "expires_at": NOW + timedelta(minutes=i)}
    base.update(over)
    return base


def spawn(i=1, **over):
    base = dict(id=i, channel_id=6, message_id=70 + i, name="Emberpup", rarity="rare", level=12, shiny=False,
                personal=False, expires_at=NOW + timedelta(minutes=i))
    base.update(over)
    return wild.ActiveSpawn(**base)


def test_query_lists_only_live_unclaimed_spawns_scoped_by_guild_clone_and_owner(monkeypatch):
    db = FakeDb(total=2, rows=[rec(1), rec(2, owner_user_id=7)])
    patch_db(monkeypatch, db)
    spawns, total = asyncio.run(wild.list_active_spawns(5, 7, None))
    assert total == 2 and [s.personal for s in spawns] == [False, True]
    for sql, args in db.queries:
        assert "guild_id = $1" in sql and "clone_key = $2" in sql
        assert "caught_by IS NULL" in sql and "NOT fled" in sql and "expires_at > now()" in sql
        assert "owner_user_id IS NULL OR owner_user_id = $3" in sql
        assert args[:3] == (5, -1, 7)
    assert "ORDER BY expires_at ASC" in db.queries[1][0] and db.queries[1][1][3] == wild.WILD_LIST_LIMIT


def test_query_scopes_by_clone(monkeypatch):
    db = FakeDb()
    patch_db(monkeypatch, db)
    asyncio.run(wild.list_active_spawns(5, 7, 3))
    assert all(args[1] == 3 for _, args in db.queries)


def test_unknown_species_falls_back_instead_of_crashing(monkeypatch):
    patch_db(monkeypatch, FakeDb(total=1, rows=[rec(species_id=99999)]))
    spawns, _ = asyncio.run(wild.list_active_spawns(5, 7, None))
    assert spawns[0].rarity == "common" and spawns[0].name


def test_jump_url():
    assert wild.jump_url(1, 2, 3) == "https://discord.com/channels/1/2/3"
    assert wild.jump_url(1, 2, None) == "https://discord.com/channels/1/2"
    assert wild.jump_url(1, None, 3) is None


def test_embed_lists_spawns_with_links_marks_personal_and_fits_limits():
    spawns = [spawn(i, personal=i == 2, shiny=i == 3) for i in range(1, 11)]
    embed = views.wild_embed(5, spawns, total=14)
    assert len(embed) <= 6000 and "https://discord.com/channels/5/6/71" in embed.description
    assert catch.text("wild.yours") in embed.description and "✨" in embed.description
    assert embed.footer.text == catch.text("wild.more", shown=10, total=14)
    assert views.wild_embed(5, spawns[:2], total=2).footer.text is None


def test_embed_empty_state():
    assert views.wild_embed(5, [], 0).description == catch.text("wild.empty")


def wild_interaction(rec_, guild_id=1):
    inter = make_interaction(rec_)
    inter.guild_id = guild_id
    return inter


def test_open_wild_zone_responds_before_gate_and_db(monkeypatch):
    r = Recorder()
    monkeypatch.setattr(views, "check_player_allowed", slow(r, "gate", SimpleNamespace(allowed=True, reason=None)))
    monkeypatch.setattr(views, "list_active_spawns", slow(r, "load", ([spawn()], 1)))
    asyncio.run(views.open_wild_zone(wild_interaction(r)))
    assert_response_first(r)
    assert r.calls.index("gate") < r.calls.index("load")


def test_open_wild_zone_refuses_when_game_is_off_and_outside_servers(monkeypatch):
    r, sent = Recorder(), []
    monkeypatch.setattr(views, "check_player_allowed", slow(r, "gate", SimpleNamespace(allowed=False, reason="game_disabled")))
    monkeypatch.setattr(views, "list_active_spawns", slow(r, "load", ([], 0)))
    inter = wild_interaction(r)

    async def followup_send(*args, **kwargs):
        sent.append(shown(args, kwargs))

    inter.followup = SimpleNamespace(send=followup_send)
    asyncio.run(views.open_wild_zone(inter))
    assert sent == [catch.text("catch.unavailable", reason="game_disabled")] and "load" not in r.calls
    sent.clear()
    asyncio.run(views.open_wild_zone(_dm(inter)))
    assert sent == [catch.text("encounter.server_only")]


def _dm(inter):
    inter.guild_id = None
    return inter


def test_load_failure_is_reported_politely(monkeypatch):
    r, sent = Recorder(), []
    monkeypatch.setattr(views, "check_player_allowed", slow(r, "gate", SimpleNamespace(allowed=True, reason=None)))

    async def boom(*args, **kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(views, "list_active_spawns", boom)
    inter = wild_interaction(r)

    async def followup_send(*args, **kwargs):
        sent.append(shown(args, kwargs))

    inter.followup = SimpleNamespace(send=followup_send)
    asyncio.run(views.open_wild_zone(inter))
    assert sent == [catch.text("wild.error")]


# ---- hub wiring ---------------------------------------------------------------------------------

def test_every_hub_button_custom_id_matches_the_restart_safe_template():
    pattern = re.compile(catch.CatchHubDynamicButton.__discord_ui_compiled_template__.pattern)
    for category in catch.CATEGORIES:
        view = catch.CatchHubView(category=category.key)
        for item in view.children:
            if isinstance(item, discord.ui.Button) and item.custom_id:
                assert pattern.fullmatch(item.custom_id), f"{item.custom_id} would not route after a restart"


def test_wild_zone_button_opens_the_real_screen_live_and_after_restart(monkeypatch):
    assert catch.REAL_ACTIONS["wild-zone"] is views.open_wild_zone
    buttons = {b.label: b for b in catch.CatchHubView(category="play").children if isinstance(b, discord.ui.Button)}
    assert buttons["Wild zone"].callback is views.open_wild_zone
    assert buttons["Wild zone"].custom_id == "catch:hub:play:wild-zone"
    seen = []

    async def mark(interaction):
        seen.append("wild")

    monkeypatch.setitem(catch.REAL_ACTIONS, "wild-zone", mark)
    match = re.fullmatch(catch.CatchHubDynamicButton.__discord_ui_compiled_template__.pattern, "catch:hub:play:wild-zone")
    item = asyncio.run(catch.CatchHubDynamicButton.from_custom_id(None, discord.ui.Button(custom_id="catch:hub:play:wild-zone"), match))
    asyncio.run(item.callback(SimpleNamespace()))
    assert seen == ["wild"]
