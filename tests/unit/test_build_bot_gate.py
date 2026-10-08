"""Build Bot button is owner-only until the owner opens it from the admin panel (OPT_IN switch)."""
import asyncio

from modules import admin_controls as ac


def _set(ok, switches):
    ac._snapshot.update({"ok": ok, "switches": set(switches), "ts": 10**12})


def test_closed_by_default_and_fails_closed():
    _set(False, [])
    assert ac.build_bot_open_cached() is False
    _set(True, [])
    assert ac.build_bot_open_cached() is False


def test_open_only_when_switch_engaged():
    _set(True, ["build_bot_public"])
    assert ac.build_bot_open_cached() is True
    assert asyncio.run(ac.build_bot_open()) is True


def test_switch_is_known_and_does_not_touch_commands():
    assert "build_bot_public" in ac.OPT_IN
    assert "build_bot_public" not in ac.FEATURES
    assert "build_bot_public" not in ac._ROOT_TO_FEATURE.values()
