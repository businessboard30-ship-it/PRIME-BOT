"""The invite tracker's join announcement mentions the new member and the inviter so their
names render, but it must never notify them (allowed_mentions none)."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from discord_bot.cogs import invites as inv_mod


def _cog():
    cog = inv_mod.InvitesCog.__new__(inv_mod.InvitesCog)
    cog.bot = SimpleNamespace(clone_id=None)
    return cog


def _wire_db(monkeypatch, *, old_cache):
    db = SimpleNamespace(
        get_invite_tracker_config=AsyncMock(return_value={"enabled": True, "channel_id": 555}),
        get_invite_cache=AsyncMock(return_value=old_cache),
        replace_invite_cache=AsyncMock(),
        record_invite_join=AsyncMock(),
        get_server_listing=AsyncMock(return_value=None),
        is_disguild_invite=AsyncMock(return_value=False),
        get_inviter_stats=AsyncMock(return_value=(3, 3)),
    )
    monkeypatch.setattr(inv_mod, "db", db)


def _member_and_channel(invites):
    channel = SimpleNamespace(send=AsyncMock())
    guild = MagicMock()
    guild.id = 1
    guild.features = []
    guild.get_channel = MagicMock(return_value=channel)
    if isinstance(invites, Exception):
        guild.invites = AsyncMock(side_effect=invites)
    else:
        guild.invites = AsyncMock(return_value=invites)
    member = SimpleNamespace(id=42, bot=False, guild=guild, mention="<@42>")
    return member, channel


def _assert_no_ping(channel):
    channel.send.assert_awaited_once()
    am = channel.send.await_args.kwargs.get("allowed_mentions")
    assert isinstance(am, discord.AllowedMentions), "announcement must pass allowed_mentions"
    assert am.everyone is False and am.users is False and am.roles is False and am.replied_user is False


def test_join_with_known_inviter_mentions_both_but_pings_nobody(monkeypatch):
    _wire_db(monkeypatch, old_cache={"abc": {"uses": 1, "inviter_id": 99, "is_vanity": False}})
    invite = SimpleNamespace(code="abc", uses=2, inviter=SimpleNamespace(id=99))
    member, channel = _member_and_channel([invite])
    asyncio.run(_cog()._handle_join(member))
    text = channel.send.await_args.args[0]
    assert "<@42>" in text and "<@99>" in text      # names still show
    _assert_no_ping(channel)


def test_join_without_manage_server_permission_pings_nobody(monkeypatch):
    _wire_db(monkeypatch, old_cache={})
    forbidden = discord.Forbidden(MagicMock(status=403, reason="Forbidden"), "missing perms")
    member, channel = _member_and_channel(forbidden)
    asyncio.run(_cog()._handle_join(member))
    assert "<@42>" in channel.send.await_args.args[0]
    _assert_no_ping(channel)


def test_untracked_join_pings_nobody(monkeypatch):
    _wire_db(monkeypatch, old_cache={})
    member, channel = _member_and_channel([])        # invites readable, none used
    asyncio.run(_cog()._handle_join(member))
    assert "untracked" in channel.send.await_args.args[0]
    _assert_no_ping(channel)
