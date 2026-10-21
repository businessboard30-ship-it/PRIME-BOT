"""The birthday announcer must cost nothing until a server sets birthdays up."""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import discord
from discord.ext import commands

from discord_bot.cogs import birthdays as cogmod
from modules import birthday_store as store


def _cog(monkeypatch, *, has_work=False, configs=(), grants=()):
    bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
    bot.wait_until_ready = AsyncMock()
    bot.get_guild = lambda gid: None            # none of the configured servers live on this process
    calls = {"has_work": 0, "configs": 0, "grants": 0}

    async def _has_work(clone_id=None):
        calls["has_work"] += 1
        return has_work

    async def _configs(clone_id=None):
        calls["configs"] += 1
        return list(configs)

    async def _grants(clone_id=None):
        calls["grants"] += 1
        return list(grants)

    monkeypatch.setattr(store, "has_work", _has_work)
    monkeypatch.setattr(store, "enabled_configs", _configs)
    monkeypatch.setattr(store, "role_grants", _grants)
    return cogmod.BirthdayCog(bot), calls


def test_nothing_set_up_means_no_worker_and_no_queries(monkeypatch):
    async def go():
        cog, calls = _cog(monkeypatch, has_work=False)
        await cog.cog_load()
        await asyncio.sleep(0.05)
        assert cog._task is None
        assert calls["configs"] == 0 and calls["grants"] == 0     # not even one poll
        assert calls["has_work"] == 1                              # a single startup check
    asyncio.run(go())


def test_worker_stops_itself_when_nothing_is_enabled(monkeypatch):
    async def go():
        cog, calls = _cog(monkeypatch)
        await cog.cog_load()
        cog.wake()
        await asyncio.sleep(0.1)
        assert cog._task is None                # shut down by itself
        assert calls["configs"] == 1            # one pass, then done
    asyncio.run(go())


def test_worker_keeps_running_while_a_server_has_it_on(monkeypatch):
    async def go():
        cog, calls = _cog(monkeypatch, configs=[{"guild_id": 1}])
        cog.CHECK_SECONDS = 0.01
        await cog.cog_load()
        cog.wake()
        await asyncio.sleep(0.15)
        assert cog._task is not None and not cog._task.done()
        assert calls["configs"] >= 3            # polling while in use
        cog._task.cancel()
    asyncio.run(go())


def test_pending_birthday_role_keeps_worker_alive(monkeypatch):
    """A role still waiting to be taken back (server present, not expired yet) keeps the worker watching."""
    from datetime import date, timedelta

    async def go():
        future = date.today() + timedelta(days=30)
        cog, _ = _cog(monkeypatch, grants=[{"guild_id": 1, "user_id": 2, "role_id": 3, "remove_after": future}])
        guild = MagicMock(); guild.id = 1
        cog.bot.get_guild = lambda gid: guild
        monkeypatch.setattr(store, "get_config", AsyncMock(return_value={"utc_offset_hours": 0}))
        cog.CHECK_SECONDS = 0.01
        await cog.cog_load()
        cog.wake()
        await asyncio.sleep(0.05)
        assert cog._task is not None and not cog._task.done()
        cog._task.cancel()
    asyncio.run(go())


def test_grant_for_a_server_we_are_not_in_does_not_keep_worker_alive(monkeypatch):
    async def go():
        cog, _ = _cog(monkeypatch, grants=[{"guild_id": 1, "user_id": 2, "role_id": 3, "remove_after": None}])
        await cog.cog_load()
        cog.wake()
        await asyncio.sleep(0.1)
        assert cog._task is None                # nothing it can act on -> sleeps
    asyncio.run(go())


def test_enabling_during_a_pass_is_not_missed(monkeypatch):
    async def go():
        cog, calls = _cog(monkeypatch)
        real = store.enabled_configs
        state = {"n": 0}

        async def configs(clone_id=None):
            state["n"] += 1
            if state["n"] == 1:
                cog.wake()                      # a server turns it on mid-pass
            return []
        monkeypatch.setattr(store, "enabled_configs", configs)
        await cog.cog_load()
        cog.wake()
        await asyncio.sleep(0.1)
        assert state["n"] == 2                  # ran again instead of shutting down
        assert cog._task is None
    asyncio.run(go())


def test_wake_after_shutdown_starts_a_fresh_worker(monkeypatch):
    async def go():
        cog, calls = _cog(monkeypatch)
        await cog.cog_load()
        cog.wake(); await asyncio.sleep(0.05)
        assert cog._task is None
        cog.wake(); await asyncio.sleep(0.05)
        assert calls["configs"] == 2
    asyncio.run(go())


def test_startup_check_failure_falls_back_to_a_safe_start(monkeypatch):
    async def go():
        cog, calls = _cog(monkeypatch)
        async def boom(clone_id=None): raise RuntimeError("db down")
        monkeypatch.setattr(store, "has_work", boom)
        await cog.cog_load()
        await asyncio.sleep(0.1)
        assert calls["configs"] == 1            # it did start, found nothing, and stopped
        assert cog._task is None
    asyncio.run(go())
