"""Server-panel honeypot writes must clear the listener's 60s config cache."""
import asyncio
import importlib
import sys
import time
import types
from unittest.mock import AsyncMock

import pytest

GUILD, CLONE, ACTOR = 9100, None, 5


class FakeDB:
    def __init__(self):
        self.cfg = {"channel_id": 77, "enabled": True, "alert_role_id": None}

    async def get_honeypot_config(self, g, clone_id=None): return dict(self.cfg)
    async def set_honeypot_config(self, g, clone_id=None, **f): self.cfg.update(f); return dict(self.cfg)
    async def delete_honeypot_config(self, g, clone_id=None): self.cfg = {"channel_id": None, "enabled": True}


@pytest.fixture()
def env(monkeypatch):
    fake = FakeDB()
    dbm = types.ModuleType("database"); dbm.db = fake
    dbm.get_pool = AsyncMock(side_effect=RuntimeError("no database in unit tests"))
    monkeypatch.setitem(sys.modules, "database", dbm)
    for n in ("modules.server_panel", "discord_bot.cogs.honeypot"):
        sys.modules.pop(n, None)
    sp = importlib.import_module("modules.server_panel")
    hp = importlib.import_module("discord_bot.cogs.honeypot")
    hp._cache.clear()
    yield types.SimpleNamespace(sp=sp, hp=hp, db=fake)
    for n in ("modules.server_panel", "discord_bot.cogs.honeypot"):
        sys.modules.pop(n, None)


def test_toggle_off_then_on_is_seen_by_the_listener_immediately(env):
    async def go():
        assert (await env.hp._cached_config(GUILD, CLONE))["enabled"] is True      # primes the cache
        await env.sp.set_honeypot(GUILD, CLONE, ACTOR, enabled=False)
        assert (await env.hp._cached_config(GUILD, CLONE))["enabled"] is False     # not 60s stale
        await env.sp.set_honeypot(GUILD, CLONE, ACTOR, enabled=True)
        assert (await env.hp._cached_config(GUILD, CLONE))["enabled"] is True
    asyncio.run(go())


def test_reset_clears_the_cache_so_a_removed_trap_stops_firing(env):
    async def go():
        assert await env.hp._cached_config(GUILD, CLONE)                            # cached as live
        await env.sp.reset_feature(GUILD, CLONE, ACTOR, "honeypot")
        assert await env.hp._cached_config(GUILD, CLONE) is None
    asyncio.run(go())


def test_a_cache_clear_failure_never_blocks_the_write(env, monkeypatch):
    monkeypatch.setattr(env.hp, "_invalidate", lambda *a: (_ for _ in ()).throw(RuntimeError("x")))
    asyncio.run(env.sp.set_honeypot(GUILD, CLONE, ACTOR, enabled=False))
    assert env.db.cfg["enabled"] is False
