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


def test_scheduler_batch_preserves_expired_and_purged_ids(monkeypatch):
    import modules.catch_scheduler as scheduler

    async def fake_claim(*, limit, conn):
        assert limit == 3
        assert conn == "connection"
        return ({"id": 11}, {"id": 12})

    async def fake_purge(*, retention_days, limit, conn):
        assert retention_days == 14
        assert limit == 5
        assert conn == "connection"
        return (21, 22)

    monkeypatch.setattr(scheduler, "claim_expired_spawns", fake_claim)
    monkeypatch.setattr(scheduler, "purge_spawn_history", fake_purge)

    async def run():
        batch = await scheduler.run_scheduler_batch(
            expire_limit=3,
            purge_limit=5,
            retention_days=14,
            conn="connection",
        )
        assert batch.expired_rows == ({"id": 11}, {"id": 12})
        assert batch.expired_ids == (11, 12)
        assert batch.purged_ids == (21, 22)

    asyncio.run(run())
