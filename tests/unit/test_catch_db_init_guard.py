"""A failing catch schema must never stop the bot starting (database._create_catch_tables)."""
import asyncio
import logging
from contextlib import asynccontextmanager

import database
from modules import catch_schema


class FakeConn:
    def __init__(self):
        self.events = []

    @asynccontextmanager
    async def transaction(self):
        self.events.append("savepoint")
        try:
            yield
        except Exception:
            self.events.append("rollback")
            raise
        else:
            self.events.append("release")


def make_db():
    return database.Database.__new__(database.Database)


def test_catch_schema_failure_is_logged_not_raised_and_rolled_back(monkeypatch, caplog):
    async def boom(conn):
        raise RuntimeError("syntax error in catch DDL")

    monkeypatch.setattr(catch_schema, "create_tables", boom)
    conn = FakeConn()
    with caplog.at_level(logging.ERROR, logger=database.logger.name):
        asyncio.run(make_db()._create_catch_tables(conn))
    assert conn.events == ["savepoint", "rollback"]
    assert any("Catch game schema failed" in r.getMessage() and r.exc_info for r in caplog.records)


def test_catch_schema_success_runs_inside_a_savepoint(monkeypatch):
    calls = []

    async def ok(conn):
        calls.append(conn)

    monkeypatch.setattr(catch_schema, "create_tables", ok)
    conn = FakeConn()
    asyncio.run(make_db()._create_catch_tables(conn))
    assert calls == [conn] and conn.events == ["savepoint", "release"]


def test_create_tables_goes_through_the_guarded_helper():
    import inspect
    src = inspect.getsource(database.Database._create_tables)
    assert "await self._create_catch_tables(conn)" in src
    assert "catch_schema.create_tables" not in src
