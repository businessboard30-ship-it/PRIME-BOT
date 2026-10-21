import asyncio
import sys
import types

import pytest

from modules import welcome_studio as ws


def _fake_db(monkeypatch, fn):
    mod = types.ModuleType("database")
    mod.db = types.SimpleNamespace(is_guild_premium_active=fn)
    monkeypatch.setitem(sys.modules, "database", mod)


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_premium_unlocks(monkeypatch):
    async def active(g, c=None):
        return True
    _fake_db(monkeypatch, active)
    assert run(ws.studio_unlocked(1, None)) is True


def test_no_premium_locked(monkeypatch):
    async def inactive(g, c=None):
        return False
    _fake_db(monkeypatch, inactive)
    assert run(ws.studio_unlocked(1, None)) is False


def test_lookup_error_fails_closed(monkeypatch):
    async def boom(g, c=None):
        raise RuntimeError("db down")
    _fake_db(monkeypatch, boom)
    assert run(ws.studio_unlocked(1, None)) is False


def test_missing_guild_is_locked(monkeypatch):
    async def active(g, c=None):
        return True
    _fake_db(monkeypatch, active)
    assert run(ws.studio_unlocked(0, None)) is False
    assert run(ws.studio_unlocked(None, None)) is False


def test_clone_is_passed_through(monkeypatch):
    seen = []

    async def only_clone_7(g, c=None):
        seen.append((g, c))
        return c == 7
    _fake_db(monkeypatch, only_clone_7)
    assert run(ws.studio_unlocked(5, 7)) is True
    assert run(ws.studio_unlocked(5, None)) is False
    assert seen == [(5, 7), (5, None)]


def test_effective_options_strips_studio_keys_only_when_locked():
    opts = {"dim": 40, "frame": "thick", "rotation": "random"}
    keys = frozenset({"frame", "rotation"})
    assert ws.effective_studio_options(opts, True, keys) == opts
    assert ws.effective_studio_options(opts, False, keys) == {"dim": 40}
    # empty set (today) never changes anything
    assert ws.effective_studio_options(opts, False) is opts
