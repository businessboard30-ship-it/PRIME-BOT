"""A member missing from the cache is not a member who left: quarantine release and the review card must ask Discord."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from database import db
from modules import antiraid_pro as pro
from modules import server_panel_quarantine as spq


def run(c):
    return asyncio.run(c)


@pytest.fixture
def patched(monkeypatch):
    st = SimpleNamespace(removed=AsyncMock(), changes=AsyncMock())
    monkeypatch.setattr(db, "get_quarantined", AsyncMock(return_value={"saved_role_ids": []}), raising=False)
    monkeypatch.setattr(db, "get_quarantine_role", AsyncMock(return_value=None), raising=False)
    monkeypatch.setattr(db, "remove_quarantined", st.removed, raising=False)
    monkeypatch.setattr(spq, "record_change", st.changes)
    return st


def _guild(fetch):
    g = MagicMock()
    g.id = 10
    g.get_member.return_value = None            # cold cache
    g.fetch_member = fetch
    g.roles = []
    return g


def _resp(status, reason):
    r = MagicMock()
    r.status, r.reason = status, reason
    return r


def test_uncached_member_is_fetched_and_released_not_dropped(patched):
    member = MagicMock()
    member.roles = []
    member.mention = "<@5>"
    member.edit = AsyncMock()
    g = _guild(AsyncMock(return_value=member))
    ok, _msg = run(spq.release_member(g, None, 1, 5))
    assert ok is True
    member.edit.assert_awaited_once()          # roles really given back
    patched.removed.assert_awaited_once()


def test_really_left_member_is_removed_from_list(patched):
    g = _guild(AsyncMock(side_effect=discord.NotFound(_resp(404, "Not Found"), "unknown member")))
    ok, msg = run(spq.release_member(g, None, 1, 5))
    assert ok is True and "left the server" in msg
    patched.removed.assert_awaited_once()


def test_discord_outage_keeps_them_on_the_list(patched):
    g = _guild(AsyncMock(side_effect=discord.HTTPException(_resp(500, "oops"), "boom")))
    ok, msg = run(spq.release_member(g, None, 1, 5))
    assert ok is False and "stay on the list" in msg
    patched.removed.assert_not_awaited()


def test_review_embed_uses_fetched_member_instead_of_saying_left():
    g = MagicMock()
    g.get_member.return_value = None
    row = {"user_id": 5, "reason": ""}
    left = pro.review_embed(g, row, 1, 1)
    assert "has left the server" in left.description
    member = SimpleNamespace(mention="<@5>", name="sam", display_avatar=SimpleNamespace(url="https://x/a.png"),
                             created_at=None, joined_at=None, avatar=None)
    found = pro.review_embed(g, row, 1, 1, member=member)
    assert "left the server" not in found.description and "sam" in found.description
