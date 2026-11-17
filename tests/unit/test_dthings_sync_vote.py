"""DiscordThings voting: parse the vote time, verify + record it once, and the /vote claim messages."""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import config
from modules import dthings_vote as dv


def run(c):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(c)


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    dv._cache.clear()
    monkeypatch.setattr(dv, "_MIN_GAP_SECONDS", 0.0)
    monkeypatch.setattr(config, "TOPGG_VOTE_HOURS", 12.0)


# ── parse_vote_time ───────────────────────────────────────────────────────

def test_parse_vote_time_accepts_iso_epoch_seconds_and_milliseconds():
    want = datetime(2026, 11, 16, 10, 0, tzinfo=timezone.utc)
    assert dv.parse_vote_time("2026-11-16T10:00:00.000Z") == want
    assert dv.parse_vote_time("2026-11-16T10:00:00+00:00") == want
    assert dv.parse_vote_time(int(want.timestamp())) == want
    assert dv.parse_vote_time(int(want.timestamp() * 1000)) == want
    assert dv.parse_vote_time(str(int(want.timestamp()))) == want


@pytest.mark.parametrize("bad", [None, "", "soon", 0, -5, True, [], {}])
def test_parse_vote_time_rejects_garbage(bad):
    assert dv.parse_vote_time(bad) is None


# ── check_vote_info: reads lastVote, caches, never hammers ────────────────

class _Resp:
    def __init__(self, body, status=200):
        self.status, self._body = status, body

    async def json(self, content_type=None):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


def _session(monkeypatch, body, status=200):
    calls = []

    class Sess:
        def __init__(self, *a, **k): pass
        def get(self, url, params=None, headers=None):
            calls.append(params)
            return _Resp(body, status)
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

    monkeypatch.setattr(config, "DTHINGS_API_KEY", "tok")
    monkeypatch.setattr(dv.aiohttp, "ClientSession", Sess)
    return calls


def test_check_vote_info_returns_the_vote_time(monkeypatch):
    _session(monkeypatch, {"voted": True, "lastVote": "2026-11-16T10:00:00.000Z"})
    status, last = run(dv.check_vote_info(5))
    assert status == dv.VOTED and last == datetime(2026, 11, 16, 10, 0, tzinfo=timezone.utc)


def test_check_vote_info_not_voted_has_no_time(monkeypatch):
    _session(monkeypatch, {"voted": False, "lastVote": "2026-11-01T10:00:00Z"})
    assert run(dv.check_vote_info(5)) == (dv.NOT_VOTED, None)


def test_same_user_is_answered_from_cache_for_a_minute(monkeypatch):
    calls = _session(monkeypatch, {"voted": True})
    run(dv.check_vote_info(9))
    run(dv.check_vote_info(9))
    assert len(calls) == 1


def test_errors_are_not_cached_so_a_retry_really_retries(monkeypatch):
    calls = _session(monkeypatch, {}, status=429)
    assert run(dv.check_vote_info(9))[0] == dv.ERROR
    run(dv.check_vote_info(9))
    assert len(calls) == 2


# ── sync_vote ─────────────────────────────────────────────────────────────

def _db(monkeypatch, returns):
    import database
    rec = AsyncMock(return_value=returns)
    monkeypatch.setattr(database, "db", SimpleNamespace(record_topgg_vote=rec))
    return rec


def test_sync_records_a_fresh_vote_from_its_real_time(monkeypatch):
    last = datetime.now(timezone.utc) - timedelta(hours=2)
    monkeypatch.setattr(dv, "check_vote_info", AsyncMock(return_value=(dv.VOTED, last)))
    rec = _db(monkeypatch, {"user_id": 5})
    assert run(dv.sync_vote(5)) == (dv.VOTED, True)
    args = rec.await_args.args
    assert args[0] == 5 and args[1] == 12.0 and args[2] == f"dthings:{int(last.timestamp())}" and args[3] == last


def test_sync_same_vote_twice_is_recorded_once(monkeypatch):
    last = datetime.now(timezone.utc) - timedelta(hours=1)
    monkeypatch.setattr(dv, "check_vote_info", AsyncMock(return_value=(dv.VOTED, last)))
    _db(monkeypatch, None)                       # the DB says "already have this vote_id"
    assert run(dv.sync_vote(5)) == (dv.VOTED, False)


