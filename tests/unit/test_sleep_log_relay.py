import asyncio
import json
from types import SimpleNamespace

import pytest

from discord_bot.cogs import guild_activity as cog_mod
from modules import sleep_events as se


class FakeDB:
    def __init__(self):
        self.rows = {}

    async def set_global_setting(self, key, value):
        self.rows[key] = value

    async def fetch(self, query, *args):
        keys = sorted(k for k in self.rows if k.startswith("sleep_events:"))[:20]
        out = [{"key": k, "value": self.rows.pop(k)} for k in keys]
        return out

    async def fetchval(self, query, *args):
        return 1 if any(k.startswith("sleep_events:") for k in self.rows) else None


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    se._queue.clear()
    se._last_sent.clear()
    fake = FakeDB()
    monkeypatch.setattr(cog_mod, "db", fake)
    yield fake
    se._queue.clear()
    se._last_sent.clear()


def _cog(clone_id, name):
    c = cog_mod.GuildActivityCog.__new__(cog_mod.GuildActivityCog)
    c.bot = SimpleNamespace(clone_id=clone_id, user=SimpleNamespace(name=name))
    return c


def test_clone_relays_its_events_and_main_collects_them_once(_clean):
    se.emit("sleep", "cat_spawner", "😴 **Cat spawner** went to sleep")
    se.emit("dormant", "42", "🌙 **Some Server** went dormant")
    clone = _cog(7, "Honeypot")
    asyncio.run(clone._relay_loop.coro(clone))
    assert se.pending() == 0 and len(_clean.rows) == 1
    key = next(iter(_clean.rows))
    assert key.startswith("sleep_events:7:")
    main = _cog(None, "PRIME")
    lines = asyncio.run(main._collect_relayed())
    assert lines == ["[Honeypot] 😴 **Cat spawner** went to sleep", "[Honeypot] 🌙 **Some Server** went dormant"]
    assert asyncio.run(main._collect_relayed()) == []      # taken exactly once


def test_nothing_pending_writes_nothing_and_bad_rows_are_skipped(_clean):
    clone = _cog(7, "Honeypot")
    asyncio.run(clone._relay_loop.coro(clone))
    assert _clean.rows == {}
    _clean.rows["sleep_events:9:1"] = "not json"
    _clean.rows["sleep_events:9:2"] = json.dumps(["ok line"])
    assert asyncio.run(_cog(None, "PRIME")._collect_relayed()) == ["ok line"]
