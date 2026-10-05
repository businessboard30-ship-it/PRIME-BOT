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


def test_wild_zone_lists_only_live_visible_spawns_in_this_guild():
    from modules import catch_species, catch_wild

    async def scenario(pool):
        async with pool.acquire() as conn:
            await catch_species.sync_to_db(conn)
            sid = (await conn.fetchval("SELECT id FROM catch_species ORDER BY id LIMIT 1"))

            async def add(guild, *, owner=None, caught_by=None, fled=False, expires=3600, clone_id=None):
                return await conn.fetchval(
                    "INSERT INTO catch_spawns (guild_id, clone_id, channel_id, message_id, species_id, level, ivs, "
                    "owner_user_id, caught_by, fled, expires_at) VALUES ($1,$2,6,NULL,$3,5,ARRAY[1,1,1,1,1],$4,$5,$6, now() + make_interval(secs => $7)) RETURNING id",
                    guild, clone_id, sid, owner, caught_by, fled, float(expires),
                )

            visible = await add(GUILD)
            mine = await add(GUILD, owner=U1, expires=7200)
            await add(GUILD, owner=U2)                    # someone else's personal spawn
            await add(GUILD, caught_by=U2)                # already caught
            await add(GUILD, fled=True)                   # fled
            await add(GUILD, expires=-60)                 # expired
            await add(GUILD + 1)                          # another server
            await add(GUILD, clone_id=3)                  # another bot clone
        spawns, total = await catch_wild.list_active_spawns(GUILD, U1, None)
        assert total == 2 and [s.id for s in spawns] == [visible, mine]
        assert [s.personal for s in spawns] == [False, True]
        spawns, total = await catch_wild.list_active_spawns(GUILD, U3, None)
        assert total == 1 and [s.id for s in spawns] == [visible]
        capped, total = await catch_wild.list_active_spawns(GUILD, U1, None, limit=1)
        assert len(capped) == 1 and total == 2

    run_with_pool(scenario)


def test_status_reads_only_this_player_and_clone_and_writes_nothing():
    from modules import catch_status, catch_species

    async def scenario(pool):
        async with pool.acquire() as conn:
            await catch_species.sync_to_db(conn)
            sid = await conn.fetchval("SELECT id FROM catch_species ORDER BY id LIMIT 1")

            async def player(user, clone, coins):
                await conn.execute(
                    "INSERT INTO catch_players (user_id, clone_id, coins, total_catches, catch_streak, best_streak, daily_streak) "
                    "VALUES ($1,$2,$3,4,2,6,3)", user, clone, coins)

            async def own(user, clone, *, shiny=False, favorite=False):
                await conn.execute(
                    "INSERT INTO catch_owned (user_id, clone_id, species_id, level, shiny, favorite, ivs, source) "
                    "VALUES ($1,$2,$3,5,$4,$5,ARRAY[1,1,1,1,1],1)", user, clone, sid, shiny, favorite)

            await player(U1, None, 250)
            await player(U2, None, 9999)          # another player
            await player(U1, 3, 7777)             # same user on another clone
            await own(U1, None)
            await own(U1, None, shiny=True, favorite=True)
            await own(U2, None, shiny=True)       # another player's creatures
            await own(U1, 3)                      # same user, another clone
            await conn.execute(
                "INSERT INTO catch_dex (user_id, clone_id, species_id, caught_count) VALUES ($1,NULL,$2,1)", U1, sid)
            await conn.execute(
                "INSERT INTO catch_dex (user_id, clone_id, species_id, caught_count) VALUES ($1,NULL,$2,1)", U2, sid)
            before = await conn.fetchval("SELECT count(*) FROM catch_audit")
        result = await catch_status.load_status(U1, None)
        assert (result.coins, result.total_catches, result.best_streak, result.daily_streak) == (250, 4, 6, 3)
        assert (result.owned, result.shinies, result.favourites) == (2, 1, 1)
        assert result.dex_caught == 1 and result.dex_total >= 1
        other_clone = await catch_status.load_status(U1, 3)
        assert (other_clone.coins, other_clone.owned) == (7777, 1)
        stranger = await catch_status.load_status(U3, None)
        assert (stranger.coins, stranger.owned, stranger.dex_caught) == (0, 0, 0)
        async with pool.acquire() as conn:
            assert await conn.fetchval("SELECT count(*) FROM catch_audit") == before
            assert await conn.fetchval("SELECT count(*) FROM catch_players WHERE user_id=$1", U3) == 0

    run_with_pool(scenario)


