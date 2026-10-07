"""While CATCH_SUPPORT_SERVER_ONLY is on, only the support server can use the game."""
import asyncio
from types import SimpleNamespace

import discord
import pytest

import config
import discord_bot.cogs.catch as catch
import discord_bot.cogs._views_join_dm as join_dm
from modules import catch_gate
from tests.unit.notice_helpers import body

SUPPORT = 5555
OTHER = 7777


@pytest.fixture(autouse=True)
def restricted(monkeypatch):
    monkeypatch.setattr(config, "CATCH_SUPPORT_SERVER_ONLY", True)
    monkeypatch.setattr(config, "DISCORD_SUPPORT_SERVER_ID", SUPPORT)
    catch_gate.invalidate()


def test_only_the_support_server_passes_the_guild_check():
    assert catch_gate.guild_allowed(SUPPORT)
    assert not catch_gate.guild_allowed(OTHER)
    assert not catch_gate.guild_allowed(None)   # DMs
    assert not catch_gate.guild_allowed(0)


def test_it_fails_closed_when_the_support_server_id_is_not_configured(monkeypatch):
    monkeypatch.setattr(config, "DISCORD_SUPPORT_SERVER_ID", 0)
    assert not catch_gate.guild_allowed(None) and not catch_gate.guild_allowed(0) and not catch_gate.guild_allowed(SUPPORT)


def test_switching_the_restriction_off_opens_every_server(monkeypatch):
    monkeypatch.setattr(config, "CATCH_SUPPORT_SERVER_ONLY", False)
    assert catch_gate.guild_allowed(OTHER) and catch_gate.guild_allowed(None)


def test_config_switch_parses_the_env_var():
    import importlib
    import os
    original = os.environ.get("CATCH_SUPPORT_SERVER_ONLY")
    try:
        for raw, expected in (("0", False), ("false", False), ("off", False), ("1", True), ("", True), ("yes", True)):
            os.environ["CATCH_SUPPORT_SERVER_ONLY"] = raw
            assert importlib.reload(config).CATCH_SUPPORT_SERVER_ONLY is expected, raw
        os.environ.pop("CATCH_SUPPORT_SERVER_ONLY")
        assert importlib.reload(config).CATCH_SUPPORT_SERVER_ONLY is True   # default is restricted
    finally:
        if original is None:
            os.environ.pop("CATCH_SUPPORT_SERVER_ONLY", None)
        else:
            os.environ["CATCH_SUPPORT_SERVER_ONLY"] = original
        importlib.reload(config)


@pytest.mark.parametrize("action", sorted(catch_gate.ACTION_FEATURE))
def test_every_action_is_refused_outside_the_support_server_before_any_database_read(monkeypatch, action):
    async def no_db(*args, **kwargs):
        raise AssertionError("flags must not be read for a refused server")

    monkeypatch.setattr(catch_gate, "_flags", no_db)
    for guild in (OTHER, None):
        gate = asyncio.run(catch_gate.check_player_allowed(1, guild, action))
        assert gate.allowed is False and gate.reason == catch_gate.SUPPORT_ONLY_REASON


def test_the_support_server_still_goes_through_the_normal_flag_checks(monkeypatch):
    async def flags(guild_id, clone_id, conn=None):
        assert guild_id == SUPPORT
        return {"game": (False, "maintenance")}

    monkeypatch.setattr(catch_gate, "_flags", flags)
    gate = asyncio.run(catch_gate.check_player_allowed(1, SUPPORT, "view"))
    assert (gate.allowed, gate.reason) == (False, "maintenance")

    async def open_flags(guild_id, clone_id, conn=None):
        return {}

    monkeypatch.setattr(catch_gate, "_flags", open_flags)
    assert asyncio.run(catch_gate.check_player_allowed(1, SUPPORT, "view")).allowed is True


def test_refusal_message_is_readable():
    msg = catch.text("unavailable", reason=catch_gate.SUPPORT_ONLY_REASON)
    assert msg == "Catch is currently unavailable: the game is only open in the support server for now."


# ---- join DM button ------------------------------------------------------------------------

