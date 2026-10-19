"""Edit bot profile is unlocked per server by one DiscordThings vote (modules/dthings_vote.py)."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import config
from discord_bot.cogs import _views_bot_profile as bp
from modules import dthings_vote as dv


def _interaction(done=False):
    resp = MagicMock()
    flag = {"done": done}
    resp.is_done.side_effect = lambda: flag["done"]

    async def _defer(*a, **k):
        flag["done"] = True
    resp.defer = AsyncMock(side_effect=_defer)
    resp.send_message = AsyncMock()
    return SimpleNamespace(user=SimpleNamespace(id=7), response=resp, followup=SimpleNamespace(send=AsyncMock()))


@pytest.fixture
def fake(monkeypatch):
    state = SimpleNamespace(unlocked=False, status=dv.NOT_VOTED, unlock=AsyncMock(), checked=0)
    monkeypatch.setattr(config, "BOT_PROFILE_VOTE_GATE", True, raising=False)
    monkeypatch.setattr(bp.db, "is_bot_profile_unlocked", AsyncMock(side_effect=lambda g: state.unlocked), raising=False)
    monkeypatch.setattr(bp.db, "unlock_bot_profile", state.unlock, raising=False)

    async def check(uid):
        state.checked += 1
        return state.status
    monkeypatch.setattr(dv, "check_vote", check)
    return state


def run(coro):
    return asyncio.run(coro)


def test_already_unlocked_skips_api(fake):
    fake.unlocked = True
    assert run(bp._ensure_unlocked(_interaction(), 1, None)) is True
    assert fake.checked == 0


def test_voted_unlocks_server_for_good(fake):
    fake.status = dv.VOTED
    assert run(bp._ensure_unlocked(_interaction(), 1, None)) is True
    fake.unlock.assert_awaited_once_with(1, 7)


def test_not_voted_is_blocked_with_vote_prompt(fake):
    i = _interaction()
    assert run(bp._ensure_unlocked(i, 1, None)) is False
    fake.unlock.assert_not_awaited()
    i.response.defer.assert_awaited_once()          # deferred before the slow API call
    sent = i.followup.send.await_args.kwargs
    assert "vote" in sent["content"].lower() and sent["ephemeral"] is True


def test_api_error_blocks_but_does_not_unlock(fake):
    fake.status = dv.ERROR
    i = _interaction(done=True)
    assert run(bp._ensure_unlocked(i, 1, None)) is False
    fake.unlock.assert_not_awaited()
    assert "couldn't reach" in i.followup.send.await_args.kwargs["content"]


def test_missing_token_lets_people_through(fake):
    fake.status = dv.NO_TOKEN
    assert run(bp._ensure_unlocked(_interaction(), 1, None)) is True
    fake.unlock.assert_not_awaited()


def test_gate_can_be_switched_off(fake, monkeypatch):
    monkeypatch.setattr(config, "BOT_PROFILE_VOTE_GATE", False, raising=False)
    assert run(bp._ensure_unlocked(_interaction(), 1, None)) is True
    assert fake.checked == 0


def test_reset_and_done_and_vote_check_are_not_gated():
    assert bp._ResetButton.gated is False and bp._DoneButton.gated is False and bp._VoteCheckButton.gated is False
    assert bp._RenameButton.gated is True and bp._ApplyButton.gated is True


def test_check_vote_parses_response(monkeypatch):
    class Resp:
        status = 200
        async def json(self, content_type=None): return {"voted": True}
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

    class Sess:
        def __init__(self, *a, **k): pass
        def get(self, url, params=None, headers=None):
            assert url.endswith("/check-vote") and params == {"userId": "7"} and headers == {"Authorization": "tok"}
            return Resp()
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

    monkeypatch.setattr(config, "DTHINGS_API_KEY", "tok")
    monkeypatch.setattr(dv.aiohttp, "ClientSession", Sess)
    assert run(dv.check_vote(7)) == dv.VOTED
