"""AI-only commands: hidden from Discord's slash-command list, still runnable by the AI.

Why these exist (owner's decision): bulk channel create/delete and per-member message purge are only worth having
when someone describes the job in plain words to the AI. As typed slash commands they are clunky, rarely used, and
each one costs a slot of Discord's 100-command cap. See AICommandSpec.ai_only in modules/ai_command_allowlist.py.
"""
import asyncio

import discord
import pytest
from discord.ext import commands

from modules import ai_command_guard as guard
from modules.ai_command_allowlist import AI_COMMANDS

AI_ONLY_PATHS = {"setup bulk-create", "setup bulk-delete", "messages user-purge",
                 "role create", "role bulk-create", "role bulk-delete"}


def test_exactly_the_six_expected_specs_are_ai_only():
    assert {s.command_path for s in AI_COMMANDS if s.ai_only} == AI_ONLY_PATHS


@pytest.fixture()
def tree_bot(monkeypatch):
    """A real command tree holding the real cogs' groups, as the bot has right before it syncs."""
    monkeypatch.setattr(guard, "_AI_ONLY_COMMANDS", {})
    bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
    from discord_bot.cogs import setup_channels, moderation, role_setup
    for mod, cog_name in ((setup_channels, "SetupChannelsCog"), (moderation, "ModerationCog"), (role_setup, "RoleSetupCog")):
        cls = getattr(mod, cog_name, None) or next(
            c for n, c in vars(mod).items() if isinstance(c, type) and issubclass(c, commands.Cog) and c.__module__ == mod.__name__
            and any(g for g in getattr(c, "__cog_app_commands__", [])))
        asyncio.run(bot.add_cog(cls(bot)))
    return bot


def _paths(bot):
    return {c.qualified_name for c in bot.tree.walk_commands()}


def test_detach_removes_them_from_what_discord_would_see_but_not_the_rest(tree_bot):
    assert AI_ONLY_PATHS <= _paths(tree_bot)
    assert guard.detach_ai_only_commands(tree_bot) == 6
    after = _paths(tree_bot)
    assert not (AI_ONLY_PATHS & after)
    # siblings in the same groups are untouched
    assert "messages purge" in after and "setup channels" in after and "role setup" in after
    # and the synced payload is genuinely free of them
    def names(opts):                      # every command/subcommand NAME in the payload, recursively
        for o in opts or []:
            yield o["name"]
            yield from names(o.get("options"))
    payload_names = set(names([c.to_dict(tree_bot.tree) for c in tree_bot.tree.get_commands()]))
    role_group = next(c for c in tree_bot.tree.get_commands() if c.name == "role")
    assert not ({"create", "bulk-create", "bulk-delete"} & {c.name for c in role_group.commands})
    assert not ({"bulk-create", "bulk-delete", "user-purge"} & payload_names)
    assert {"purge", "channels", "role", "setup"} <= payload_names


def test_detach_is_idempotent(tree_bot):
    assert guard.detach_ai_only_commands(tree_bot) == 6
    assert guard.detach_ai_only_commands(tree_bot) == 0


def test_the_ai_can_still_resolve_them_with_their_cog_and_group(tree_bot):
    guard.detach_ai_only_commands(tree_bot)
    for path in AI_ONLY_PATHS:
        cmd = guard._resolve_command(tree_bot, path)
        assert isinstance(cmd, discord.app_commands.Command), path
        assert cmd.binding is not None, path                # the AI calls the cog's handler through this
        assert cmd.parent is not None, path                 # the group's own checks still apply
        assert cmd.parameters, path                         # the AI builds its tool schema from these


def test_other_commands_still_resolve_normally_after_detaching(tree_bot):
    guard.detach_ai_only_commands(tree_bot)
    assert guard._resolve_command(tree_bot, "messages purge") is not None