def test_join_dm_catch_button_is_dimmed_outside_the_support_server_and_live_inside_it():
    other = join_dm._FeatureToggleButton("catch", OTHER).item
    assert other.disabled is True and other.style == discord.ButtonStyle.secondary and "support server only" in other.label
    support = join_dm._FeatureToggleButton("catch", SUPPORT).item
    assert support.disabled is False and support.label.startswith("Turn on")


def test_other_join_dm_buttons_are_never_dimmed_by_this():
    for key in join_dm.FEATURE_TOGGLES:
        if key != "catch":
            assert join_dm._FeatureToggleButton(key, OTHER).item.disabled is False, key


def test_a_stale_enabled_join_dm_button_is_refused_by_the_handler(monkeypatch):
    saved = []

    async def no_save(*args, **kwargs):
        saved.append(args)

    import modules.catch_setup as setup_mod
    monkeypatch.setattr(setup_mod, "save_setup", no_save)
    guild = SimpleNamespace(id=OTHER)
    ok, message = asyncio.run(join_dm._enable_catch(SimpleNamespace(user=SimpleNamespace(id=1)), guild, None))
    assert ok is False and "support server" in message and saved == []


def test_a_rebuild_after_another_click_keeps_the_dimmed_button_dimmed():
    import inspect
    src = inspect.getsource(join_dm._FeatureToggleButton.callback)
    assert "bool(component.disabled)" in src


# ---- owner setup panel ---------------------------------------------------------------------

def test_setup_turn_on_is_dimmed_outside_the_support_server_but_turn_off_never_is():
    off = catch.CatchSetupView(catch.CatchSetup(), guild_id=OTHER)
    toggle = next(c for c in off.children if getattr(c, "custom_id", "") == "catch:setup:toggle")
    assert toggle.disabled is True and toggle.label == catch.text("setup.support_only")
    on = catch.CatchSetupView(catch.CatchSetup(enabled=True, spawn_channel_ids=(1,)), guild_id=OTHER)
    toggle = next(c for c in on.children if getattr(c, "custom_id", "") == "catch:setup:toggle")
    assert toggle.disabled is False
    home = catch.CatchSetupView(catch.CatchSetup(), guild_id=SUPPORT)
    toggle = next(c for c in home.children if getattr(c, "custom_id", "") == "catch:setup:toggle")
    assert toggle.disabled is False and toggle.label == catch.text("setup.turn_on")


def test_setup_toggle_callback_refuses_turning_on_outside_the_support_server():
    view = catch.CatchSetupView(catch.CatchSetup(spawn_channel_ids=(1,)), guild_id=OTHER)
    sent = []

    async def send_message(content=None, **kwargs):
        sent.append(content)

    inter = SimpleNamespace(response=SimpleNamespace(send_message=send_message))
    asyncio.run(view._toggle(inter))
    assert sent == [catch.text("setup.support_only_detail")] and view.setup.enabled is False


# ---- real entry points refuse ---------------------------------------------------------------

def test_spawn_trigger_and_catch_command_do_nothing_in_other_servers(monkeypatch):
    async def boom(*args, **kwargs):
        raise AssertionError("must not load setup for a refused server")

    monkeypatch.setattr(catch, "load_setup", boom)
    cog = catch.CatchCog.__new__(catch.CatchCog)
    cog.bot = SimpleNamespace(clone_id=None)
    msg = SimpleNamespace(guild=SimpleNamespace(id=OTHER), author=SimpleNamespace(bot=False, id=1), channel=SimpleNamespace(id=2))
    asyncio.run(catch.CatchCog.on_message(cog, msg))   # returns quietly


def test_hub_command_refuses_outside_the_support_server():
    sent = []

    async def defer(**kwargs):
        pass

    async def send(*args, **kwargs):
        sent.append((args, kwargs))

    inter = SimpleNamespace(user=SimpleNamespace(id=1), guild_id=OTHER, client=SimpleNamespace(clone_id=None),
                            response=SimpleNamespace(defer=defer), followup=SimpleNamespace(send=send))
    cog = catch.CatchCog.__new__(catch.CatchCog)
    asyncio.run(catch.CatchCog.catch.callback(cog, inter))
    assert body(sent[0]) == catch.text("unavailable", reason=catch_gate.SUPPORT_ONLY_REASON) and "view" not in sent[0][1]
