"""Per-process Postgres pool sizing (keeps the bot + clones under the server's connection cap)."""
from database import _pool_limits


def test_main_process_defaults_are_unchanged():
    assert _pool_limits(False, argv=["bot.py"], env={}) == (1, 5)
    assert _pool_limits(True, argv=["bot.py"], env={}) == (1, 20)


def test_clone_processes_get_a_smaller_pool():
    assert _pool_limits(False, argv=["bot.py", "--clone-id", "3"], env={}) == (1, 3)
    assert _pool_limits(True, argv=["bot.py", "--clone-id", "3"], env={}) == (1, 3)


def test_env_overrides_win_and_bad_values_fall_back():
    assert _pool_limits(False, argv=["x"], env={"DB_POOL_MAX": "2", "DB_POOL_MIN": "0"}) == (0, 2)
    assert _pool_limits(False, argv=["x", "--clone-id", "1"], env={"DB_POOL_MAX": "8"}) == (1, 8)
    assert _pool_limits(False, argv=["x"], env={"DB_POOL_MAX": "abc"}) == (1, 5)
    assert _pool_limits(False, argv=["x"], env={"DB_POOL_MAX": "2", "DB_POOL_MIN": "9"}) == (2, 2)
