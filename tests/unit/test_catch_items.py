"""Inventory, starter kit and daily reward: pure rules, SQL contracts, screens, claim-flow hooks."""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import discord
import pytest

import discord_bot.cogs._views_catch_items as views
import discord_bot.cogs.catch as catch
from modules import catch_items as ci
from tests.unit.test_catch_interaction_timing import Recorder, assert_response_first, make_interaction, slow
from tests.unit.notice_helpers import shown

NOW = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)


class FakeDb:
    def __init__(self, *, fetchval=None, fetchrow=None, rows=()):
        self._fetchval, self._fetchrow, self.rows = list(fetchval or []), list(fetchrow or []), list(rows)
        self.executed, self.queries = [], []

    async def fetchval(self, sql, *args):
        self.queries.append((sql, args))
        return self._fetchval.pop(0) if self._fetchval else None

    async def fetchrow(self, sql, *args):
        self.queries.append((sql, args))
        return self._fetchrow.pop(0) if self._fetchrow else None

    async def fetch(self, sql, *args):
        self.queries.append((sql, args))
        return self.rows

    async def execute(self, sql, *args):
        self.executed.append((sql, args))


def patch_db(monkeypatch, db):
    @asynccontextmanager
    async def fake(conn=None):
        yield db

    monkeypatch.setattr(ci.catch_db, "connection", fake)
    monkeypatch.setattr(ci.catch_db, "transaction", fake)


# ---- pure rules ---------------------------------------------------------------------------------

def test_daily_reward_scales_then_caps_and_adds_milestones():
    assert ci.daily_reward(1).coins == 100 and ci.daily_reward(1).items == {"capsule_basic": 5}
    assert ci.daily_reward(7).coins == 250 and ci.daily_reward(7).items["capsule_sturdy"] == 2
    assert ci.daily_reward(40).coins == 250  # streak bonus caps
    assert ci.daily_reward(30).items["capsule_prime"] == 1
    assert ci.daily_reward(0).coins == 100  # never below day 1
    assert all(key in ci.KNOWN_ITEMS for streak in range(1, 61) for key in ci.daily_reward(streak).items)


def test_streak_continues_within_48h_and_resets_after():
    assert ci.next_streak(3, NOW - timedelta(hours=24), NOW) == 4
    assert ci.next_streak(3, NOW - timedelta(hours=48), NOW) == 4
    assert ci.next_streak(3, NOW - timedelta(hours=49), NOW) == 1
    assert ci.next_streak(0, None, NOW) == 1


def test_sorted_inventory_hides_empty_and_keeps_game_order():
    balls, berries = ci.sorted_inventory({"capsule_prime": 1, "capsule_basic": 4, "goldberry": 2, "honeyberry": 0, "mystery": 9})
    assert balls == [("capsule_basic", 4), ("capsule_prime", 1)] and berries == [("goldberry", 2)]


# ---- SQL contracts ------------------------------------------------------------------------------

def test_grant_items_upserts_and_rejects_bad_input(monkeypatch):
    db = FakeDb()
    patch_db(monkeypatch, db)
    asyncio.run(ci.grant_items(5, 2, {"capsule_basic": 3, "honeyberry": 1}))
    assert len(db.executed) == 2 and all("ON CONFLICT (user_id, clone_key, item_key)" in sql for sql, _ in db.executed)
    assert db.executed[0][1] == (5, 2, "capsule_basic", 3)
    with pytest.raises(KeyError):
        asyncio.run(ci.grant_items(5, None, {"free_money": 1}))
    with pytest.raises(ValueError):
        asyncio.run(ci.grant_items(5, None, {"capsule_basic": 0}))
    assert len(db.executed) == 2  # nothing written for rejected grants


def test_load_inventory_scopes_to_user_and_clone(monkeypatch):
    db = FakeDb(rows=[{"item_key": "capsule_basic", "quantity": 7}])
    patch_db(monkeypatch, db)
    assert asyncio.run(ci.load_inventory(5, None)) == {"capsule_basic": 7}
    assert db.queries[-1][1] == (5, -1)


def test_starter_kit_grants_only_on_first_call(monkeypatch):
    db = FakeDb(fetchval=[1])
    patch_db(monkeypatch, db)
    assert asyncio.run(ci.ensure_starter_kit(5, None)) is True
    assert any(args[2] == "capsule_basic" and args[3] == 10 for _, args in db.executed)
    assert any("'starter_kit'" in sql for sql, _ in db.executed)
    again = FakeDb(fetchval=[None])
    patch_db(monkeypatch, again)
    assert asyncio.run(ci.ensure_starter_kit(5, None)) is False
    assert again.executed == []  # flag already set: no items, no audit row


