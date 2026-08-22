"""
Tests for api/vote_webhook.py — the verified vote-bonus path for
economy.py's /vote command (spec §4 open question #1).
"""

from unittest.mock import AsyncMock, patch

import pytest

import config
from api.vote_webhook import process_vote, VoteRejected, _extract_ids


def _set_auth(monkeypatch, value="topgg-secret"):
    monkeypatch.setattr(config, "TOPGG_WEBHOOK_AUTH", value)
    monkeypatch.setattr(config, "DISCORD_BOT_USER_ID", 111)


def test_extract_ids_topgg_shape():
    bot_id, user_id = _extract_ids({"bot": "111", "user": "222", "type": "upvote"})
    assert (bot_id, user_id) == (111, 222)


def test_extract_ids_missing_fields():
    assert _extract_ids({"type": "upvote"}) == (None, None)


@pytest.mark.asyncio
async def test_rejects_when_secret_not_configured(monkeypatch):
    monkeypatch.setattr(config, "TOPGG_WEBHOOK_AUTH", "")
    with pytest.raises(VoteRejected) as exc:
        await process_vote({"bot": "111", "user": "222"}, "anything")
    assert exc.value.status == 401


@pytest.mark.asyncio
async def test_rejects_bad_auth_header(monkeypatch):
    _set_auth(monkeypatch)
    with pytest.raises(VoteRejected) as exc:
        await process_vote({"bot": "111", "user": "222"}, "wrong-secret")
    assert exc.value.status == 401


@pytest.mark.asyncio
async def test_rejects_malformed_payload(monkeypatch):
    _set_auth(monkeypatch)
    with pytest.raises(VoteRejected) as exc:
        await process_vote({"type": "upvote"}, "topgg-secret")
    assert exc.value.status == 400


@pytest.mark.asyncio
async def test_ignores_unhandled_vote_type(monkeypatch):
    _set_auth(monkeypatch)
    result = await process_vote({"bot": "111", "user": "222", "type": "test"}, "topgg-secret")
    assert result["status"] == "ignored"


@pytest.mark.asyncio
async def test_main_bot_vote_credits_with_clone_id_none(monkeypatch):
    _set_auth(monkeypatch)
    with patch("api.vote_webhook.db.grant_vote_bonus_for_voter", new=AsyncMock(return_value=[{"guild_id": 1}])) as mock_grant, \
         patch("api.vote_webhook.db.resolve_clone_id_by_bot_user_id", new=AsyncMock()) as mock_resolve:
        result = await process_vote({"bot": "111", "user": "222", "type": "upvote"}, "topgg-secret")

    mock_resolve.assert_not_awaited()  # bot_id matched DISCORD_BOT_USER_ID directly, no lookup needed
    mock_grant.assert_awaited_once_with(222, None)
    assert result == {"status": "ok", "credited_guilds": 1}


@pytest.mark.asyncio
async def test_clone_vote_resolves_clone_id_and_credits(monkeypatch):
    _set_auth(monkeypatch)
    with patch("api.vote_webhook.db.resolve_clone_id_by_bot_user_id", new=AsyncMock(return_value=7)) as mock_resolve, \
         patch("api.vote_webhook.db.grant_vote_bonus_for_voter", new=AsyncMock(return_value=[])) as mock_grant:
        result = await process_vote({"bot": "999", "user": "222", "type": "upvote"}, "topgg-secret")

    mock_resolve.assert_awaited_once_with(999)
    mock_grant.assert_awaited_once_with(222, 7)
    assert result == {"status": "ok", "credited_guilds": 0}


@pytest.mark.asyncio
async def test_unknown_bot_id_rejected(monkeypatch):
    _set_auth(monkeypatch)
    with patch("api.vote_webhook.db.resolve_clone_id_by_bot_user_id", new=AsyncMock(return_value=None)):
        with pytest.raises(VoteRejected) as exc:
            await process_vote({"bot": "999", "user": "222", "type": "upvote"}, "topgg-secret")
    assert exc.value.status == 404
