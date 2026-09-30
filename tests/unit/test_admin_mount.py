"""Regression tests for discord_bot/cogs/_admin_mount.py.

Owner commands from several cogs are mounted under the single global /admin
group to save global slash-command slots (Discord caps them at 100). These
tests use real discord.py objects with tiny fake cogs, so they catch both our
helper regressing and a discord.py upgrade changing how bindings/groups work.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from discord import app_commands
from discord.ext import commands

from discord_bot.cogs._admin_mount import (
    get_admin_group,
    mount_admin_command,
    mount_admin_group,
)


class AdminCog(commands.Cog):  # name must be "AdminCog": that's what the helper looks up
    admin = app_commands.Group(name="admin", description="admin")

    @admin.command(name="stats", description="stats")
    async def stats(self, interaction: discord.Interaction):
        pass


class OwnerCog(commands.Cog):
    def __init__(self):
        self.calls = []

    async def cog_load(self):
        bot = self.bot
        mount_admin_command(bot, self.plain, name="plain", description="plain")
        mount_admin_command(bot, self.approve, name="approve", subgroup="payments", description="approve")
        mount_admin_command(bot, self.reject, name="reject", subgroup="payments", description="reject")
        mount_admin_command(bot, self.find, name="find", description="find",
                            autocompletes={"query": self.find_ac})

    @app_commands.describe(reference="ref")
    async def approve(self, interaction: discord.Interaction, reference: str, amount: float = None):
        self.calls.append(("approve", reference, amount))

    async def reject(self, interaction: discord.Interaction, reference: str):
        self.calls.append(("reject", reference))

    async def plain(self, interaction: discord.Interaction):
        self.calls.append(("plain",))

    async def find(self, interaction: discord.Interaction, query: str):
        self.calls.append(("find", query))

    async def find_ac(self, interaction: discord.Interaction, current: str):
        return [app_commands.Choice(name=f"{self.__class__.__name__}:{current}", value=current)]


class BumpLikeCog(commands.Cog):
    """Mirrors bump.py: a cog-level group re-parented under /admin in cog_load."""
    legacy = app_commands.Group(name="legacy", description="legacy")

    async def cog_load(self):
        self.__cog_app_commands__ = [c for c in self.__cog_app_commands__ if c is not self.legacy]
        mount_admin_group(self.bot, self.legacy, name="bump")

    @legacy.command(name="cooldown", description="cooldown")
    async def cooldown(self, interaction: discord.Interaction, minutes: int = None):
        self.saw_self = True


def _run(coro):
    return asyncio.run(coro)


async def _build(order=("admin", "owner", "bump")):
    bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
    cogs = {"admin": AdminCog(), "owner": OwnerCog(), "bump": BumpLikeCog()}
    for k in order:
        cogs[k].bot = bot  # the real cogs take bot in __init__
        await bot.add_cog(cogs[k])
    return bot, cogs


def test_single_top_level_slot_and_shape():
    bot, _ = _run(_build())
    top = [c.name for c in bot.tree.get_commands()]
    assert top == ["admin"], "mounted commands must not register top-level"
    admin = bot.tree.get_command("admin")
    names = {c.name for c in admin.commands}
    assert names == {"stats", "plain", "find", "payments", "bump"}
    assert {c.name for c in admin.get_command("payments").commands} == {"approve", "reject"}
    assert {c.name for c in admin.get_command("bump").commands} == {"cooldown"}
    # serializes within Discord's nesting rules (raises if invalid)
    payload = bot.tree.get_command("admin").to_dict(bot.tree)
    assert payload["name"] == "admin"


def test_self_is_bound_and_args_reach_the_method():
    bot, cogs = _run(_build())
    it = MagicMock()
    sub = bot.tree.get_command("admin").get_command("payments").get_command("approve")
    _run(sub._callback(sub.binding, it, reference="r1", amount=2.5))
    assert cogs["owner"].calls == [("approve", "r1", 2.5)]
    assert sub.binding is cogs["owner"]
    # @describe on the undecorated method survives into the command
    assert sub.parameters[0].description == "ref"
    # group re-parented from another cog still binds to its own cog
    cool = bot.tree.get_command("admin").get_command("bump").get_command("cooldown")
    _run(cool._callback(cool.binding, it))
    assert cogs["bump"].saw_self is True


def test_autocomplete_is_wired_to_the_bound_method():
    bot, cogs = _run(_build())
    find = bot.tree.get_command("admin").get_command("find")
    ac = find._params["query"].autocomplete
    out = _run(ac(MagicMock(), "abc"))
    assert out[0].name == "OwnerCog:abc"
    assert find.to_dict(bot.tree)["options"][0]["autocomplete"] is True


def test_missing_admin_cog_fails_loudly():
    async def go():
        bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
        with pytest.raises(RuntimeError, match="AdminCog must be loaded"):
            get_admin_group(bot)
    _run(go())


def test_load_order_matters_and_is_enforced():
    # Mounting cog loaded BEFORE admin must error (bot.py loads admin first).
    with pytest.raises(Exception):
        _run(_build(order=("owner", "admin", "bump")))


def test_reload_replaces_instead_of_raising():
    async def go():
        bot, cogs = await _build()
        await bot.remove_cog("OwnerCog")
        owner = OwnerCog(); owner.bot = bot
        await bot.add_cog(owner)  # cog_load re-mounts: override=True
        admin = bot.tree.get_command("admin")
        assert admin.get_command("payments").get_command("approve").binding is owner
    _run(go())