def player_row(streak=0, last=None, now=NOW):
    return {"daily_streak": streak, "last_daily_at": last, "now": now}


def test_claim_daily_first_time_pays_day_one(monkeypatch):
    db = FakeDb(fetchrow=[player_row()])
    patch_db(monkeypatch, db)
    result = asyncio.run(ci.claim_daily(5, None))
    assert result.claimed and result.streak == 1 and result.reward.coins == 100
    sqls = [sql for sql, _ in db.executed]
    assert any("UPDATE catch_players SET daily_streak" in s for s in sqls)
    assert any("INSERT INTO catch_inventory" in s for s in sqls)
    assert any("'daily'" in s for s in sqls)
    assert any("FOR UPDATE" in sql for sql, _ in db.queries)  # player row locked: no double payout


def test_claim_daily_too_soon_pays_nothing(monkeypatch):
    last = NOW - timedelta(hours=3)
    db = FakeDb(fetchrow=[player_row(streak=4, last=last)])
    patch_db(monkeypatch, db)
    result = asyncio.run(ci.claim_daily(5, None))
    assert not result.claimed and result.streak == 4 and result.reward is None
    assert result.ready_at == last + timedelta(seconds=ci.DAILY_COOLDOWN_SECONDS)
    assert not any("UPDATE catch_players" in sql or "catch_inventory" in sql for sql, _ in db.executed)


def test_claim_daily_continues_and_resets_streak(monkeypatch):
    db = FakeDb(fetchrow=[player_row(streak=6, last=NOW - timedelta(hours=30))])
    patch_db(monkeypatch, db)
    result = asyncio.run(ci.claim_daily(5, None))
    assert result.streak == 7 and result.reward.items["capsule_sturdy"] == 2
    db = FakeDb(fetchrow=[player_row(streak=6, last=NOW - timedelta(days=5))])
    patch_db(monkeypatch, db)
    assert asyncio.run(ci.claim_daily(5, None)).streak == 1


# ---- screens ------------------------------------------------------------------------------------

def test_inventory_embed_lists_stacks_and_fits_limits():
    embed = views.inventory_embed({"capsule_basic": 12, "capsule_sovereign": 1, "honeyberry": 3}, ci.PlayerSummary(coins=1234, total_catches=9, daily_streak=2))
    names = {f.name: f.value for f in embed.fields}
    assert "Basic capsule** ×12" in names["Capsules"] and "Sovereign capsule" in names["Capsules"]
    assert "+10% catch chance" in names["Berries"] and names["Coins"] == "1,234"
    assert len(embed) <= 6000


def test_empty_inventory_points_to_daily():
    embed = views.inventory_embed({}, ci.PlayerSummary())
    assert all("Daily" in f.value for f in embed.fields[:2])


def test_daily_embeds_cover_claimed_and_waiting():
    ready = NOW + timedelta(hours=20)
    claimed = views.daily_embed(ci.DailyResult(True, 3, ready, ci.daily_reward(3)))
    assert "Day 3" in claimed.description and any("Basic capsule ×5" in f.value for f in claimed.fields)
    waiting = views.daily_embed(ci.DailyResult(False, 3, ready))
    assert f"<t:{int(ready.timestamp())}:R>" in waiting.description


def gate_ok(rec):
    return slow(rec, "gate", SimpleNamespace(allowed=True, reason=None))


