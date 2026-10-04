"""Real-Postgres smoke test for the catch game (handoff section 7A.1).

Skipped unless CATCH_TEST_DSN points at a DISPOSABLE database: the test drops and
recreates every catch_* table first. Example:

    CATCH_TEST_DSN=postgresql://postgres:smoke@localhost:5432/smoke \
        python -m pytest -q tests/integration/test_catch_postgres_smoke.py
"""
import asyncio
import os
import random

import pytest

DSN = os.environ.get("CATCH_TEST_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="CATCH_TEST_DSN not set")

U1, U2, U3 = 9001, 9002, 9003
GUILD, CHANNEL = 5001, 6001


def run_with_pool(scenario):
    """Fresh pool + fresh schema per scenario, then call ``scenario(pool)``."""
    import asyncpg

    from modules import catch_db, catch_gate, catch_schema, catch_species

    async def main():
        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=8)
        try:
            async with pool.acquire() as conn:
                tables = await conn.fetch("SELECT tablename FROM pg_tables WHERE tablename LIKE 'catch\\_%'")
                for t in tables:
                    await conn.execute(f'DROP TABLE IF EXISTS "{t["tablename"]}" CASCADE')
                # main-bot table that sync_to_db reads (defined in database.py)
                await conn.execute(
                    "CREATE TABLE IF NOT EXISTS admin_config (key TEXT PRIMARY KEY, value TEXT NOT NULL, "
                    "updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
                )
                await conn.execute("DELETE FROM admin_config")
                await catch_schema.create_tables(conn)

            async def fake_get_pool():
                return pool

            orig = catch_db.get_pool
            catch_db.get_pool = fake_get_pool
            catch_gate.invalidate()
            catch_species.invalidate_cache()
            try:
                await scenario(pool)
            finally:
                catch_db.get_pool = orig
        finally:
            await pool.close()

    asyncio.run(main())


def test_schema_and_species_sync_are_idempotent():
    from modules import catch_schema, catch_species

    async def scenario(pool):
        async with pool.acquire() as conn:
            await catch_schema.create_tables(conn)  # second run
            first = await catch_species.sync_to_db(conn)
            await catch_species.sync_to_db(conn, force=True)
            count = await conn.fetchval("SELECT count(*) FROM catch_species")
        assert count == len(catch_species.all_species()) == 48
        assert first is not None

    run_with_pool(scenario)


def test_starter_kit_and_daily_pay_once():
    from modules import catch_items, catch_species

    async def scenario(pool):
        async with pool.acquire() as conn:
            await catch_species.sync_to_db(conn)
        assert await catch_items.ensure_starter_kit(U1, None) is True
        assert await catch_items.ensure_starter_kit(U1, None) is False
        inv = await catch_items.load_inventory(U1, None)
        assert inv["capsule_basic"] == catch_items.STARTER_KIT["capsule_basic"]

        first = await catch_items.claim_daily(U1, None)
        second = await catch_items.claim_daily(U1, None)
        assert first.claimed is True and second.claimed is False
        player = await catch_items.load_player(U1, None)
        assert player.coins == first.reward.coins
        # concurrent double tap pays once
        results = await asyncio.gather(*(catch_items.claim_daily(U2, None) for _ in range(5)))
        assert sum(1 for r in results if r.claimed) == 1

    run_with_pool(scenario)


async def _make_spawn(pool, **kwargs):
    from modules import catch_species
    from modules.catch_spawn import create_spawn, roll_spawn

    roll = roll_spawn(list(catch_species.all_species().values()), random.Random(1))
    spawn_id = await create_spawn(guild_id=GUILD, clone_id=None, channel_id=CHANNEL, roll=roll, **kwargs)
    return spawn_id, roll


def test_record_catch_from_spawn_and_concurrent_race():
    from modules import catch_items, catch_species
    from modules.catch_service import record_catch

    async def scenario(pool):
        async with pool.acquire() as conn:
            await catch_species.sync_to_db(conn)
        for u in (U1, U2, U3):
            await catch_items.ensure_starter_kit(u, None)
        before = (await catch_items.load_inventory(U1, None))["capsule_basic"]

        spawn_id, roll = await _make_spawn(pool)
        res = await record_catch(user_id=U1, clone_id=None, guild_id=GUILD, source="wild", spawn_id=spawn_id)
        # a failed throw may still consume the ball; success must record rows
        after = (await catch_items.load_inventory(U1, None))["capsule_basic"]
        assert after == before - 1
        async with pool.acquire() as conn:
            owned = await conn.fetchval("SELECT count(*) FROM catch_owned WHERE user_id=$1", U1)
            audit = await conn.fetchval("SELECT count(*) FROM catch_audit WHERE user_id=$1", U1)
            dex = await conn.fetchval("SELECT count(*) FROM catch_dex WHERE user_id=$1", U1) if await conn.fetchval(
                "SELECT to_regclass('catch_dex')") else None
        assert audit >= 1
        if res.claimed:
            assert owned == 1 and res.species_id == roll.species_id
            if dex is not None:
                assert dex >= 1

        # race: two users, one spawn -> at most one claim, never two owned rows
        spawn2, _ = await _make_spawn(pool)
        outcomes = await asyncio.gather(
            record_catch(user_id=U2, clone_id=None, guild_id=GUILD, source="wild", spawn_id=spawn2),
            record_catch(user_id=U3, clone_id=None, guild_id=GUILD, source="wild", spawn_id=spawn2),
            return_exceptions=True,
        )
        winners = [o for o in outcomes if not isinstance(o, Exception) and o.claimed]
        assert len(winners) <= 1, outcomes
        async with pool.acquire() as conn:
            caught_by = await conn.fetchval("SELECT caught_by FROM catch_spawns WHERE id=$1", spawn2)
            owned2 = await conn.fetchval(
                "SELECT count(*) FROM catch_owned WHERE user_id = ANY($1::bigint[])", [U2, U3]
            )
        assert owned2 == len(winners)
        assert (caught_by is not None) == (len(winners) == 1)

    run_with_pool(scenario)


def test_collection_favourite_ownership_and_sorts():
    from modules import catch_collection, catch_species
    from modules.catch_service import record_catch

    async def scenario(pool):
        async with pool.acquire() as conn:
            await catch_species.sync_to_db(conn)
        owned_ids = []
        for i in range(4):
            res = await record_catch(
                user_id=U1, clone_id=None, guild_id=GUILD, source="giveaway",
                species_id=(i % 3) + 1, level=5 + i * 7, ivs=[10] * 5, idem_key=f"smoke-{i}",
            )
            assert res.claimed
            owned_ids.append(res.owned_id)
        # same idem_key replays instead of granting twice
        again = await record_catch(
            user_id=U1, clone_id=None, guild_id=GUILD, source="giveaway",
            species_id=1, level=5, ivs=[10] * 5, idem_key="smoke-0",
        )
        assert again.replay is True and again.owned_id == owned_ids[0]

        assert await catch_collection.set_favorite(owned_ids[0], U2, None) is None  # non-owner
        assert await catch_collection.set_favorite(owned_ids[0], U1, None) is True
        for sort in catch_collection.SORTS:
            rows, total, page = await catch_collection.list_owned(U1, None, sort=sort)
            assert total == len(owned_ids) and len(rows) == total and page == 0
        rows, _, _ = await catch_collection.list_owned(U1, None, sort="level")
        assert [r.level for r in rows] == sorted((r.level for r in rows), reverse=True)
        rows, _, _ = await catch_collection.list_owned(U1, None, sort="favorites")
        assert rows[0].favorite is True

    run_with_pool(scenario)
