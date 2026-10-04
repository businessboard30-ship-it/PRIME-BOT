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


def test_shop_concurrent_purchases_never_overspend_and_roll_back_on_failure():
    import pytest

    from modules import catch_items, catch_shop

    async def scenario(pool):
        # one daily claim gives the player a known balance
        daily = await catch_items.claim_daily(U1, None)
        coins = daily.reward.coins
        price = catch_shop.CATALOG["capsule_basic"]
        affordable = coins // price
        assert affordable >= 1, "test needs a balance that can afford at least one capsule"
        before = (await catch_items.load_inventory(U1, None)).get("capsule_basic", 0)

        # a failure after the coins were taken rolls the whole purchase back (player CAN afford it)
        original = catch_shop.grant_items

        async def broken(*args, **kwargs):
            raise RuntimeError("grant failed")

        catch_shop.grant_items = broken
        try:
            with pytest.raises(RuntimeError):
                await catch_shop.purchase(U1, None, "capsule_basic", 1)
        finally:
            catch_shop.grant_items = original
        assert (await catch_shop.load_wallet(U1, None)).coins == coins
        assert (await catch_items.load_inventory(U1, None)).get("capsule_basic", 0) == before
        async with pool.acquire() as conn:
            assert await conn.fetchval("SELECT count(*) FROM catch_audit WHERE user_id=$1 AND action='shop_purchase'", U1) == 0

        # concurrent purchases: exactly as many succeed as the balance affords
        results = await asyncio.gather(*(catch_shop.purchase(U1, None, "capsule_basic", 1) for _ in range(affordable + 4)))
        assert sum(r.ok for r in results) == affordable
        assert all(r.reason == "insufficient_coins" for r in results if not r.ok)
        wallet = await catch_shop.load_wallet(U1, None)
        assert wallet.coins == coins - affordable * price >= 0
        assert (await catch_items.load_inventory(U1, None)).get("capsule_basic", 0) - before == affordable
        async with pool.acquire() as conn:
            audits = await conn.fetchval("SELECT count(*) FROM catch_audit WHERE user_id=$1 AND action='shop_purchase'", U1)
        assert audits == affordable
        spent = [e.coins for e in wallet.entries if e.action == "shop_purchase"]
        assert spent == [-price] * affordable

        # a refused purchase changes nothing
        poor = await catch_shop.purchase(U2, None, "capsule_prime", 10)
        assert (poor.ok, poor.reason, poor.coins_left) == (False, "insufficient_coins", 0)

    run_with_pool(scenario)


def test_sell_concurrent_taps_pay_once_and_protect_favourites_and_other_players():
    from modules import catch_items, catch_service, catch_sell, catch_shop, catch_species

    async def scenario(pool):
        async with pool.acquire() as conn:
            await catch_species.sync_to_db(conn)
        await catch_items.claim_daily(U1, None)
        await catch_items.claim_daily(U2, None)
        coins_before = (await catch_shop.load_wallet(U1, None)).coins
        species = catch_sell.all_species()
        sid = next(i for i, sp in species.items() if sp["rarity"] == "rare")
        ivs = [10, 10, 10, 10, 10]

        async def make(user, key):
            return await catch_service.record_catch(
                user_id=user, clone_id=None, guild_id=None, source="wild", species_id=sid, level=10, ivs=ivs, idem_key=key,
            )

        a = await make(U1, "sell-a")
        keep = await make(U1, "sell-fav")
        lock = await make(U1, "sell-lock")
        theirs = await make(U2, "sell-other")
        ids = {n: (await _owned_id(pool, k)) for n, k in (("a", "sell-a"), ("fav", "sell-fav"), ("lock", "sell-lock"), ("theirs", "sell-other"))}
        assert a and keep and lock and theirs
        async with pool.acquire() as conn:
            await conn.execute("UPDATE catch_owned SET favorite = TRUE WHERE id = $1", ids["fav"])
            await conn.execute("UPDATE catch_owned SET locked = TRUE WHERE id = $1", ids["lock"])
        value = catch_sell.sale_value("rare")

        # 6 taps that genuinely overlap: a holder keeps the creature row locked while all six
        # sells start, then releases it, so every sell is in flight at the same moment
        holder = await pool.acquire()
        tx = holder.transaction()
        await tx.start()
        await holder.fetchval("SELECT id FROM catch_owned WHERE id = $1 FOR UPDATE", ids["a"])
        pending = [asyncio.create_task(catch_sell.sell_creature(ids["a"], U1, None)) for _ in range(6)]
        await asyncio.sleep(0.5)
        assert not any(t.done() for t in pending), "sells should be waiting on the row lock"
        await tx.commit()
        await pool.release(holder)
        results = await asyncio.gather(*pending)
        assert sum(r.ok for r in results) == 1
        assert all(r.reason == "not_found" for r in results if not r.ok)
        assert (await catch_shop.load_wallet(U1, None)).coins == coins_before + value
        async with pool.acquire() as conn:
            assert await conn.fetchval("SELECT count(*) FROM catch_owned WHERE id = $1", ids["a"]) == 0
            assert await conn.fetchval("SELECT count(*) FROM catch_audit WHERE user_id=$1 AND action='sell'", U1) == 1

        # favourite, locked and someone else's creature are refused and nothing changes
        assert (await catch_sell.sell_creature(ids["fav"], U1, None)).reason == "favorite"
        assert (await catch_sell.sell_creature(ids["lock"], U1, None)).reason == "locked"
        assert (await catch_sell.sell_creature(ids["theirs"], U1, None)).reason == "not_found"
        assert (await catch_sell.sell_creature(ids["a"], U1, 3)).reason == "not_found"  # wrong clone scope
        async with pool.acquire() as conn:
            assert await conn.fetchval("SELECT count(*) FROM catch_owned WHERE id = ANY($1::bigint[])", [ids["fav"], ids["lock"], ids["theirs"]]) == 3
        assert (await catch_shop.load_wallet(U1, None)).coins == coins_before + value

        # the sellable list hides favourites and locked creatures
        rows, total, _ = await catch_sell.list_sellable(U1, None)
        assert rows == [] and total == 0

        # the sale shows in the wallet as a positive line
        wallet = await catch_shop.load_wallet(U1, None)
        assert [e.coins for e in wallet.entries if e.action == "sell"] == [value]

        # a missing player row rolls the sale back: the creature is not deleted
        async with pool.acquire() as conn:
            await conn.execute("UPDATE catch_owned SET favorite = FALSE WHERE id = $1", ids["fav"])
            await conn.execute("DELETE FROM catch_players WHERE user_id = $1", U1)
        with pytest.raises(RuntimeError):
            await catch_sell.sell_creature(ids["fav"], U1, None)
        async with pool.acquire() as conn:
            assert await conn.fetchval("SELECT count(*) FROM catch_owned WHERE id = $1", ids["fav"]) == 1

    async def _owned_id(pool, key):
        async with pool.acquire() as conn:
            return await conn.fetchval("SELECT id FROM catch_owned WHERE idem_key = $1", key)

    run_with_pool(scenario)
