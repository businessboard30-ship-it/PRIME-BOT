"""A member caught by Scam Shield in one server is flagged to the staff of other servers they join."""
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from modules import scam_reputation as rep

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)


def run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


# ── the lookup ──
class _Pool:
    def __init__(self, row=None, boom=False):
        self.row, self.boom, self.args = row, boom, None
        pool = self

        class Conn:
            async def fetchrow(self, sql, *args):
                if pool.boom:
                    raise RuntimeError("db down")
                pool.args, pool.sql = args, sql
                return pool.row

        class Acq:
            async def __aenter__(self): return Conn()
            async def __aexit__(self, *a): return False
        self.acquire = lambda: Acq()


def _use(monkeypatch, pool):
    async def get(): return pool
    monkeypatch.setattr(rep, "_pool", get)


def test_lookup_returns_counts_and_friendly_kinds_only(monkeypatch):
    pool = _Pool({"catches": 3, "servers": 2, "last_at": NOW, "kinds": ["domain", "vision", "weird"]})
    _use(monkeypatch, pool)
    out = run(rep.lookup(42, 555))
    assert out == {"catches": 3, "servers": 2, "last_at": NOW,
                   "kinds": ["a known scam link", "an AI-detected scam image", "a scam pattern"]}
    assert pool.args == (42, 555, rep.LOOKBACK_DAYS)                     # excludes THIS server, bounded window
    assert "guild_id <> $2" in pool.sql and "snippet" not in pool.sql and "matched" not in pool.sql   # never reads the message text


def test_lookup_none_when_nothing_or_failure(monkeypatch):
    _use(monkeypatch, _Pool({"catches": 0, "servers": 0, "last_at": None, "kinds": []}))
    assert run(rep.lookup(42, 555)) is None
    _use(monkeypatch, _Pool(None))
    assert run(rep.lookup(42, 555)) is None
    _use(monkeypatch, _Pool(boom=True))
    assert run(rep.lookup(42, 555)) is None                              # a DB hiccup is not a false alarm


# ── the join warning ──
def _chan(cid, can=True):
    perms = SimpleNamespace(view_channel=can, send_messages=can, embed_links=can)
    ch = MagicMock(spec=discord.TextChannel)
    ch.id, ch.position, ch.mention = cid, cid, f"<#{cid}>"
    ch.permissions_for = lambda me: perms
    ch.send = AsyncMock()
    return ch


@pytest.fixture()
def j(monkeypatch):
    from discord_bot.cogs import scam_shield as cm
    cm._warned.clear()
    cog = cm.ScamShieldCog.__new__(cm.ScamShieldCog)
    cog.bot = SimpleNamespace(clone_id=None)
    st = SimpleNamespace(cm=cm, cog=cog, modlog=None, channels=[], system=None, log_id=None)
    monkeypatch.setattr(cm.ss, "is_enabled", lambda: True)
    monkeypatch.setattr(cm.ss, "guild_settings", AsyncMock(return_value={"enabled": True, "allowed_domains": ()}))
    found = {"catches": 2, "servers": 2, "last_at": NOW, "kinds": ["a known scam link"]}
    st.lookup = AsyncMock(return_value=found)
    monkeypatch.setattr(cm.rep, "lookup", st.lookup)
    monkeypatch.setattr(cm.db, "get_automod_config", AsyncMock(side_effect=lambda gid, clone_id=None: {"log_channel_id": st.log_id}), raising=False)
    guild = MagicMock()
    guild.id, guild.me = 555, object()
    guild.get_channel = lambda cid: next((c for c in st.channels if c.id == cid), None)
    type(guild).system_channel = property(lambda s: st.system)
    type(guild).text_channels = property(lambda s: list(st.channels))
    st.guild = guild
    m = MagicMock()
    m.bot, m.id, m.guild = False, 42, guild
    m.__str__ = lambda s: "scammer"
    st.member = m
    return st


def test_modlog_gets_a_permanent_flag_with_support_and_evidence(j):
    modlog = _chan(100); j.channels, j.log_id = [modlog], 100
    assert run(j.cog._warn_staff_about_join(j.member)) is True
    kw = modlog.send.await_args.kwargs
    e = kw["embed"]
    assert "delete_after" not in kw
    assert "scammer" in e.description and "`42`" in e.description and "**2** other servers" in e.description and "2 catches" in e.description
    assert "Nothing has been done" in e.description and "hacked" in e.description
    ev = next(f.value for f in e.fields if f.name == "Evidence")
    assert "evidence supporting this flag" in ev and "contact support" in ev and "discord.gg/" in ev
    assert "a known scam link" in next(f.value for f in e.fields if f.name == "What was caught")
    assert kw["allowed_mentions"].users is False or kw["allowed_mentions"].users == []
    j.lookup.assert_awaited_once_with(42, 555)                              # looks for catches in OTHER servers


def test_no_modlog_posts_a_self_deleting_flag_elsewhere(j):
    other = _chan(60); j.channels = [other]
    assert run(j.cog._warn_staff_about_join(j.member)) is True
    kw = other.send.await_args.kwargs
    assert kw["delete_after"] == j.cm.JOIN_FALLBACK_SECONDS
    d = kw["embed"].description
    assert "will be deleted in a short time" in d and "/modlog" in d


def test_prefers_system_channel_and_skips_unwritable_modlog(j):
    modlog, system, other = _chan(100, can=False), _chan(50), _chan(60)
    j.channels, j.log_id, j.system = [modlog, other], 100, system
    run(j.cog._warn_staff_about_join(j.member))
    system.send.assert_awaited_once(); modlog.send.assert_not_awaited(); other.send.assert_not_awaited()


def test_nowhere_to_post_is_quiet(j):
    j.channels = [_chan(60, can=False)]
    assert run(j.cog._warn_staff_about_join(j.member)) is False


def test_no_flag_for_clean_members_bots_or_a_server_with_scam_shield_off(j, monkeypatch):
    modlog = _chan(100); j.channels, j.log_id = [modlog], 100
    j.lookup.return_value = None
    assert run(j.cog._warn_staff_about_join(j.member)) is False
    j.lookup.return_value = {"catches": 1, "servers": 1, "last_at": None, "kinds": []}
    j.member.bot = True
    assert run(j.cog._warn_staff_about_join(j.member)) is False
    j.member.bot = False
    monkeypatch.setattr(j.cm.ss, "guild_settings", AsyncMock(return_value={"enabled": False, "allowed_domains": ()}))
    assert run(j.cog._warn_staff_about_join(j.member)) is False
    monkeypatch.setattr(j.cm.ss, "is_enabled", lambda: False)
    assert run(j.cog._warn_staff_about_join(j.member)) is False
    modlog.send.assert_not_awaited()


def test_rejoin_does_not_repeat_the_warning(j):
    modlog = _chan(100); j.channels, j.log_id = [modlog], 100
    assert run(j.cog._warn_staff_about_join(j.member)) is True
    assert run(j.cog._warn_staff_about_join(j.member)) is False
    assert modlog.send.await_count == 1


def test_listener_never_raises(j, monkeypatch):
    monkeypatch.setattr(j.cm.rep, "lookup", AsyncMock(side_effect=RuntimeError("boom")))
    run(j.cog.on_member_join(j.member))                                     # must not raise into discord.py


def test_flag_never_names_other_servers_or_repeats_scam_text(j):
    modlog = _chan(100); j.channels, j.log_id = [modlog], 100
    run(j.cog._warn_staff_about_join(j.member))
    blob = str(modlog.send.await_args.kwargs["embed"].to_dict())
    assert "guild" not in blob.lower() and "snippet" not in blob.lower()
