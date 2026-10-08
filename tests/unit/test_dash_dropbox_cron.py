import asyncio

import pytest

from api import cron_dash_dropbox_dm as cron


@pytest.mark.parametrize("err,expected", [
    ("open_dm_failed:403", False),      # closed DMs: final
    ("open_dm_failed:404", False),
    ("send_failed:403", False),
    ("open_dm_failed:429", True),       # rate limited: retry
    ("send_failed:502", True),
    ("network_error:timeout", True),
    ("garbage", False),
])
def test_is_retryable(err, expected):
    assert cron.is_retryable(err) is expected


class FakeDB:
    def __init__(self, rows):
        self.rows, self.finished, self.released = rows, [], []
    async def dropbox_dm_claim(self, limit):
        return self.rows
    async def dropbox_dm_finish(self, dm_id, error=None, retry=False, max_attempts=3):
        self.finished.append((dm_id, error, retry))
    async def dropbox_dm_release(self, ids):
        self.released.extend(ids)


def _row(i, attempts=1):
    return dict(id=i, message_id=1, user_id=100 + i, attempts=attempts, title="T", body="B", kind="info")


def _run(monkeypatch, rows, results):
    fake = FakeDB(rows)
    monkeypatch.setattr(cron, "db", fake)
    monkeypatch.setattr(cron, "DISCORD_BOT_TOKEN", "x")
    monkeypatch.setattr(cron, "DM_DELAY_SECONDS", 0)
    it = iter(results)
    async def dm(session, token, uid, text, *a):
        return next(it)
    monkeypatch.setattr(cron, "_dm_user", dm)
    return fake, asyncio.run(cron.run_pending_dropbox_dms())


def test_sent_closed_dm_and_transient(monkeypatch):
    fake, totals = _run(monkeypatch, [_row(1), _row(2), _row(3)], [None, "open_dm_failed:403", "send_failed:429"])
    assert totals == {"sent": 1, "failed": 1, "retry": 1}
    assert fake.finished[0] == (1, None, False)
    assert fake.finished[1] == (2, "open_dm_failed:403", False)   # closed DMs are never retried
    assert fake.finished[2] == (3, "send_failed:429", True)


def test_gives_up_after_max_attempts(monkeypatch):
    fake, totals = _run(monkeypatch, [_row(1, attempts=cron.MAX_ATTEMPTS)], ["send_failed:429"])
    assert totals["failed"] == 1 and totals["retry"] == 0


def test_deadline_releases_unattempted(monkeypatch):
    monkeypatch.setattr(cron, "WALL_CLOCK_BUDGET_SECONDS", -1)
    fake, totals = _run(monkeypatch, [_row(1), _row(2)], [])
    assert fake.released == [1, 2] and totals["sent"] == 0 and not fake.finished
