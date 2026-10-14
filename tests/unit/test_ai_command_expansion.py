"""AI-runnable commands: group subcommands, permission enforcement, response adapter, and a coverage floor."""
import asyncio
import importlib
import inspect
import pkgutil
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from discord import app_commands
from discord.ext import commands

from modules import ai_command_guard as guard
from modules import command_reference
from modules.ai_command_allowlist import (
    AI_COMMANDS, AI_COMMANDS_BY_NAME, BANNED_OWNER_ONLY_COMMANDS, is_command_allowed, module_is_banned,
    runnable_labels,
)

# The one allowlisted name that doesn't exist as a live command. It was already like this before the expansion
# (resolve_and_check reports it as "naming drift"), so it is tracked here instead of silently ignored.
KNOWN_UNRESOLVED = {"announcements"}
# Group subcommands that only touch the asker's OWN data, so they legitimately declare no server permission.
USER_SCOPED = {"tools_alert_set", "tools_alert_remove", "tools_alert_clear"}


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(scope="module")
def top():
    import discord_bot.cogs as pkg
    found = {}
    for m in pkgutil.iter_modules(pkg.__path__):
        name = f"discord_bot.cogs.{m.name}"
        mod = importlib.import_module(name)
        for _, cls in inspect.getmembers(mod, inspect.isclass):
            if issubclass(cls, commands.Cog) and cls.__module__ == name:
                for c in getattr(cls, "__cog_app_commands__", []):
                    found.setdefault(c.name, (name, c))
    return found


@pytest.fixture(scope="module")
def bot(top):
    tree = SimpleNamespace(get_command=lambda n: top[n][1] if n in top else None,
                           walk_commands=lambda: (s for _, (_m, c) in top.items() for s in
                                                  ([c] + list(c.walk_commands()) if isinstance(c, app_commands.Group) else [c])))
    return SimpleNamespace(tree=tree)


def live(bot, spec):
    return guard._resolve_command(bot, spec.command_path)


# ── the allowlist itself ──────────────────────────────────────────────────

def test_every_spec_resolves_to_a_live_runnable_command(bot):
    missing = [s.name for s in AI_COMMANDS if s.name not in KNOWN_UNRESOLVED and live(bot, s) is None]
    assert missing == []


def test_tool_names_are_unique_and_valid_for_the_model():
    names = [s.name for s in AI_COMMANDS]
    assert len(names) == len(set(names))
    assert all(re.fullmatch(r"[A-Za-z0-9_-]{1,64}", n) for n in names)


def test_coverage_of_top_level_commands_is_at_least_70_percent(top, bot):
    roots = set()
    for s in AI_COMMANDS:
        c = live(bot, s)
        if c is None:
            continue
        node = c
        while getattr(node, "parent", None) is not None:
            node = node.parent
        roots.add(node.name)
    pct = 100 * len(roots) / len(top)
    assert pct >= 70, f"only {len(roots)} of {len(top)} top-level commands ({pct:.0f}%) are AI-runnable"


def test_no_money_premium_or_owner_commands_are_runnable(bot):
    for s in AI_COMMANDS:
        c = live(bot, s)
        if c is None:
            continue
        module = type(c.binding).__module__ if getattr(c, "binding", None) is not None else s.cog_module
        assert not module_is_banned(module), f"{s.name} is bound to a banned module {module}"
        assert not module_is_banned(s.cog_module), s.name
        assert c.name not in BANNED_OWNER_ONLY_COMMANDS
        assert is_command_allowed(s.name) is not None


@pytest.mark.parametrize("name", ["welcome_buypack", "welcome_buyultra", "welcome_theme", "levelrole_giftboost",
                                  "ad_submit", "archive_boost", "card_pack", "economy_buy", "premium",
                                  "automod_owner_cleanupreminders", "admin_stats", "ownerbroadcast"])
def test_money_and_owner_commands_stay_out(name):
    assert is_command_allowed(name) is None


NEW_PLAIN = {"cat", "catsetup", "honeypot", "scamshield", "bumpsetup"}
# Pre-existing allowlist entry that was already attachment-based when it was the top-level /imagesearch;
# it became a subcommand of /tools without changing behaviour, so it is not a "new" command.
LEGACY_ATTACHMENT = {"tools_imagesearch"}


def test_new_commands_need_no_attachment_or_modal(bot):
    for s in AI_COMMANDS:
        c = live(bot, s)
        if c is None or s.name in LEGACY_ATTACHMENT or not (s.path or s.name in NEW_PLAIN):
            continue
        assert all(getattr(p.type, "name", "") != "attachment" for p in c.parameters), s.name
        src = inspect.getsource(c.callback)
        assert "send_modal" not in src, f"{s.name} opens a modal, which an AI-run command can't"


def test_new_server_changing_subcommands_declare_a_permission():
    for s in AI_COMMANDS:
        if s.path and s.requires_confirmation and s.name not in USER_SCOPED:
            assert s.needs_perms, f"{s.name} changes the server but declares no needs_perms"
    names = {s.name for s in AI_COMMANDS}
    assert {"welcome_enable", "automod_bannedword_add", "schedule_once"} <= names


# ── permission enforcement ────────────────────────────────────────────────

