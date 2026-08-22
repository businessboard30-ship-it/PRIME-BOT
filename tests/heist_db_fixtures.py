"""
Shared fixtures for Heist Wars integration/security tests.

Requires a real Postgres reachable at TEST_DATABASE_URL (falls back to
DATABASE_URL). Tests using this fixture are skipped automatically if no
database is reachable — they are not meant to run against mocks, since the
whole point of the concurrency/idempotency tests is exercising real
Postgres transaction semantics.
"""

from __future__ import annotations

import os
import pathlib

import asyncpg
import pytest
import pytest_asyncio

MIGRATION_PATH = pathlib.Path(__file__).resolve().parent.parent / "database" / "migrations" / "001_heist_wars.sql"

TEST_DB_URL = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL", "")


def _db_available() -> bool:
    return bool(TEST_DB_URL) and "test:test@localhost" not in TEST_DB_URL


requires_db = pytest.mark.skipif(not _db_available(), reason="requires a real TEST_DATABASE_URL Postgres instance")


@pytest_asyncio.fixture
async def db_pool():
    pool = await asyncpg.create_pool(TEST_DB_URL, min_size=1, max_size=5)
    async with pool.acquire() as conn:
        await conn.execute(MIGRATION_PATH.read_text())
        # Seed a fake economy row set so heist_service._grant_cash has a
        # table to write to even in a bare test database.
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS discord_economy_balances (
                guild_id BIGINT NOT NULL, clone_id INTEGER, user_id BIGINT NOT NULL,
                balance BIGINT NOT NULL DEFAULT 0
            );
            CREATE UNIQUE INDEX IF NOT EXISTS test_econ_bal_key
                ON discord_economy_balances (guild_id, COALESCE(clone_id, -1), user_id);
            CREATE TABLE IF NOT EXISTS discord_economy_transactions (
                id SERIAL PRIMARY KEY, guild_id BIGINT NOT NULL, clone_id INTEGER,
                user_id BIGINT NOT NULL, amount BIGINT NOT NULL, reason TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
        """)
    yield pool
    async with pool.acquire() as conn:
        await conn.execute("""
            TRUNCATE heist_reward_ledger, heist_events_log, heist_participants,
                     heist_runs, heist_players, discord_economy_transactions,
                     discord_economy_balances RESTART IDENTITY CASCADE
        """)
    await pool.close()


@pytest_asyncio.fixture
async def patched_pool(db_pool, monkeypatch):
    """Points game.heist_service / database.get_pool at the test pool."""
    import database

    async def _get_pool():
        return db_pool

    monkeypatch.setattr(database, "get_pool", _get_pool)
    from game import heist_service
    monkeypatch.setattr(heist_service, "get_pool", _get_pool)
    return db_pool
