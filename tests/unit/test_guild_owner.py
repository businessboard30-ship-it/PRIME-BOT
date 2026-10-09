"""discord.py has no Guild.fetch_owner(); the owner join DM and automod reminders must use resolve_guild_owner."""
import asyncio
import types
from pathlib import Path

import discord

from utils.guild_owner import resolve_guild_owner


def run(c):
    return asyncio.run(c)


class _G:
    def __init__(self, owner=None, owner_id=5, member=None, exc=None):
        self.owner, self.owner_id, self._member, self._exc = owner, owner_id, member, exc
        self.calls = []
        client = types.SimpleNamespace(fetch_user=self._fetch_user)
        self._state = types.SimpleNamespace(_get_client=lambda: client)

    async def fetch_member(self, uid):
        self.calls.append(("member", uid))
        if self._exc:
            raise self._exc
        return self._member

    async def _fetch_user(self, uid):
        self.calls.append(("user", uid))
        return types.SimpleNamespace(id=uid, kind="user")


def _resp():
    return types.SimpleNamespace(status=404, reason="x")


def test_real_discord_py_has_no_fetch_owner():
    assert not hasattr(discord.Guild, "fetch_owner") and hasattr(discord.Guild, "fetch_member")


def test_cached_owner_is_returned_without_http():
    g = _G(owner="cached")
    assert run(resolve_guild_owner(g)) == "cached" and g.calls == []


def test_uncached_owner_is_fetched_as_a_member():
    g = _G(member="member")
    assert run(resolve_guild_owner(g)) == "member" and g.calls == [("member", 5)]


def test_not_a_member_falls_back_to_user_fetch():
    g = _G(exc=discord.NotFound(_resp(), "gone"))
    assert run(resolve_guild_owner(g)).kind == "user" and g.calls == [("member", 5), ("user", 5)]


def test_http_errors_never_escape():
    g = _G(exc=discord.Forbidden(types.SimpleNamespace(status=403, reason="x"), "no"))
    assert run(resolve_guild_owner(g)).kind == "user"

    class Broken(_G):
        async def _fetch_user(self, uid):
            raise RuntimeError("down")
    assert run(resolve_guild_owner(Broken(exc=discord.NotFound(_resp(), "gone")))) is None


def test_no_owner_id_returns_none():
    assert run(resolve_guild_owner(_G(owner_id=0))) is None


def test_source_never_calls_fetch_owner_again():
    root = Path(__file__).resolve().parents[2]
    bad = [str(p) for d in ("discord_bot", "api", "modules", "utils") for p in (root / d).rglob("*.py")
           if "guild.fetch_owner(" in p.read_text(encoding="utf-8", errors="ignore")]
    assert not bad, bad
