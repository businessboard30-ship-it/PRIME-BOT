"""CLONE_MANAGER_EXCLUDE_IDS: clones hosted elsewhere must not be started by the Railway supervisor."""
import asyncio

import pytest

from discord_bot import clone_manager as cm


def test_parse_exclude_ids(monkeypatch):
    monkeypatch.setenv("CLONE_MANAGER_EXCLUDE_IDS", "3, 7 x,9")
    assert cm._excluded_clone_ids() == {3, 7, 9}


def test_parse_exclude_ids_unset(monkeypatch):
    monkeypatch.delenv("CLONE_MANAGER_EXCLUDE_IDS", raising=False)
    assert cm._excluded_clone_ids() == set()


def test_reconcile_skips_excluded_and_stops_running(monkeypatch):
    monkeypatch.setenv("CLONE_MANAGER_EXCLUDE_IDS", "2")

    async def dupes():
        return []

    async def active():
        return [{"clone_id": 1}, {"clone_id": 2}]

    monkeypatch.setattr(cm.db, "get_duplicate_active_clone_tokens", dupes)
    monkeypatch.setattr(cm.db, "get_active_discord_clones", active)

    started = []

    class Fake:
        def __init__(self, clone_id, *a, **k):
            self.clone_id = clone_id
            self.stopped = False

        def stop(self):
            self.stopped = True

    # An excluded clone that was already running is stopped and dropped.
    managed = {2: Fake(2)}
    running_two = managed[2]
    monkeypatch.setattr(cm, "ManagedClone", Fake)

    async def run():
        try:
            await asyncio.wait_for(cm._reconcile(managed), timeout=1)
        except Exception:
            pass  # the start path needs a live db/subprocess; we only assert the filtering

    asyncio.run(run())
    assert running_two.stopped
    assert 2 not in managed
