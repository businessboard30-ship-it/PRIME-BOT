"""The harness runs on exactly the configured bots: the main bot when TEST_CLONE_IDS is empty, else ONLY those clones (the real
default is {11, 12}). Each run belongs to the bot that started it, so one bot never restores or stops another bot's run."""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import config
from discord_bot.cogs import test_harness as cogmod
from discord_bot.cogs.test_harness import TestHarnessCog
from modules import ai_command_guard as guard
from modules import test_harness as th

GUILD, OWNER, STRANGER = 111, 42, 99


def run(c):
    return asyncio.run(c)


def test_real_default_is_clones_11_and_12_only():
    assert set(config.TEST_CLONE_IDS) == {11, 12}
    assert th.bot_may_run(11) and th.bot_may_run(12)
    assert not th.bot_may_run(None) and not th.bot_may_run(13) and not th.bot_may_run(1)


@pytest.mark.parametrize("clone,expect", [(None, True), (11, False), (12, False)])
def test_empty_set_means_main_bot_only(clone, expect):
    assert th.bot_may_run(clone, test_clones=set()) is expect


@pytest.mark.parametrize("clone,expect", [(11, True), (12, True), ("12", True), (None, False), (13, False), (10, False), (111, False)])
def test_listed_clones_only(clone, expect):
    assert th.bot_may_run(clone, test_clones={11, 12}) is expect


def test_junk_in_the_set_is_ignored():
    assert th.bot_may_run(11, test_clones={11, "x", None, -4, 0}) is True
    assert th.bot_may_run(None, test_clones={"x", None, 0, -1}) is True          # nothing valid listed -> main bot


@pytest.mark.parametrize("clone", [11, 12])
def test_owner_in_test_guild_on_a_listed_clone_is_allowed(clone):
    assert th.check_gates(GUILD, OWNER, clone, test_guild=GUILD, admin_ids={OWNER}, test_clones={11, 12}) is None
    assert th.check_guild(GUILD, clone, test_guild=GUILD, test_clones={11, 12}) is None


@pytest.mark.parametrize("clone", [None, 13, 1])
def test_main_bot_and_other_clones_refused_when_clones_are_listed(clone):
    assert th.check_gates(GUILD, OWNER, clone, test_guild=GUILD, admin_ids={OWNER}, test_clones={11, 12}) is not None
    assert th.check_guild(GUILD, clone, test_guild=GUILD, test_clones={11, 12}) is not None


def test_a_listed_clone_still_needs_the_right_guild_and_the_owner():
    kw = dict(test_guild=GUILD, admin_ids={OWNER}, test_clones={11, 12})
    assert th.check_gates(222, OWNER, 11, **kw) is not None
    assert th.check_gates(GUILD, STRANGER, 11, **kw) is not None
    assert th.check_gates(GUILD, None, 11, **kw) is not None
    assert th.check_gates(GUILD, OWNER, 11, test_guild=0, admin_ids={OWNER}, test_clones={11, 12}) is not None   # kill switch


def test_live_config_is_read_every_time(monkeypatch):
    monkeypatch.setattr(config, "TEST_GUILD_ID", GUILD, raising=False)
    monkeypatch.setattr(config, "DISCORD_CLONE_ADMIN_IDS", {OWNER})
    monkeypatch.setattr(config, "TEST_CLONE_IDS", {12})
    assert th.check_gates(GUILD, OWNER, 12) is None and th.check_gates(GUILD, OWNER, 11) is not None
    monkeypatch.setattr(config, "TEST_CLONE_IDS", set())
    assert th.check_gates(GUILD, OWNER, None) is None and th.check_gates(GUILD, OWNER, 12) is not None


# ── setup: only a harness bot gets the commands, the timer and the panel hook ─────────────
@pytest.mark.parametrize("clone,registered", [(11, True), (12, True), (13, False), (None, False)])
def test_setup_registers_only_on_listed_clones(monkeypatch, clone, registered):
    monkeypatch.setattr(config, "TEST_CLONE_IDS", {11, 12})
    monkeypatch.setattr(guard, "_AI_ONLY_COMMANDS", {})
    bot = MagicMock(clone_id=clone)
    bot.test_harness_cog = None
    run(cogmod.setup(bot))
    assert ("testharness status" in guard._AI_ONLY_COMMANDS) is registered
    assert (bot.test_harness_cog is not None) is registered
    bot.add_cog.assert_not_called()


