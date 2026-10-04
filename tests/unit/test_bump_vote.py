"""Bump: a Top.gg vote cast since the last bump lets that person bump through the cooldown (once per vote)."""
import asyncio
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from discord_bot.cogs import bump

ROOT = Path(__file__).resolve().parents[2]


def run(c):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(c)


@pytest.fixture()
def dbm(monkeypatch):
    m = SimpleNamespace(bump_check_cooldown=AsyncMock(return_value=(False, 600)),
                        bump_vote_unlocks=AsyncMock(return_value=False))
    monkeypatch.setattr(bump, "db", m)
    return m


def test_no_cooldown_means_no_vote_lookup(dbm):
    dbm.bump_check_cooldown.return_value = (True, 0)
    assert run(bump._bump_access(1, 5, 3600)) == (True, 0, False)
    dbm.bump_vote_unlocks.assert_not_awaited()


def test_on_cooldown_without_a_vote_is_refused_with_the_time_left(dbm):
    assert run(bump._bump_access(1, 5, 3600)) == (False, 600, False)


def test_a_vote_since_the_last_bump_unlocks_it(dbm):
    dbm.bump_vote_unlocks.return_value = True
    assert run(bump._bump_access(1, 5, 3600)) == (True, 0, True)
    dbm.bump_vote_unlocks.assert_awaited_once_with(1, 5)


def test_a_failing_vote_lookup_never_unlocks_and_never_crashes(dbm):
    dbm.bump_vote_unlocks.side_effect = RuntimeError("db down")
    assert run(bump._bump_access(1, 5, 3600)) == (False, 600, False)


def test_vote_view_is_one_link_button_to_the_vote_page(monkeypatch):
    import config
    monkeypatch.setattr(config, "TOPGG_VOTE_URL", "https://top.gg/bot/123/vote")
    v = bump._vote_view()
    (b,) = v.children
    assert b.url == "https://top.gg/bot/123/vote" and "Vote" in b.label
    monkeypatch.setattr(config, "TOPGG_VOTE_URL", "")
    assert bump._vote_view() is None


def test_pressing_bump_on_cooldown_offers_the_vote_button(dbm, monkeypatch):
    import config
    monkeypatch.setattr(config, "TOPGG_VOTE_URL", "https://top.gg/bot/123/vote")
    dbm.bump_get_listing = AsyncMock(return_value=dict(id=5, guild_id=77, clone_id=None))
    dbm.bump_get_guild_config = AsyncMock(return_value=dict(bump_channel_id=9, receives_bumps=True))
    cog = MagicMock()
    cog._cooldown_seconds = AsyncMock(return_value=43200)
    cog._do_bump = AsyncMock()
    i = MagicMock()
    i.guild_id, i.user.id = 77, 1
    i.response.defer = AsyncMock()
    i.followup.send = AsyncMock()
    i.client.get_cog.return_value = cog
    run(bump.DynamicBumpPromptButton(5).callback(i))
    cog._do_bump.assert_not_awaited()
    kw = i.followup.send.await_args.kwargs
    assert "Vote" in i.followup.send.await_args.args[0] and kw["view"].children[0].url.endswith("/vote")
    # ...and after voting, the same press goes through
    dbm.bump_vote_unlocks.return_value = True
    run(bump.DynamicBumpPromptButton(5).callback(i))
    cog._do_bump.assert_awaited_once()


def test_vote_is_only_valid_if_cast_after_the_last_bump():
    src = (ROOT / "database.py").read_text()
    body = src[src.index("async def bump_vote_unlocks"):src.index("async def bump_record")]
    assert "v.last_vote_at > b.last_bump_at" in body and "v.expires_at > NOW()" in body
    assert "b.last_bump_at IS NULL" in body
