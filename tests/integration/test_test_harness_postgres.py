"""Test harness runs table + Database.th_* methods against REAL Postgres (the DDL is read straight out of database.py so
this tests what ships), plus one end-to-end arm -> damage -> stop run through the real methods."""
import asyncio
import json
import pathlib
import re
import tempfile
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

pgserver = pytest.importorskip("pgserver")
asyncpg = pytest.importorskip("asyncpg")

import config
import database
from discord_bot.cogs import test_harness as cogmod
from discord_bot.cogs.test_harness import TestHarnessCog
from tests.unit._fake_guild import FakeGuild
from tests.unit.test_test_harness_runs import Inter

SRC = (pathlib.Path(__file__).resolve().parents[2] / "database.py").read_text()


def _ddl() -> list:
    table = re.search(r'CREATE TABLE IF NOT EXISTS test_harness_runs \(.*?\n        """', SRC, re.S).group(0)[:-len('"""')]
    index = re.search(r'CREATE UNIQUE INDEX IF NOT EXISTS ux_test_harness_one_active.*?\n        """', SRC, re.S).group(0)[:-len('"""')]
    return [table, index]


@pytest.fixture(scope="module")
def pg_uri():
    return pgserver.get_server(tempfile.mkdtemp()).get_uri()


@pytest_asyncio.fixture
async def db(pg_uri, monkeypatch):
    pool = await asyncpg.create_pool(pg_uri, min_size=2, max_size=10)
    async with pool.acquire() as c:
        await c.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        for stmt in _ddl():
            await c.execute(stmt)

    async def _p():
        return pool
    monkeypatch.setattr(database, "get_pool", _p)
    yield database.db
    await pool.close()


def soon(minutes=15):
    return datetime.now(timezone.utc) + timedelta(minutes=minutes)


@pytest.mark.asyncio
async def test_create_get_and_one_unfinished_run_per_guild(db):
    a = await db.th_create_run(1, 42, '{"x":1}', "h", soon(), 99)
    assert a and await db.th_create_run(1, 42, "{}", "h2", soon()) is None       # same guild: refused by the unique index
    assert await db.th_create_run(2, 42, "{}", "h3", soon())                      # another guild: fine
    row = await db.th_get_run(a)
    assert row["status"] == "armed" and row["snapshot_json"] == '{"x":1}' and row["report_channel_id"] == 99
    assert row["deadline_at"].tzinfo is not None and await db.th_get_run(9999) is None


@pytest.mark.asyncio
async def test_finishing_frees_the_guild_and_records_the_result(db):
    a = await db.th_create_run(1, 42, "{}", "h", soon())
    await db.th_finish_run(a, "restored", '{"ok":true}')
    row = await db.th_get_run(a)
    assert row["status"] == "restored" and row["finished_at"] is not None and json.loads(row["result_json"]) == {"ok": True}
    assert await db.th_create_run(1, 42, "{}", "h", soon())                       # a finished run no longer blocks


@pytest.mark.asyncio
async def test_claim_is_atomic_only_one_caller_wins(db):
    b = await db.th_create_run(2, 42, "{}", "h", soon())
    wins = await asyncio.gather(*[db.th_claim_run(b, ["armed"], "restoring") for _ in range(8)])
    assert wins.count(True) == 1 and wins.count(False) == 7                        # eight racers, exactly one winner
    assert (await db.th_get_run(b))["status"] == "restoring"
    assert await db.th_claim_run(9999, ["armed"], "restoring") is False


@pytest.mark.asyncio
async def test_unfinished_and_latest_queries(db):
    a = await db.th_create_run(1, 42, "{}", "h", soon())
    b = await db.th_create_run(2, 42, "{}", "h", soon())
    await db.th_finish_run(b, "restored")
    assert [r["id"] for r in await db.th_unfinished_runs()] == [a]
    assert [r["id"] for r in await db.th_unfinished_runs(1)] == [a] and await db.th_unfinished_runs(2) == []
    c = await db.th_create_run(2, 42, "{}", "h", soon())
    assert [r["id"] for r in await db.th_unfinished_runs()] == [a, c]
    assert (await db.th_latest_run(2))["id"] == c and await db.th_latest_run(3) is None
    await db.th_claim_run(a, ["armed"], "restoring")
    assert {r["status"] for r in await db.th_unfinished_runs(1)} == {"restoring"}   # restoring still counts as unfinished


@pytest.mark.asyncio
async def test_end_to_end_arm_damage_stop_against_real_postgres(db, monkeypatch):
    g = FakeGuild()
    monkeypatch.setattr(config, "TEST_GUILD_ID", g.id)
    monkeypatch.setattr(config, "DISCORD_CLONE_ADMIN_IDS", {42})
    monkeypatch.setattr(cogmod.modx, "log_action", AsyncMock())
    monkeypatch.setattr(cogmod, "RESTORE_PAUSE", 0)
    cat = g.add_channel("CHAT", "category")
    gen = g.add_channel("general", "text", cat, topic="hello")
    g.add_channel("staff", "text", cat)
    cog = TestHarnessCog(SimpleNamespace(clone_id=None, get_guild=lambda gid: g, wait_until_ready=AsyncMock()))
    i = Inter(g, gen, user_id=42)
    await cog.ai_snapshot(i, minutes=5)
    assert (await db.th_latest_run(g.id))["status"] == "armed"
    for c in [c for c in g.channels if c.type.name != "category"]:
        await c.delete()
    g.add_channel("raid", "text")
    await TestHarnessCog.harness_stop.callback(cog, Inter(g, gen, user_id=42))
    last = await db.th_latest_run(g.id)
    assert last["status"] == "restored"
    assert sorted(c.name for c in g.channels) == ["CHAT", "general", "staff"]