def _inter(**perm):
    return SimpleNamespace(permissions=discord.Permissions(**perm), user=SimpleNamespace(id=1))


SPEC = AI_COMMANDS_BY_NAME["welcome_enable"]


def test_spec_permissions_enforced():
    ok, _ = guard.check_spec_permissions(_inter(manage_guild=True), SPEC)
    assert ok
    ok, reason = guard.check_spec_permissions(_inter(send_messages=True), SPEC)
    assert not ok and "Manage Guild" in reason
    ok, _ = guard.check_spec_permissions(_inter(administrator=True), SPEC)
    assert ok
    ok, _ = guard.check_spec_permissions(_inter(), AI_COMMANDS_BY_NAME["rank"])        # no perms declared
    assert ok
    assert guard.check_spec_permissions(SimpleNamespace(permissions=None, user=SimpleNamespace(guild_permissions=None)),
                                        SPEC)[0] is False


def test_resolve_and_check_denies_a_member_without_the_permission(bot, monkeypatch):
    log = AsyncMock()
    monkeypatch.setattr(guard.db, "log_ai_command", log)
    monkeypatch.setattr(guard, "check_ai_command_cap", AsyncMock(return_value=(True, None)))
    i = SimpleNamespace(client=bot, guild=object(), guild_id=5, user=SimpleNamespace(id=9),
                        permissions=discord.Permissions(send_messages=True))
    spec, cmd, reason, cap = run(guard.resolve_and_check(i, "welcome_enable", {}))
    assert spec is None and "permission" in reason.lower() and not cap
    i.permissions = discord.Permissions(manage_guild=True)
    spec, cmd, reason, cap = run(guard.resolve_and_check(i, "welcome_enable", {}))
    assert reason is None and cmd.qualified_name == "welcome enable"


def test_groups_and_missing_paths_never_resolve(bot):
    assert guard._resolve_command(bot, "welcome") is None                  # a Group has no callback
    assert guard._resolve_command(bot, "welcome nope") is None
    assert guard._resolve_command(bot, "nope enable") is None
    assert guard._resolve_command(bot, "automod bannedword add").qualified_name == "automod bannedword add"


# ── response adapter ──────────────────────────────────────────────────────

def _used_interaction():
    i = MagicMock(spec=discord.Interaction)
    i.response = MagicMock()
    i.response.is_done.return_value = True
    i.followup = SimpleNamespace(send=AsyncMock())
    i.edit_original_response = AsyncMock()
    return i


def test_adapter_turns_a_second_response_into_a_followup():
    i = _used_interaction()
    original = guard._adapt_used_response(i)
    assert original is not None and isinstance(i.response, guard._AlreadyRespondedResponse)
    run(i.response.send_message("hi", ephemeral=True, tts=False))
    i.followup.send.assert_awaited_once_with("hi", ephemeral=True)
    assert run(i.response.defer(thinking=True)) is None and i.response.is_done()
    run(i.response.edit_message(content="x"))
    i.edit_original_response.assert_awaited_once_with(content="x")
    with pytest.raises(RuntimeError):
        run(i.response.send_modal(object()))


def test_adapter_leaves_fresh_and_standin_interactions_alone():
    fresh = _used_interaction()
    fresh.response.is_done.return_value = False
    assert guard._adapt_used_response(fresh) is None
    standin = SimpleNamespace(response=SimpleNamespace(is_done=lambda: True))
    assert guard._adapt_used_response(standin) is None


# ── what the AI is told ───────────────────────────────────────────────────

def test_runnable_labels_include_group_and_path():
    labels = runnable_labels(["welcome_enable", "rank", "automod_bannedword_add", "nonsense"])
    assert {"welcome enable", "welcome", "rank", "automod bannedword add", "automod"} <= labels


def test_command_reference_says_which_subcommands_are_runnable():
    bot = SimpleNamespace(tree=SimpleNamespace(get_commands=lambda type=None: []))
    text = command_reference.build_context(None, None, runnable={"welcome", "welcome enable", "welcome disable"})
    assert "I can run" in text
    lines = ["/welcome <enable|disable|message> — Configure welcome cards"]
    runnable = {"welcome", "welcome enable", "welcome disable"}
    import modules.command_reference as cr
    orig = cr._live_lines
    cr._live_lines = lambda b: lines
    try:
        out = cr.build_context(None, object(), runnable=runnable)
    finally:
        cr._live_lines = orig
    assert "I can run these for you with the matching tool: disable, enable" in out
    assert "describe-only" in out


def test_tool_schema_for_a_subcommand(bot):
    from discord_bot.cogs.ai_tools import AIToolsCog
    cog = AIToolsCog.__new__(AIToolsCog)
    for name in ("welcome_enable", "automod_toggle", "schedule_once"):
        spec = AI_COMMANDS_BY_NAME[name]
        schema = cog._command_tool_schema(spec, live(bot, spec))
        fn = schema["function"]
        assert fn["name"] == name and fn["parameters"]["required"]
    toggle = cog._command_tool_schema(AI_COMMANDS_BY_NAME["automod_toggle"], live(bot, AI_COMMANDS_BY_NAME["automod_toggle"]))
    assert "enum" in toggle["function"]["parameters"]["properties"]["filter"]
