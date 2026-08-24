import pytest

from game import heist_service
from game.models import HeistState
from tests.heist_db_fixtures import requires_db

pytestmark = [pytest.mark.asyncio, requires_db]

GUILD, USER = 1001, 5001


async def _play_to_completion(patched_pool, run):
    """Drives a run through every event phase by always picking the first
    available choice, until it reaches a terminal state."""
    while HeistState(run["status"]) not in (HeistState.COMPLETED, HeistState.FAILED, HeistState.EXPIRED):
        current = await heist_service.current_event_for_run(run)
        if current is None:
            run = await heist_service.complete_heist(GUILD, None, USER, run["id"])
            break
        choice_key = current["event"].choices[0].key
        run = await heist_service.choose_event(GUILD, None, USER, run["id"], choice_key)
    return run


class TestCreateRun:
    async def test_start_heist_creates_run_in_infiltration(self, patched_pool):
        run = await heist_service.start_heist(GUILD, None, USER, "convenience_store", "stealth")
        assert run["status"] == HeistState.INFILTRATION.value
        assert run["user_id"] == USER

    async def test_cannot_start_second_active_run(self, patched_pool):
        await heist_service.start_heist(GUILD, None, USER, "convenience_store", "stealth")
        with pytest.raises(heist_service.AlreadyActiveRunError):
            await heist_service.start_heist(GUILD, None, USER, "convenience_store", "stealth")

    async def test_level_gate_enforced(self, patched_pool):
        with pytest.raises(heist_service.LevelTooLowError):
            await heist_service.start_heist(GUILD, None, USER, "bank_vault", "stealth")


class TestResumeRun:
    async def test_resume_returns_active_run(self, patched_pool):
        started = await heist_service.start_heist(GUILD, None, USER, "convenience_store", "stealth")
        resumed = await heist_service.resume_heist(GUILD, None, USER)
        assert resumed["id"] == started["id"]

    async def test_resume_returns_none_when_no_active_run(self, patched_pool):
        assert await heist_service.resume_heist(GUILD, None, 999999) is None


class TestCompleteRun:
    async def test_full_run_reaches_terminal_state(self, patched_pool):
        run = await heist_service.start_heist(GUILD, None, USER, "convenience_store", "stealth")
        final = await _play_to_completion(patched_pool, run)
        assert HeistState(final["status"]) in (HeistState.COMPLETED, HeistState.FAILED)
        assert final["reward_granted"] is True

    async def test_completed_run_no_longer_active(self, patched_pool):
        run = await heist_service.start_heist(GUILD, None, USER, "convenience_store", "stealth")
        await _play_to_completion(patched_pool, run)
        assert await heist_service.resume_heist(GUILD, None, USER) is None


class TestCooldown:
    async def test_cooldown_blocks_immediate_retry(self, patched_pool):
        run = await heist_service.start_heist(GUILD, None, USER, "convenience_store", "stealth")
        await _play_to_completion(patched_pool, run)
        with pytest.raises(heist_service.CooldownActiveError):
            await heist_service.start_heist(GUILD, None, USER, "convenience_store", "stealth")

    async def test_different_location_not_blocked_by_other_locations_cooldown(self, patched_pool):
        run = await heist_service.start_heist(GUILD, None, USER, "convenience_store", "stealth")
        await _play_to_completion(patched_pool, run)
        # jewelry_store requires level 5; bump the player's level directly for the test.
        import database
        pool = await database.get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE heist_players SET level = 5 WHERE guild_id=$1 AND user_id=$2", GUILD, USER
            )
        run2 = await heist_service.start_heist(GUILD, None, USER, "jewelry_store", "stealth")
        assert run2["status"] == HeistState.INFILTRATION.value


class TestOwnership:
    async def test_other_user_cannot_answer_choice(self, patched_pool):
        run = await heist_service.start_heist(GUILD, None, USER, "convenience_store", "stealth")
        current = await heist_service.current_event_for_run(run)
        choice_key = current["event"].choices[0].key
        with pytest.raises(heist_service.NotOwnerError):
            await heist_service.choose_event(GUILD, None, 9999999, run["id"], choice_key)


class TestStatePersistence:
    async def test_decision_modifier_persists_across_calls(self, patched_pool):
        run = await heist_service.start_heist(GUILD, None, USER, "convenience_store", "stealth")
        current = await heist_service.current_event_for_run(run)
        choice = current["event"].choices[0]
        updated = await heist_service.choose_event(GUILD, None, USER, run["id"], choice.key)
        assert updated["decision_modifier_total"] == choice.success_modifier


class TestRewardTransaction:
    async def test_reward_ledger_row_created_on_completion(self, patched_pool):
        run = await heist_service.start_heist(GUILD, None, USER, "convenience_store", "stealth")
        final = await _play_to_completion(patched_pool, run)
        async with patched_pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM heist_reward_ledger WHERE run_id = $1", final["id"])
        assert row is not None
        assert row["cash"] == final["reward_cash"]
