"""Shared DB access for the catch game modules.

Every catch helper takes an optional ``conn``. Passing one lets a caller run
several helpers inside one transaction (``conn.transaction()`` nests as a
savepoint); omitting it acquires a pooled connection. Tests replace
``get_pool`` with a pool on a disposable database.
"""

from __future__ import annotations

from contextlib import asynccontextmanager


async def get_pool():
    from database import (
        get_pool as _real_get_pool,  # lazy: keeps module importable in tests
    )

    return await _real_get_pool()


@asynccontextmanager
async def transaction(conn=None):
    if conn is not None:
        async with conn.transaction():
            yield conn
        return
    pool = await get_pool()
    async with pool.acquire() as acquired, acquired.transaction():
        yield acquired


@asynccontextmanager
async def connection(conn=None):
    if conn is not None:
        yield conn
        return
    pool = await get_pool()
    async with pool.acquire() as acquired:
        yield acquired


def clone_key(clone_id: int | None) -> int:
    """Value used in unique indexes so the main bot (NULL) is its own scope."""
    return -1 if clone_id is None else clone_id
