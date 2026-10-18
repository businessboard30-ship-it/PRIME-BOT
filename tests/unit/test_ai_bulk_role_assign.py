"""Bulk 'give / take a role to many members' (member role-add-all / role-remove-all) -- planning rules and the background run."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from discord_bot.cogs.ai_server_ops import AIServerOpsCog
from modules import ai_bulk
from modules.ai_command_allowlist import AI_COMMANDS_BY_NAME, needs_confirmation
from tests.unit.test_ai_server_ops import FakeMember, FakeRole, World


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(ai_bulk, "ASSIGN_PAUSE_SECONDS", 0)
    ai_bulk.JOBS._running.clear(); ai_bulk.JOBS._finished.clear()


def crowd(w, humans=3, bots=1):
    ms = []
    for n in range(humans):
        m = FakeMember(1000 + n, f"H{n}", w.staff); m.roles = []; m.bot = False; ms.append(m)
    for n in range(bots):
        m = FakeMember(2000 + n, f"B{n}", w.staff); m.roles = []; m.bot = True; ms.append(m)
    w.target.bot = False
    w.guild.members = ms + [w.target]
    w.guild.get_member = lambda mid: next((m for m in w.guild.members if m.id == mid), None)
    w.i.channel = SimpleNamespace(send=AsyncMock())
    return ms


def plan(w, key="member_role_add_all", **kw):
    return asyncio.run(AIServerOpsCog(None)._plan(key, w.i, **kw))


def test_commands_registered_and_confirmed():
    for n in ("member_role_add_all", "member_role_remove_all"):
        assert n in AI_COMMANDS_BY_NAME and AI_COMMANDS_BY_NAME[n].ai_only and needs_confirmation(AI_COMMANDS_BY_NAME[n], {})


def test_confirm_text_shows_exact_head_count_and_scope():
    w = World(); crowd(w)
    p = plan(w, role="Staff")
    assert p.error is None and "**5**" in p.text and "everyone" in p.text
    assert "**1**" in plan(w, role="Staff", who="bots").text and "bots only" in plan(w, role="Staff", who="bots").text
    assert "**4**" in plan(w, role="Staff", who="humans").text


def test_has_role_filter_and_skips_those_who_already_have_it():
    w = World(); ms = crowd(w)
    ms[0].roles = [w.staff]
    assert "**4**" in plan(w, role="Staff").text                       # one already has it
    ms[1].roles = [w.helper]
    p = plan(w, role="Staff", who="humans", has_role="Helper")
    assert p.error is None and "**1**" in p.text


def test_refuses_unsafe_roles():
    w = World(); crowd(w)
    assert "managed" in plan(w, role="Booster").error
    assert "above your highest" in plan(w, role="High").error
    assert "@everyone" in plan(w, role="@everyone").error
    risky = FakeRole(9, "Mods", 2, perms=discord.Permissions(kick_members=True).value)
    w.guild.roles.append(risky)
    assert "powerful permissions" in plan(w, role="Mods").error
    assert plan(w, role="Mods", key="member_role_remove_all").error != "x"   # removing a risky role in bulk is allowed (not blocked by that rule)


def test_needs_manage_roles_and_valid_scope_and_nothing_to_do():
    w = World(actor_perms=0); crowd(w)
    assert "Manage Roles" in plan(w, role="Staff").error
    w = World(); ms = crowd(w)
    assert "everyone, humans or bots" in plan(w, role="Staff", who="admins").error
    for m in w.guild.members:
        m.roles = [w.staff]
    assert "nothing to do" in plan(w, role="Staff").error


def test_cap(monkeypatch):
    w = World(); crowd(w)
    monkeypatch.setattr(ai_bulk, "MAX_BULK_ASSIGN", 2)
    assert "at most 2" in plan(w, role="Staff").error


async def _go(w, key, **kw):
    cog = AIServerOpsCog(None)
    p = await cog._plan(key, w.i, **kw)
    assert p.error is None, p.error
    started = await p.run()
    await asyncio.gather(*list(AIServerOpsCog._BULK_TASKS))
    return started


def test_run_adds_role_in_background_and_posts_summary():
    w = World(); ms = crowd(w)
    started = asyncio.run(_go(w, "member_role_add_all", role="Staff", who="humans"))
    assert "Started" in started
    for m in ms:
        if not m.bot:
            m.add_roles.assert_awaited_once()
        else:
            m.add_roles.assert_not_awaited()
    assert "Gave" in w.i.channel.send.await_args.args[0] and not ai_bulk.JOBS.is_running(w.guild.id)


def test_remove_all_and_one_job_per_server():
    w = World(); ms = crowd(w)
    for m in ms:
        m.roles = [w.staff]
    asyncio.run(_go(w, "member_role_remove_all", role="Staff", who="bots"))
    assert ms[-1].remove_roles.await_count == 1 and ms[0].remove_roles.await_count == 0

    async def twice():
        cog = AIServerOpsCog(None)
        p1 = await cog._plan("member_role_remove_all", w.i, role="Staff", who="humans")
        p2 = await cog._plan("member_role_remove_all", w.i, role="Staff", who="humans")
        ai_bulk.JOBS._finished.clear()
        r1 = await p1.run(); r2 = await p2.run()
        await asyncio.gather(*list(AIServerOpsCog._BULK_TASKS))
        return r1, r2
    r1, r2 = asyncio.run(twice())
    assert "Started" in r1 and "already running" in r2


def test_forbidden_stops_the_run_and_frees_the_lock():
    w = World(); ms = crowd(w)
    ms[0].add_roles.side_effect = discord.Forbidden(SimpleNamespace(status=403, reason="x"), "no")
    asyncio.run(_go(w, "member_role_add_all", role="Staff", who="humans"))
    assert "Stopped early" in w.i.channel.send.await_args.args[0]
    assert not ai_bulk.JOBS.is_running(w.guild.id)
