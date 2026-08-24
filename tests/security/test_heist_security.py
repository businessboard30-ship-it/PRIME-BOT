import asyncio

import pytest

from game import heist_service
from game.models import HeistState
from tests.heist_db_fixtures import requires_db

pytestmark = [pytest.mark.asyncio, requires_db]

GUILD, USER, ATTACKER = 2001, 6001, 6002


async def _drive_to_escape(run):
    """Answers every event choice until the run reaches ESCAPE (returned
    as-is if it fails/completes earlier via instant_fail)."""
    while True:
        current = await heist_service.current_event_for_run(run)
        if current is None:
            return run
        choice_key = current["event"].choices[0].key
        run = await heist_service.choose_event(GUILD, None, USER, run["id"], choice_key)
        if HeistState(run["status"]) in (HeistState.COMPLETED, HeistState.FAILED, HeistState.ESCAPE):
            return run


class TestWrongUserClickingButton:
    async def test_attacker_cannot_answer_victim_choice(self, patched_pool):
        run = await heist_service.start_heist(GUILD, None, USER, "convenience_store", "stealth")
        current = await heist_service.current_event_for_run(run)
        choice_key = current["event"].choices[0].key
        with pytest.raises(heist_service.NotOwnerError):
            await heist_service.choose_event(GUILD, None, ATTACKER, run["id"], choice_key)

    async def test_attacker_cannot_complete_victim_run(self, patched_pool):
        run = await heist_service.start_heist(GUILD, None, USER, "convenience_store", "stealth")
        with pytest.raises(heist_service.NotOwnerError):
            await heist_service.complete_heist(GUILD, None, ATTACKER, run["id"])


class TestModifiedRunId:
    async def test_nonexistent_run_id_raises(self, patched_pool):
        with pytest.raises(heist_service.NoActiveRunError):
            await heist_service.choose_event(GUILD, None, USER, 99999999, "wait")

    async def test_modified_choice_key_rejected(self, patched_pool):
        run = await heist_service.start_heist(GUILD, None, USER, "convenience_store", "stealth")
        with pytest.raises(ValueError):
            await heist_service.choose_event(GUILD, None, USER, run["id"], "not_a_real_choice_forged_by_client")


class TestDuplicateCompletion:
    async def test_second_completion_does_not_pay_twice(self, patched_pool):
        run = await heist_service.start_heist(GUILD, None, USER, "convenience_store", "stealth")
        run = await _drive_to_escape(run)
        if HeistState(run["status"]) == HeistState.ESCAPE:
            first = await heist_service.complete_heist(GUILD, None, USER, run["id"])
        else:
            first = run
        second = await heist_service.complete_heist(GUILD, None, USER, run["id"])
        assert first["reward_cash"] == second["reward_cash"]
        assert first["random_modifier"] == second["random_modifier"]

        async with patched_pool.acquire() as conn:
            count = await conn.fetchval("SELECT COUNT(*) FROM heist_reward_ledger WHERE run_id = $1", run["id"])
        assert count == 1


class TestSimultaneousCompletion:
    async def test_concurrent_completion_pays_exactly_once(self, patched_pool):
        run = await heist_service.start_heist(GUILD, None, USER, "convenience_store", "stealth")
        run = await _drive_to_escape(run)
        assert HeistState(run["status"]) == HeistState.ESCAPE

        results = await asyncio.gather(
            *[heist_service.complete_heist(GUILD, None, USER, run["id"]) for _ in range(10)]
        )

        statuses = {HeistState(r["status"]) for r in results}
        assert statuses <= {HeistState.COMPLETED, HeistState.FAILED}
        assert len(statuses) == 1  # every caller sees the same final outcome

        async with patched_pool.acquire() as conn:
            ledger_count = await conn.fetchval(
                "SELECT COUNT(*) FROM heist_reward_ledger WHERE run_id = $1", run["id"]
            )
            run_row = await conn.fetchrow("SELECT * FROM heist_runs WHERE id = $1", run["id"])

        assert ledger_count == 1
        assert run_row["reward_granted"] is True


class TestExpiredRun:
    async def test_cannot_act_on_expired_run(self, patched_pool):
        run = await heist_service.start_heist(GUILD, None, USER, "convenience_store", "stealth")
        async with patched_pool.acquire() as conn:
            await conn.execute("UPDATE heist_runs SET status = 'EXPIRED' WHERE id = $1", run["id"])
        with pytest.raises(heist_service.InvalidStateError):
            await heist_service.choose_event(GUILD, None, USER, run["id"], "wait")


class TestInvalidStateTransition:
    async def test_cannot_complete_a_planning_run(self, patched_pool):
        run = await heist_service.start_heist(GUILD, None, USER, "convenience_store", "stealth")
        async with patched_pool.acquire() as conn:
            await conn.execute("UPDATE heist_runs SET status = 'PLANNING' WHERE id = $1", run["id"])
        with pytest.raises(heist_service.InvalidStateError):
            await heist_service.complete_heist(GUILD, None, USER, run["id"])

    async def test_cannot_complete_already_completed_run_to_change_reward(self, patched_pool):
        run = await heist_service.start_heist(GUILD, None, USER, "convenience_store", "stealth")
        run = await _drive_to_escape(run)
        if HeistState(run["status"]) == HeistState.ESCAPE:
            run = await heist_service.complete_heist(GUILD, None, USER, run["id"])
        again = await heist_service.complete_heist(GUILD, None, USER, run["id"])
        assert again["reward_cash"] == run["reward_cash"]
