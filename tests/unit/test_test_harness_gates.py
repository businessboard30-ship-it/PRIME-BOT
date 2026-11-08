"""Bot test harness, phase 1 (docs/BOT_TEST_HARNESS_PLAN.md): the three gates must refuse everywhere except the one
test guild, for the owner only, on the main bot only, and the whole feature is off while TEST_GUILD_ID is unset."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import config
from discord_bot.cogs import test_harness as cogmod
from discord_bot.cogs.test_harness import TestHarnessCog
from modules import ai_command_guard as guard
from modules import test_harness as th
from modules.ai_command_allowlist import AI_COMMANDS, AI_COMMANDS_BY_NAME


@pytest.fixture(autouse=True)
def _main_bot_is_the_harness_bot(monkeypatch):
    """These tests model the main bot as the harness bot; the clone rule has its own tests (test_th_clones.py)."""
    import config as _config
    monkeypatch.setattr(_config, "TEST_CLONE_IDS", set(), raising=False)

TEST_GUILD = 111
OTHER_GUILD = 222
OWNER = 42
STRANGER = 99


def run(coro):
    return asyncio.run(coro)


def inter(guild_id=TEST_GUILD, user_id=OWNER, clone_id=None):
    i = SimpleNamespace(
        guild_id=guild_id, guild=SimpleNamespace(id=guild_id), user=SimpleNamespace(id=user_id),
        client=SimpleNamespace(clone_id=clone_id, tree=MagicMock(get_command=lambda n: None, walk_commands=lambda: [])),
        permissions=__import__("discord").Permissions.all(),
    )
    i.response = SimpleNamespace(is_done=lambda: False, send_message=AsyncMock())
    i.followup = SimpleNamespace(send=AsyncMock())
    return i


@pytest.fixture()
def armed(monkeypatch):
    monkeypatch.setattr(config, "TEST_GUILD_ID", TEST_GUILD, raising=False)
    monkeypatch.setattr(config, "DISCORD_CLONE_ADMIN_IDS", {OWNER})
    monkeypatch.setattr(cogmod.modx, "log_action", AsyncMock())
    monkeypatch.setattr(cogmod.db, "th_latest_run", AsyncMock(return_value=None), raising=False)
    monkeypatch.setattr(guard, "_AI_ONLY_COMMANDS", {})
    guard.register_hidden_cog(TestHarnessCog(MagicMock()))
    monkeypatch.setattr(guard.db, "log_ai_command", AsyncMock())
    monkeypatch.setattr(guard, "check_ai_command_cap", AsyncMock(return_value=(True, None)))


# ── pure gate logic ──────────────────────────────────────────────────────
def test_allowed_only_for_owner_in_test_guild_on_main_bot():
    assert th.check_gates(TEST_GUILD, OWNER, None, test_guild=TEST_GUILD, admin_ids={OWNER}) is None


@pytest.mark.parametrize("unset", [0, None, "", "abc", -5])
def test_unset_or_invalid_test_guild_disables_everywhere(unset):
    assert th.check_gates(TEST_GUILD, OWNER, None, test_guild=unset, admin_ids={OWNER}) is not None
    assert th.check_gates(0, OWNER, None, test_guild=unset, admin_ids={OWNER}) is not None


def test_default_config_is_armed_for_the_one_test_guild_only():
    assert config.TEST_GUILD_ID == 1536047270321979514 and th.is_enabled() is True
    owner = next(iter(config.DISCORD_CLONE_ADMIN_IDS))
    assert th.check_gates(config.TEST_GUILD_ID, owner) is None
    assert th.check_gates(1, owner) is not None                       # any other guild
    assert th.check_gates(config.TEST_GUILD_ID, 1) is not None        # any other user
    assert th.check_gates(config.TEST_GUILD_ID, owner, clone_id=1) is not None


def test_wrong_guild_refused():
    assert th.check_gates(OTHER_GUILD, OWNER, None, test_guild=TEST_GUILD, admin_ids={OWNER})
    assert th.check_gates(None, OWNER, None, test_guild=TEST_GUILD, admin_ids={OWNER})


def test_non_owner_refused_even_with_all_permissions():
    assert th.check_gates(TEST_GUILD, STRANGER, None, test_guild=TEST_GUILD, admin_ids={OWNER})
    assert th.check_gates(TEST_GUILD, None, None, test_guild=TEST_GUILD, admin_ids={OWNER})


def test_clone_refused():
    assert th.check_gates(TEST_GUILD, OWNER, 7, test_guild=TEST_GUILD, admin_ids={OWNER})
    assert th.check_gates(TEST_GUILD, OWNER, 0, test_guild=TEST_GUILD, admin_ids={OWNER})  # 0 is still "a clone id"


# ── allowlist invariants ─────────────────────────────────────────────────
def test_every_test_guild_spec_is_ai_only_and_only_the_harness():
    flagged = [s for s in AI_COMMANDS if s.test_guild_only]
    assert flagged, "phase 1 must register the status command"
    assert all(s.ai_only for s in flagged), "test-guild tools must never be real slash commands"
    assert all(s.cog_module == "discord_bot.cogs.test_harness" for s in flagged)
    assert AI_COMMANDS_BY_NAME["testharness_status"].test_guild_only


# ── offer gate ───────────────────────────────────────────────────────────
def offered(i):
    return {s.name for s in run(guard.get_qualifying_commands(i))}


def test_offer_gate_shows_tool_only_to_owner_in_test_guild(armed):
    assert "testharness_status" in offered(inter())
    assert "testharness_status" not in offered(inter(guild_id=OTHER_GUILD))
    assert "testharness_status" not in offered(inter(user_id=STRANGER))
    assert "testharness_status" not in offered(inter(clone_id=3))


def test_offer_gate_hides_tool_when_unset(monkeypatch, armed):
    monkeypatch.setattr(config, "TEST_GUILD_ID", 0)
    assert "testharness_status" not in offered(inter())


# ── execution gate ───────────────────────────────────────────────────────
def check(i):
    return run(guard.resolve_and_check(i, "testharness_status", {}))


def test_execution_gate_allows_owner_in_test_guild(armed):
    spec, cmd, reason, cap = check(inter())
    assert reason is None and spec is not None and cmd is not None


@pytest.mark.parametrize("kw", [dict(guild_id=OTHER_GUILD), dict(user_id=STRANGER), dict(clone_id=3)])
def test_execution_gate_refuses_and_reveals_nothing(armed, kw):
    spec, cmd, reason, cap = check(inter(**kw))
    assert spec is None and cmd is None and cap is False
    assert reason == "'testharness_status' isn't something I'm able to run."
    guard.db.log_ai_command.assert_awaited()


def test_execution_gate_refuses_when_unset(monkeypatch, armed):
    monkeypatch.setattr(config, "TEST_GUILD_ID", 0)
    assert check(inter())[0] is None


# ── handler gate (direct call, skipping the guard entirely) ──────────────
def call_handler(i):
    cog = TestHarnessCog(MagicMock())
    run(TestHarnessCog.harness_status.callback(cog, i))
    return i


def test_handler_gate_runs_for_owner_in_test_guild(armed):
    i = call_handler(inter())
    msg = i.response.send_message.await_args.args[0]
    assert "armed" in msg
    cogmod.modx.log_action.assert_awaited_once()


@pytest.mark.parametrize("kw", [dict(guild_id=OTHER_GUILD), dict(user_id=STRANGER), dict(clone_id=3)])
def test_handler_gate_refuses_direct_calls(armed, kw):
    i = call_handler(inter(**kw))
    assert i.response.send_message.await_args.args[0] == cogmod.REFUSAL
    cogmod.modx.log_action.assert_not_awaited()


def test_handler_gate_refuses_direct_call_when_unset(monkeypatch, armed):
    monkeypatch.setattr(config, "TEST_GUILD_ID", 0)
    assert call_handler(inter()).response.send_message.await_args.args[0] == cogmod.REFUSAL


# ── never on the slash tree / never on clones ────────────────────────────
def test_setup_skips_clones_and_never_touches_the_tree(monkeypatch):
    monkeypatch.setattr(guard, "_AI_ONLY_COMMANDS", {})
    bot = MagicMock(clone_id=5)
    run(cogmod.setup(bot))
    assert guard._AI_ONLY_COMMANDS == {}
    bot = MagicMock(clone_id=None)
    run(cogmod.setup(bot))
    assert "testharness status" in guard._AI_ONLY_COMMANDS
    bot.add_cog.assert_not_called()
