import asyncio
import sys
import types

import discord

# database.py needs asyncpg, which isn't needed for these checks.
sys.modules.setdefault("asyncpg", types.SimpleNamespace())


def test_mentions_for_skips_muted_users(monkeypatch):
    from discord_bot.cogs import leveling

    async def fake_muted(guild_id, ids):
        return {2}
    monkeypatch.setattr(leveling.db, "get_level_ping_muted", fake_muted)
    cog = object.__new__(leveling.LevelingCog) if hasattr(leveling, "LevelingCog") else None
    assert cog is not None
    am = asyncio.run(cog._mentions_for(10, 1, 2, None))
    assert [u.id for u in am.users] == [1]
    assert am.everyone is False and am.roles is False


def test_mentions_for_falls_back_when_lookup_fails(monkeypatch):
    from discord_bot.cogs import leveling

    async def boom(guild_id, ids):
        raise RuntimeError("db down")
    monkeypatch.setattr(leveling.db, "get_level_ping_muted", boom)
    cog = object.__new__(leveling.LevelingCog)
    am = asyncio.run(cog._mentions_for(10, 5))
    assert [u.id for u in am.users] == [5]


def test_one_level_up_pings_a_member_only_once(monkeypatch):
    from discord_bot.cogs import leveling

    async def none_muted(guild_id, ids):
        return set()
    monkeypatch.setattr(leveling.db, "get_level_ping_muted", none_muted)
    cog = object.__new__(leveling.LevelingCog)
    pinged = set()
    first = asyncio.run(cog._mentions_for(10, 7, 8, pinged=pinged))     # chief announcement (new + old chief)
    second = asyncio.run(cog._mentions_for(10, 7, pinged=pinged))       # level-up card for the same member
    third = asyncio.run(cog._mentions_for(10, 7, pinged=pinged))        # clan card caption
    assert [u.id for u in first.users] == [7, 8]
    assert second.users == [] and third.users == []
    other = asyncio.run(cog._mentions_for(10, 9, pinged=pinged))        # someone else is still pinged
    assert [u.id for u in other.users] == [9]


def test_without_a_shared_set_pings_are_unchanged(monkeypatch):
    from discord_bot.cogs import leveling

    async def none_muted(guild_id, ids):
        return set()
    monkeypatch.setattr(leveling.db, "get_level_ping_muted", none_muted)
    cog = object.__new__(leveling.LevelingCog)
    assert [u.id for u in asyncio.run(cog._mentions_for(10, 7)).users] == [7]
    assert [u.id for u in asyncio.run(cog._mentions_for(10, 7)).users] == [7]