def test_sync_ignores_a_vote_older_than_the_boost_window(monkeypatch):
    last = datetime.now(timezone.utc) - timedelta(hours=13)
    monkeypatch.setattr(dv, "check_vote_info", AsyncMock(return_value=(dv.VOTED, last)))
    rec = _db(monkeypatch, {"x": 1})
    assert run(dv.sync_vote(5)) == (dv.NOT_VOTED, False)
    rec.assert_not_awaited()


def test_sync_clamps_a_future_vote_time(monkeypatch):
    monkeypatch.setattr(dv, "check_vote_info",
                        AsyncMock(return_value=(dv.VOTED, datetime.now(timezone.utc) + timedelta(hours=5))))
    rec = _db(monkeypatch, {"x": 1})
    run(dv.sync_vote(5))
    assert rec.await_args.args[3] <= datetime.now(timezone.utc)


def test_sync_without_a_time_allows_one_grant_per_window(monkeypatch):
    monkeypatch.setattr(dv, "check_vote_info", AsyncMock(return_value=(dv.VOTED, None)))
    rec = _db(monkeypatch, {"x": 1})
    run(dv.sync_vote(5))
    assert rec.await_args.args[2].startswith("dthings:window:") and rec.await_args.args[3] is None


@pytest.mark.parametrize("status", [dv.NOT_VOTED, dv.ERROR, dv.NO_TOKEN])
def test_sync_passes_other_statuses_through_without_recording(monkeypatch, status):
    monkeypatch.setattr(dv, "check_vote_info", AsyncMock(return_value=(status, None)))
    rec = _db(monkeypatch, {"x": 1})
    assert run(dv.sync_vote(5)) == (status, False)
    rec.assert_not_awaited()


def test_sync_survives_a_database_failure(monkeypatch):
    import database
    monkeypatch.setattr(dv, "check_vote_info", AsyncMock(return_value=(dv.VOTED, datetime.now(timezone.utc))))
    monkeypatch.setattr(database, "db", SimpleNamespace(record_topgg_vote=AsyncMock(side_effect=RuntimeError("db down"))))
    assert run(dv.sync_vote(5)) == (dv.ERROR, False)


# ── /vote messages + bump hook ────────────────────────────────────────────

@pytest.mark.parametrize("result,needle", [
    ((dv.VOTED, True), "Thanks for voting"),
    ((dv.VOTED, False), "already have that vote"),
    ((dv.NOT_VOTED, False), "can't see a vote"),
    ((dv.NO_TOKEN, False), "isn't set up"),
    ((dv.ERROR, False), "couldn't reach"),
])
def test_vote_claim_message(monkeypatch, result, needle):
    from discord_bot.cogs import dthings_stats as ds
    monkeypatch.setattr(ds.dthings_vote, "sync_vote", AsyncMock(return_value=result))
    assert needle in run(ds.vote_claim_message(1))


def test_vote_claim_view_has_a_link_and_an_i_voted_button():
    from discord_bot.cogs import dthings_stats as ds

    async def build():
        return ds.VoteClaimView("https://discordthings.com/bot/1")
    v = run(build())
    labels = {getattr(c, "label", None): getattr(c, "url", None) for c in v.children}
    assert labels["Vote on DiscordThings"] == "https://discordthings.com/bot/1" and "I voted" in labels


def test_bump_hook_is_off_without_a_vote_link_and_on_with_one(monkeypatch):
    from discord_bot.cogs import bump
    monkeypatch.setattr(config, "DTHINGS_VOTE_URL", "")
    assert run(bump._sync_dthings_vote(1)) is False
    monkeypatch.setattr(config, "DTHINGS_VOTE_URL", "https://discordthings.com/bot/1")
    monkeypatch.setattr(dv, "sync_vote", AsyncMock(return_value=(dv.VOTED, True)))
    assert run(bump._sync_dthings_vote(1)) is True
    monkeypatch.setattr(dv, "sync_vote", AsyncMock(return_value=(dv.NOT_VOTED, False)))
    assert run(bump._sync_dthings_vote(1)) is False