def test_setup_on_main_bot_when_no_clones_listed(monkeypatch):
    monkeypatch.setattr(config, "TEST_CLONE_IDS", set())
    monkeypatch.setattr(guard, "_AI_ONLY_COMMANDS", {})
    run(cogmod.setup(MagicMock(clone_id=None)))
    assert "testharness status" in guard._AI_ONLY_COMMANDS
    monkeypatch.setattr(guard, "_AI_ONLY_COMMANDS", {})
    run(cogmod.setup(MagicMock(clone_id=11)))
    assert guard._AI_ONLY_COMMANDS == {}


# ── run ownership: a bot only touches its own runs ───────────────────────────────────────
@pytest.fixture()
def armed(monkeypatch):
    monkeypatch.setattr(config, "TEST_GUILD_ID", GUILD, raising=False)
    monkeypatch.setattr(config, "DISCORD_CLONE_ADMIN_IDS", {OWNER})
    monkeypatch.setattr(config, "TEST_CLONE_IDS", {11, 12})
    monkeypatch.setattr(cogmod.modx, "log_action", AsyncMock())


def inter(clone_id):
    i = SimpleNamespace(guild_id=GUILD, guild=SimpleNamespace(id=GUILD), user=SimpleNamespace(id=OWNER),
                        client=SimpleNamespace(clone_id=clone_id), channel=SimpleNamespace(id=9, send=AsyncMock()))
    i.response = SimpleNamespace(is_done=lambda: False, send_message=AsyncMock(), defer=AsyncMock())
    i.followup = SimpleNamespace(send=AsyncMock())
    return i


def cog_for(clone_id):
    c = TestHarnessCog(MagicMock(clone_id=clone_id))
    c._restore_run = AsyncMock()
    return c


def a_run(clone):
    return {"id": 5, "guild_id": GUILD, "owner_id": OWNER, "status": "armed", "clone_id": clone,
            "deadline_at": datetime.now(timezone.utc) + timedelta(minutes=5)}


def test_timer_and_recovery_only_look_at_this_bots_own_runs(armed, monkeypatch):
    seen = []

    async def unfinished(guild_id=None, clone_id="ANY"):
        seen.append(clone_id)
        return []
    monkeypatch.setattr(cogmod.db, "th_unfinished_runs", unfinished, raising=False)
    run(cog_for(12)._tick())
    run(cog_for(11)._tick())
    assert seen == [12, 11]                                  # never the unfiltered (everyone's) query


def test_stop_does_not_touch_another_bots_run(armed, monkeypatch):
    async def unfinished(guild_id=None, clone_id="ANY"):
        return [a_run(11)] if clone_id == "ANY" or clone_id == 11 else []
    monkeypatch.setattr(cogmod.db, "th_unfinished_runs", unfinished, raising=False)
    c, i = cog_for(12), inter(12)
    run(c.harness_stop.callback(c, i))
    c._restore_run.assert_not_awaited()
    assert "other test bot" in i.response.send_message.await_args.args[0]


def test_stop_restores_its_own_run(armed, monkeypatch):
    async def unfinished(guild_id=None, clone_id="ANY"):
        return [a_run(12)] if clone_id in ("ANY", 12) else []
    monkeypatch.setattr(cogmod.db, "th_unfinished_runs", unfinished, raising=False)
    c, i = cog_for(12), inter(12)
    run(c.harness_stop.callback(c, i))
    c._restore_run.assert_awaited_once()


def test_new_runs_record_which_bot_made_them(armed, monkeypatch):
    created = {}

    async def create(*args):
        created["args"] = args
        return None                                          # "already armed" ends the handler right after the call
    monkeypatch.setattr(cogmod.db, "th_create_run", create, raising=False)
    monkeypatch.setattr(cogmod.gs, "capture", lambda g: {"hash": "h", "x": 1})
    monkeypatch.setattr(cogmod.gs, "seal", lambda s: s)
    monkeypatch.setattr(cogmod.gs, "verify", lambda s: True)
    run(cog_for(11).ai_snapshot(inter(11)))
    assert created["args"][-1] == 11


def test_wave_only_arms_from_its_own_run(armed, monkeypatch):
    async def unfinished(guild_id=None, clone_id="ANY"):
        return [a_run(11)] if clone_id in ("ANY", 11) else []
    monkeypatch.setattr(cogmod.db, "th_unfinished_runs", unfinished, raising=False)
    assert run(cog_for(12)._armed_run(GUILD)) is None
    assert run(cog_for(11)._armed_run(GUILD))["id"] == 5