def test_failed_catch_schema_does_not_break_the_surrounding_migration_transaction():
    import database
    from modules import catch_schema

    async def scenario(pool):
        async def bad_create_tables(conn):
            await conn.execute("CREATE TABLE catch_guard_partial (id INT)")   # rolled back with the savepoint
            await conn.execute("THIS IS NOT SQL")

        original = catch_schema.create_tables
        catch_schema.create_tables = bad_create_tables
        try:
            async with pool.acquire() as conn:
                async with conn.transaction():
                    await conn.execute("CREATE TABLE catch_guard_before (id INT)")
                    await database.Database.__new__(database.Database)._create_catch_tables(conn)
                    await conn.execute("CREATE TABLE catch_guard_after (id INT)")   # must still work
        finally:
            catch_schema.create_tables = original
        async with pool.acquire() as conn:
            names = {r["tablename"] for r in await conn.fetch("SELECT tablename FROM pg_tables WHERE tablename LIKE 'catch_guard%'")}
            await conn.execute("DROP TABLE IF EXISTS catch_guard_before, catch_guard_after")
        assert names == {"catch_guard_before", "catch_guard_after"}

    run_with_pool(scenario)


async def _xp_setup(pool, user=U1, key="xp-a", level=5, species_rarity="common"):
    """Sync species, make one creature for ``user`` and return its owned id."""
    from modules import catch_service, catch_species

    async with pool.acquire() as conn:
        await catch_species.sync_to_db(conn)
    sid = next(i for i, sp in catch_species.all_species().items() if sp["rarity"] == species_rarity)
    await catch_service.record_catch(
        user_id=user, clone_id=None, guild_id=None, source="wild", species_id=sid, level=level,
        ivs=[10, 10, 10, 10, 10], idem_key=key,
    )
    async with pool.acquire() as conn:
        return await conn.fetchval("SELECT id FROM catch_owned WHERE idem_key = $1", key)


async def _creature(pool, owned_id):
    async with pool.acquire() as conn:
        return await conn.fetchrow("SELECT level, xp FROM catch_owned WHERE id = $1", owned_id)


def test_xp_levels_up_audits_and_replays_without_double_paying():
    from modules import catch_xp

    async def scenario(pool):
        oid = await _xp_setup(pool)
        need = catch_xp.xp_to_next(5)
        first = await catch_xp.grant_xp(oid, U1, None, source="catch", amount=30, idem_key="g1")
        assert first.ok and first.gained == 30 and first.level_after >= 5
        row = await _creature(pool, oid)
        expected = catch_xp.apply_xp(5, 0, 30)
        assert (row["level"], row["xp"]) == expected
        again = await catch_xp.grant_xp(oid, U1, None, source="catch", amount=30, idem_key="g1")
        assert again.ok and again.replay and again.gained == 30
        assert (await _creature(pool, oid))["xp"] == row["xp"]
        async with pool.acquire() as conn:
            n = await conn.fetchval("SELECT count(*) FROM catch_audit WHERE action = 'xp' AND ref_id = $1", oid)
        assert n == 1
        big = await catch_xp.grant_xp(oid, U1, None, source="catch", amount=40, idem_key="g2")
        assert big.ok and need > 0

    run_with_pool(scenario)


def test_xp_cannot_touch_someone_elses_creature_or_other_clone():
    from modules import catch_xp

    async def scenario(pool):
        mine = await _xp_setup(pool, U1, "xp-mine")
        theirs = await _xp_setup(pool, U2, "xp-theirs")
        stolen = await catch_xp.grant_xp(theirs, U1, None, source="catch", amount=20, idem_key="steal")
        assert not stolen.ok and stolen.reason == "not_found"
        assert (await _creature(pool, theirs))["xp"] == 0
        async with pool.acquire() as conn:  # the same player exists in another clone
            await conn.execute("INSERT INTO catch_players (user_id, clone_id) VALUES ($1, 7)", U1)
        other_clone = await catch_xp.grant_xp(mine, U1, 7, source="catch", amount=20, idem_key="clone")
        assert not other_clone.ok and other_clone.reason == "not_found"
        assert (await _creature(pool, mine))["xp"] == 0

    run_with_pool(scenario)


