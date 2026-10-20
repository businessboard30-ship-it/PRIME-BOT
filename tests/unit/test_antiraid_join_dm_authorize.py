"""Anti-raid button on the join DM: the server owner must pass _authorize even when the
member cache is cold (get_member -> None, guild.owner -> None), as happens from a DM click."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from discord_bot.cogs import antiraid


def _interaction(guild, user_id):
    resp = MagicMock()
    resp.is_done.return_value = True
    return SimpleNamespace(
        client=SimpleNamespace(get_guild=lambda gid: guild),
        user=SimpleNamespace(id=user_id),
        response=resp,
        followup=SimpleNamespace(send=AsyncMock()),
    )


def _guild(owner_id=1, member=None):
    g = MagicMock()
    g.owner_id = owner_id
    g.owner = None  # cold cache
    g.get_member.return_value = None
    g.fetch_member = AsyncMock(return_value=member)
    return g


def test_owner_passes_with_cold_cache():
    g = _guild(owner_id=1)
    i = _interaction(g, 1)
    assert asyncio.run(antiraid._authorize(i, 123)) is g
    i.followup.send.assert_not_called()


def test_fetched_member_with_manage_guild_passes():
    member = SimpleNamespace(guild_permissions=SimpleNamespace(administrator=False, manage_guild=True))
    g = _guild(owner_id=1, member=member)
    assert asyncio.run(antiraid._authorize(_interaction(g, 2), 123)) is g


def test_member_without_permission_is_denied():
    member = SimpleNamespace(guild_permissions=SimpleNamespace(administrator=False, manage_guild=False))
    g = _guild(owner_id=1, member=member)
    i = _interaction(g, 2)
    assert asyncio.run(antiraid._authorize(i, 123)) is None
    i.followup.send.assert_awaited_once()
