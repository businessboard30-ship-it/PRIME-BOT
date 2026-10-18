import asyncio
import database


def test_new_ultra_trials_are_off():
    assert database.ULTRA_TRIAL_ENABLED is False
    # Must return before touching the pool, so no database is needed.
    assert asyncio.run(database.db.start_ultra_trial(1, 2, clone_id=None)) is False