def test_xp_daily_budget_cuts_then_refuses():
    from modules import catch_xp

    async def scenario(pool):
        oid = await _xp_setup(pool, level=1)
        per_grant, per_day = catch_xp.SOURCES["daily"]
        got = 0
        for i in range(per_day // per_grant):
            r = await catch_xp.grant_xp(oid, U1, None, source="daily", amount=per_grant, idem_key=f"d{i}")
            assert r.ok
            got += r.gained
        assert got == per_day
        over = await catch_xp.grant_xp(oid, U1, None, source="daily", amount=5, idem_key="d-over")
        assert not over.ok and over.reason == "capped"
        # another source has its own budget
        other = await catch_xp.grant_xp(oid, U1, None, source="buddy", amount=5, idem_key="b0")
        assert other.ok

    run_with_pool(scenario)


def test_xp_max_level_refuses_and_stays_at_cap():
    from modules import catch_xp
    from modules.catch_game import LEVEL_MAX

    async def scenario(pool):
        oid = await _xp_setup(pool, level=LEVEL_MAX - 1)
        r = await catch_xp.grant_xp(oid, U1, None, source="catch", amount=40, idem_key="m1")
        assert r.ok
        # push to the cap directly, then confirm further grants are refused
        async with pool.acquire() as conn:
            await conn.execute("UPDATE catch_owned SET level = $2, xp = 0 WHERE id = $1", oid, LEVEL_MAX)
        r2 = await catch_xp.grant_xp(oid, U1, None, source="catch", amount=40, idem_key="m2")
        assert not r2.ok and r2.reason == "max_level"
        row = await _creature(pool, oid)
        assert (row["level"], row["xp"]) == (LEVEL_MAX, 0)

    run_with_pool(scenario)


def test_xp_concurrent_same_key_pays_once():
    from modules import catch_xp

    async def scenario(pool):
        oid = await _xp_setup(pool, level=3)
        holder = await pool.acquire()
        tx = holder.transaction()
        await tx.start()
        await holder.fetchval("SELECT 1 FROM catch_players WHERE user_id = $1 FOR UPDATE", U1)
        tasks = [
            asyncio.create_task(catch_xp.grant_xp(oid, U1, None, source="catch", amount=25, idem_key="same"))
            for _ in range(6)
        ]
        await asyncio.sleep(0.5)
        assert not any(t.done() for t in tasks), "grants should be waiting on the player lock"
        await tx.commit()
        await pool.release(holder)
        results = await asyncio.wait_for(asyncio.gather(*tasks), timeout=30)
        assert all(r.ok for r in results)
        assert sum(1 for r in results if not r.replay) == 1
        assert (await _creature(pool, oid))["xp"] == catch_xp.apply_xp(3, 0, 25)[1]
        async with pool.acquire() as conn:
            assert await conn.fetchval("SELECT count(*) FROM catch_audit WHERE action = 'xp' AND ref_id = $1", oid) == 1

    run_with_pool(scenario)


def test_buddy_catch_xp_goes_to_buddy_only_and_never_twice():
    from modules import catch_xp

    async def scenario(pool):
        buddy = await _xp_setup(pool, U1, "bd-a", level=4)
        none_yet = await catch_xp.grant_buddy_catch_xp(U1, None, caught_owned_id=111)
        assert none_yet is None
        theirs = await _xp_setup(pool, U2, "bd-b", level=4)
        async with pool.acquire() as conn:
            await conn.execute("UPDATE catch_players SET buddy_id = $2 WHERE user_id = $1", U1, theirs)
        foreign = await catch_xp.grant_buddy_catch_xp(U1, None, caught_owned_id=112)
        assert foreign is None  # a buddy id that is not the player's own is ignored
        assert (await _creature(pool, theirs))["xp"] == 0
        async with pool.acquire() as conn:
            await conn.execute("UPDATE catch_players SET buddy_id = $2 WHERE user_id = $1", U1, buddy)
        r = await catch_xp.grant_buddy_catch_xp(U1, None, caught_owned_id=113, new_species=True)
        assert r.ok and r.gained == catch_xp.BUDDY_CATCH_XP + catch_xp.BUDDY_NEW_SPECIES_XP
        again = await catch_xp.grant_buddy_catch_xp(U1, None, caught_owned_id=113, new_species=True)
        assert again.replay and (await _creature(pool, buddy))["xp"] == catch_xp.apply_xp(4, 0, r.gained)[1]

    run_with_pool(scenario)
