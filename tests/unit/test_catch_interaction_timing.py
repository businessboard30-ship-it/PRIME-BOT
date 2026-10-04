"""Interaction-time checks (plan rule B.6): every catch callback must acknowledge
the interaction BEFORE any DB / permission / network work. A slow dependency is
simulated so a callback that works first would fail the ordering assertion."""
import asyncio
from types import SimpleNamespace

import discord_bot.cogs.catch as catch

SLOW = 0.05


class Recorder:
    def __init__(self):
        self.calls = []

    def add(self, name):
        self.calls.append(name)


def make_interaction(rec):
    async def defer(**kwargs):
        rec.add("response")

    async def send_message(*args, **kwargs):
        rec.add("response")

    async def edit_message(*args, **kwargs):
        rec.add("response")

    async def followup_send(*args, **kwargs):
        rec.add("followup")
        return SimpleNamespace(id=99)

    async def edit_original_response(*args, **kwargs):
        rec.add("edit_original")

    guild = SimpleNamespace(id=1, owner_id=7, channels=[])
    return SimpleNamespace(
        response=SimpleNamespace(defer=defer, send_message=send_message, edit_message=edit_message),
        followup=SimpleNamespace(send=followup_send),
        edit_original_response=edit_original_response,
        guild=guild, guild_id=1, channel_id=2,
        user=SimpleNamespace(id=7), client=SimpleNamespace(clone_id=None), data={},
    )


def slow(rec, name, result=None):
    async def fn(*args, **kwargs):
        rec.add(name)
        await asyncio.sleep(SLOW)
        return result
    return fn


def assert_response_first(rec):
    assert rec.calls, "callback did nothing"
    assert rec.calls[0] == "response", f"slow work ran before the response: {rec.calls}"


def test_catch_command_responds_before_gate(monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(catch, "check_player_allowed", slow(rec, "gate", SimpleNamespace(allowed=True, reason=None)))
    cog = catch.CatchCog.__new__(catch.CatchCog)
    asyncio.run(catch.CatchCog.catch.callback(cog, make_interaction(rec)))
    assert_response_first(rec)
    assert "gate" in rec.calls


def test_hub_setup_responds_before_permission_and_db(monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(catch, "user_can_manage_guild", slow(rec, "perm", True))
    monkeypatch.setattr(catch, "load_setup", slow(rec, "db", catch.CatchSetup()))
    asyncio.run(catch.CatchHubView()._setup(make_interaction(rec)))
    assert_response_first(rec)
    assert rec.calls.index("perm") < rec.calls.index("db")


def test_hub_setup_denies_without_manage_server(monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(catch, "user_can_manage_guild", slow(rec, "perm", False))
    monkeypatch.setattr(catch, "load_setup", slow(rec, "db", catch.CatchSetup()))
    asyncio.run(catch.CatchHubView()._setup(make_interaction(rec)))
    assert "db" not in rec.calls, "setup must not load for someone without Manage Server"


def test_encounter_responds_before_gate_and_db(monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(catch, "check_player_allowed", slow(rec, "gate", SimpleNamespace(allowed=True, reason=None)))
    monkeypatch.setattr(catch, "create_player_encounter", slow(rec, "db", 5))
    monkeypatch.setattr(catch, "attach_spawn_message", slow(rec, "attach"))
    monkeypatch.setattr(catch, "roll_spawn", lambda *a, **k: SimpleNamespace())
    monkeypatch.setattr(catch, "all_species", lambda: {})
    monkeypatch.setattr(
        catch, "spawn_embed_data",
        lambda roll, expires_at: {"title": "t", "description": "d", "rarity": "r", "level": "1", "expires_at": "x"},
    )
    asyncio.run(catch.CatchHubView()._encounter(make_interaction(rec)))
    assert_response_first(rec)
    assert rec.calls.index("gate") < rec.calls.index("db")


def test_encounter_blocked_by_gate_does_no_db_work(monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(catch, "check_player_allowed", slow(rec, "gate", SimpleNamespace(allowed=False, reason="encounter_disabled")))
    monkeypatch.setattr(catch, "create_player_encounter", slow(rec, "db", 5))
    asyncio.run(catch.CatchHubView()._encounter(make_interaction(rec)))
    assert "db" not in rec.calls


def test_setup_refresh_responds_before_permission_and_saves(monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(catch, "user_can_manage_guild", slow(rec, "perm", True))
    monkeypatch.setattr(catch, "save_setup", slow(rec, "save"))
    monkeypatch.setattr(catch, "set_feature_flag", slow(rec, "flag"))
    view = catch.CatchSetupView(catch.CatchSetup(), guild_id=1)
    asyncio.run(view._refresh(make_interaction(rec)))
    assert_response_first(rec)
    assert rec.calls.index("perm") < rec.calls.index("save") < rec.calls.index("flag")


def test_setup_refresh_without_manage_server_saves_nothing(monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(catch, "user_can_manage_guild", slow(rec, "perm", False))
    monkeypatch.setattr(catch, "save_setup", slow(rec, "save"))
    monkeypatch.setattr(catch, "set_feature_flag", slow(rec, "flag"))
    view = catch.CatchSetupView(catch.CatchSetup(), guild_id=1)
    asyncio.run(view._refresh(make_interaction(rec)))
    assert "save" not in rec.calls and "flag" not in rec.calls


def test_setup_view_passes_clone_id_to_storage(monkeypatch):
    seen = {}

    async def fake_save(guild_id, setup, clone_id=None, **kw):
        seen["save"] = clone_id

    async def fake_flag(guild_id, clone_id, feature, enabled, **kw):
        seen["flag"] = clone_id

    async def allowed(*a, **k):
        return True

    monkeypatch.setattr(catch, "user_can_manage_guild", allowed)
    monkeypatch.setattr(catch, "save_setup", fake_save)
    monkeypatch.setattr(catch, "set_feature_flag", fake_flag)
    view = catch.CatchSetupView(catch.CatchSetup(), guild_id=1, clone_id=42)
    asyncio.run(view._refresh(make_interaction(Recorder())))
    assert seen == {"save": 42, "flag": 42}
