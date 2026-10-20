"""Quarantine review / ban buttons: the owner and admins must pass even with a cold member cache."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord

from discord_bot.cogs import _views_antiraid_pro as ui


def _perms(**kw):
    base = dict(administrator=False, ban_members=False, manage_guild=False)
    base.update(kw)
    return SimpleNamespace(**base)


def _guild(owner_id=1, fetched=None):
    g = MagicMock()
    g.id = 10
    g.owner_id = owner_id
    g.get_member.return_value = None            # cold cache
    g.fetch_member = AsyncMock(return_value=fetched)
    return g


def _interaction(guild, user):
    resp = MagicMock()
    resp.is_done.return_value = False
    resp.send_message = AsyncMock()
    return SimpleNamespace(client=SimpleNamespace(get_guild=lambda gid: guild, clone_id=None), user=user,
                           response=resp, followup=SimpleNamespace(send=AsyncMock()))


def run(c):
    return asyncio.run(c)


def test_owner_passes_review_with_cold_cache():
    g = _guild(owner_id=1)
    i = _interaction(g, SimpleNamespace(id=1))
    assert run(ui._auth(i, 10, "review")) is g


def test_guild_member_with_ban_members_passes_review_without_cache():
    g = _guild()
    user = MagicMock(spec=discord.Member)
    user.id = 2
    user.guild = g
    user.guild_permissions = _perms(ban_members=True)
    assert run(ui._auth(_interaction(g, user), 10, "review")) is g


def test_fetched_member_with_manage_guild_passes():
    g = _guild(fetched=SimpleNamespace(id=3, guild_permissions=_perms(manage_guild=True)))
    assert run(ui._auth(_interaction(g, SimpleNamespace(id=3)), 10, "review")) is g


def test_plain_member_is_denied():
    g = _guild(fetched=SimpleNamespace(id=4, guild_permissions=_perms()))
    i = _interaction(g, SimpleNamespace(id=4))
    assert run(ui._auth(i, 10, "review")) is None
    i.response.send_message.assert_awaited_once()
