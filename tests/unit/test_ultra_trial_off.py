import asyncio
import database


def test_trial_switch_off_never_starts_a_trial(monkeypatch):
    """ULTRA_TRIAL_ENABLED is the owner's on/off switch for the free Customize Card trial.

    The shipped value is a product decision (currently ON, 3 days), so this test forces
    the switch OFF and checks the guard: with the switch off, start_ultra_trial must
    return False before touching the pool, so no database is needed.
    """
    monkeypatch.setattr(database, "ULTRA_TRIAL_ENABLED", False)
    assert asyncio.run(database.db.start_ultra_trial(1, 2, clone_id=None)) is False


def test_trial_length_is_a_positive_number_of_days():
    assert isinstance(database.ULTRA_TRIAL_DAYS, int) and database.ULTRA_TRIAL_DAYS > 0
