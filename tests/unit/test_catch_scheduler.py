import asyncio

from modules.catch_scheduler import purge_spawn_history


def test_purge_rejects_invalid_retention():
    async def run():
        try:
            await purge_spawn_history(retention_days=0)
        except ValueError as exc:
            assert "positive" in str(exc)
        else:
            raise AssertionError("expected ValueError")

    asyncio.run(run())


def test_zero_limits_are_noops():
    from modules.catch_scheduler import claim_expired_spawns

    async def run():
        assert await claim_expired_spawns(limit=0) == ()
        assert await purge_spawn_history(limit=0) == ()

    asyncio.run(run())