def test_open_inventory_responds_first_and_grants_starter_before_reading(monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(views, "check_player_allowed", gate_ok(rec))
    monkeypatch.setattr(views, "ensure_starter_kit", slow(rec, "starter", True))
    monkeypatch.setattr(views, "load_inventory", slow(rec, "inv", {"capsule_basic": 10}))
    monkeypatch.setattr(views, "load_player", slow(rec, "player", ci.PlayerSummary()))
    asyncio.run(views.open_inventory(make_interaction(rec)))
    assert_response_first(rec)
    assert rec.calls.index("gate") < rec.calls.index("starter") < rec.calls.index("inv")


def test_open_daily_responds_first_and_claims(monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(views, "check_player_allowed", gate_ok(rec))
    monkeypatch.setattr(views, "ensure_starter_kit", slow(rec, "starter", False))
    monkeypatch.setattr(views, "claim_daily", slow(rec, "claim", ci.DailyResult(True, 1, NOW, ci.daily_reward(1))))
    asyncio.run(views.open_daily(make_interaction(rec)))
    assert_response_first(rec)
    assert rec.calls.index("gate") < rec.calls.index("claim") and rec.calls[-1] == "followup"


def test_blocked_gate_grants_and_pays_nothing(monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(views, "check_player_allowed", slow(rec, "gate", SimpleNamespace(allowed=False, reason="game_disabled")))
    for name in ("ensure_starter_kit", "claim_daily", "load_inventory"):
        monkeypatch.setattr(views, name, slow(rec, name))
    asyncio.run(views.open_daily(make_interaction(rec)))
    asyncio.run(views.open_inventory(make_interaction(rec)))
    assert not {"ensure_starter_kit", "claim_daily", "load_inventory"} & set(rec.calls)


def test_daily_failure_shows_friendly_error(monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(views, "check_player_allowed", gate_ok(rec))

    async def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(views, "ensure_starter_kit", boom)
    asyncio.run(views.open_daily(make_interaction(rec)))
    assert rec.calls[-1] == "followup"


# ---- hub + claim flow ---------------------------------------------------------------------------

def test_hub_daily_and_inventory_are_real_handlers():
    pinned = {b.label: b.callback for b in catch.CatchHubView().children if isinstance(b, discord.ui.Button)}
    assert pinned["Daily"] is views.open_daily
    collect = {b.label: b.callback for b in catch.CatchHubView(category="collect").children if isinstance(b, discord.ui.Button)}
    assert collect["Inventory"] is views.open_inventory


def test_dynamic_button_routes_daily_and_inventory_after_restart(monkeypatch):
    seen = []

    async def mark(name):
        seen.append(name)

    for key in ("daily", "inventory"):
        monkeypatch.setitem(catch.REAL_ACTIONS, key, lambda interaction, key=key: mark(key))
        asyncio.run(catch.CatchHubDynamicButton(discord.ui.Button(custom_id=f"catch:hub:{key}"), action=key).callback(SimpleNamespace()))
    assert seen == ["daily", "inventory"]


def claim_interaction(rec):
    inter = make_interaction(rec)
    inter.message = SimpleNamespace(edit=slow(rec, "edit"))
    inter.guild_id = 1
    return inter


def test_claim_grants_starter_kit_before_throwing(monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(catch, "check_player_allowed", gate_ok(rec))
    monkeypatch.setattr(catch, "ensure_starter_kit", slow(rec, "starter", True))
    monkeypatch.setattr(catch, "record_catch", slow(rec, "record", SimpleNamespace(claimed=False)))
    asyncio.run(catch.SpawnClaimView(1)._claim(claim_interaction(rec)))
    assert_response_first(rec)
    assert rec.calls.index("starter") < rec.calls.index("record")


def test_claim_without_items_gets_helpful_message(monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(catch, "check_player_allowed", gate_ok(rec))
    monkeypatch.setattr(catch, "ensure_starter_kit", slow(rec, "starter", False))
    sent = []

    async def no_ball(**kwargs):
        raise ValueError("capsule_sturdy is not available")

    monkeypatch.setattr(catch, "record_catch", no_ball)
    inter = claim_interaction(rec)

    async def followup_send(*args, **kwargs):
        sent.append(shown(args, kwargs))

    inter.followup = SimpleNamespace(send=followup_send)
    asyncio.run(catch.SpawnClaimView(1)._claim(inter))
    assert sent == [catch.text("claim.no_items")]


def _success_embed(monkeypatch, shiny):
    rec = Recorder()
    monkeypatch.setattr(catch, "check_player_allowed", gate_ok(rec))
    monkeypatch.setattr(catch, "ensure_starter_kit", slow(rec, "starter", False))
    result = SimpleNamespace(
        claimed=True, species_id=1, level=5, shiny=shiny, new_species=False, owned_id=5, replay=False,
    )
    monkeypatch.setattr(catch, "record_catch", slow(rec, "record", result))
    monkeypatch.setattr(catch, "grant_buddy_catch_xp", slow(rec, "buddy_xp", None))
    sent = []

    async def followup_send(*args, **kwargs):
        sent.append(kwargs.get("embed"))

    inter = claim_interaction(rec)
    inter.followup = SimpleNamespace(send=followup_send)
    asyncio.run(catch.SpawnClaimView(1)._claim(inter))
    return sent[-1]


def test_claim_success_shows_species_name(monkeypatch):
    species_name = catch.all_species()[1]["name"]
    embed = _success_embed(monkeypatch, shiny=False)
    assert f"**{species_name}**" in embed.description
    assert "Creature" not in embed.description


def test_claim_success_marks_shiny_species(monkeypatch):
    species_name = catch.all_species()[1]["name"]
    embed = _success_embed(monkeypatch, shiny=True)
    assert f"**Shiny {species_name}**" in embed.description
