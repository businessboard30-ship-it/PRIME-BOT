"""Scam Shield with more than one bot in a server.

The main bot, every clone, or two copies of one bot can all sit in the same server and all receive the same
message event. Before the claim, each of them deleted / archived / logged / warned: the losers got "not found" and
reported "couldn't delete", and staff saw duplicate warnings, as if the bots were fighting. Now exactly one acts.
"""
import asyncio
import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from discord_bot.cogs import scam_shield as cog_mod
from modules import scam_shield as ss


def run(c):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(c)


# ── a stand-in for the shared Postgres: one table, one unique key ────────

class FakeDB:
    def __init__(self):
        self.keys = {}
        self.sql = []

    def acquire(self):
        db = self

        class _Ctx:
            async def __aenter__(self_):
                return db

            async def __aexit__(self_, *a):
                return False
        return _Ctx()

    async def execute(self, sql, *args):
        self.sql.append(sql)

    async def fetchrow(self, sql, key, guild_id, clone_id):
        if key in self.keys:
            return None                        # ON CONFLICT DO NOTHING -> no row returned
        self.keys[key] = (guild_id, clone_id)
        return {"claim_key": key}


@pytest.fixture()
def shared_db(monkeypatch):
    db = FakeDB()

    async def pool():
        return db
    monkeypatch.setattr(ss, "_pool", pool)
    monkeypatch.setattr(ss, "_claims_ready", False)
    return db


@pytest.fixture(autouse=True)
def rules(monkeypatch):
    ss._c.words, ss._c.domains, ss._c.images = [(1, "fatowin")], [(2, "fatowin.com")], []
    ss._c.enabled, ss._c.loaded_at = True, 10 ** 12
    ss._last_hit.clear()
    cog_mod._warned.clear()
    monkeypatch.setattr(ss, "CLAIM_GRACE_SECONDS", 0.05)       # keep the "can't delete -> wait" tests fast
    yield


# ── ss.claim ─────────────────────────────────────────────────────────────

def test_first_claim_wins_and_the_rest_lose(shared_db):
    async def go():
        assert await ss.claim("msg:1", 555, None) is True
        assert await ss.claim("msg:1", 555, 7) is False       # a clone, same message
        assert await ss.claim("msg:1", 555, None) is False    # a second copy of the same bot
        assert await ss.claim("msg:2", 555, 7) is True        # a different message is independent
    run(go())


def test_claim_fails_open_when_the_database_is_down(monkeypatch):
    async def boom():
        raise ConnectionError("db down")
    monkeypatch.setattr(ss, "_pool", boom)
    # a duplicate warning is better than a scam left up
    assert run(ss.claim("msg:1", 555, None)) is True


def test_claim_table_is_created_once_not_per_message(shared_db):
    async def go():
        for i in range(5):
            await ss.claim(f"msg:{i}", 1, None)
        assert sum("CREATE TABLE IF NOT EXISTS scam_shield_claims" in q for q in shared_db.sql) == 1
    run(go())


def test_old_claims_are_swept_now_and_then(shared_db, monkeypatch):
    async def go():
        monkeypatch.setattr(ss.random, "random", lambda: 0.5)
        await ss.claim("msg:1", 1, None)
        assert not any("DELETE FROM scam_shield_claims" in q for q in shared_db.sql)
        monkeypatch.setattr(ss.random, "random", lambda: 0.0)
        await ss.claim("msg:2", 1, None)
        assert any("DELETE FROM scam_shield_claims" in q for q in shared_db.sql)
    run(go())


# ── two bots, one server ─────────────────────────────────────────────────

def make_cog(monkeypatch, bot_id, clone_id=None):
    bot = MagicMock()
    bot.user, bot.clone_id = SimpleNamespace(id=bot_id), clone_id
    c = cog_mod.ScamShieldCog.__new__(cog_mod.ScamShieldCog)
    c.bot = bot
    c.log, c.flag, c.notice = AsyncMock(), AsyncMock(return_value=None), AsyncMock()
    monkeypatch.setattr(cog_mod.ss, "log_hit", c.log)
    monkeypatch.setattr(cog_mod.ss, "load", AsyncMock())
    monkeypatch.setattr(c, "_flag", c.flag, raising=False)
    monkeypatch.setattr(c, "_notice", c.notice, raising=False)
    monkeypatch.setattr(c, "_evidence", AsyncMock(return_value=(None, [])), raising=False)
    return c


def make_message(msg_id=900001, can_delete=True, delete_error=None):
    guild = SimpleNamespace(id=555, owner_id=1, get_channel=lambda cid: None, me=SimpleNamespace())
    perms = SimpleNamespace(administrator=False, manage_guild=False, manage_messages=False)
    author = MagicMock(spec=discord.Member)
    author.id, author.guild_permissions = 42, perms
    m = MagicMock()
    m.id, m.guild, m.author, m.content, m.embeds, m.attachments = msg_id, guild, author, "free $3500 at fatowin.com", [], []
    m.type = discord.MessageType.default
    m.channel = SimpleNamespace(id=7, mention="#c",
                                permissions_for=lambda me: SimpleNamespace(manage_messages=can_delete))
    m.delete = AsyncMock(side_effect=delete_error)
    return m


def test_two_bots_seeing_one_scam_message_act_exactly_once(shared_db, monkeypatch):
    async def go():
        main, clone = make_cog(monkeypatch, 999, None), make_cog(monkeypatch, 998, 5)
        m = make_message()
        results = await asyncio.gather(main._inspect(m), clone._inspect(m))
        assert sorted(results) == [False, True]                 # one acted, one stood down
        m.delete.assert_awaited_once()                          # no second delete -> no false "couldn't delete"
        assert main.log.await_count + clone.log.await_count == 1
        assert main.flag.await_count + clone.flag.await_count == 1
    run(go())


def test_three_bots_still_act_once(shared_db, monkeypatch):
    async def go():
        bots = [make_cog(monkeypatch, 990 + i, i or None) for i in range(3)]
        m = make_message()
        await asyncio.gather(*(b._inspect(m) for b in bots))
        m.delete.assert_awaited_once()
        assert sum(b.flag.await_count for b in bots) == 1
    run(go())


def test_a_different_message_is_handled_even_if_the_first_was_claimed(shared_db, monkeypatch):
    async def go():
        a, b = make_cog(monkeypatch, 999), make_cog(monkeypatch, 998, 5)
        m1, m2 = make_message(1), make_message(2)
        await a._inspect(m1)
        ss._last_hit.clear()                                    # the per-user throttle is its own thing
        assert await b._inspect(m2) is True
        m1.delete.assert_awaited_once()
        m2.delete.assert_awaited_once()
    run(go())


def test_an_edited_message_is_not_handled_twice(shared_db, monkeypatch):
    async def go():
        a = make_cog(monkeypatch, 999)
        m = make_message()
        assert await a._inspect(m) is True
        assert await a._inspect(m) is False                     # on_message_edit fires _inspect again
        m.delete.assert_awaited_once()
    run(go())


def test_the_bot_that_can_delete_goes_first_even_if_it_is_slower(shared_db, monkeypatch):
    async def go():
        weak, strong = make_cog(monkeypatch, 999), make_cog(monkeypatch, 998, 5)
        m_weak, m_strong = make_message(can_delete=False), make_message(can_delete=True)
        m_strong.delete = m_weak.delete                          # same Discord message
        # the weak bot's event arrives first; the strong one should still win the claim
        results = await asyncio.gather(weak._inspect(m_weak), strong._inspect(m_strong))
        assert results == [False, True]
        m_weak.delete.assert_awaited_once()
        assert strong.flag.await_count == 1 and weak.flag.await_count == 0
    run(go())


def test_when_nobody_can_delete_one_bot_still_flags_it(shared_db, monkeypatch):
    async def go():
        a, b = make_cog(monkeypatch, 999), make_cog(monkeypatch, 998, 5)
        err = discord.Forbidden(MagicMock(status=403), "Missing Permissions")
        ma, mb = make_message(can_delete=False, delete_error=err), make_message(can_delete=False, delete_error=err)
        results = await asyncio.gather(a._inspect(ma), b._inspect(mb))
        assert sorted(results) == [False, True]
        assert a.flag.await_count + b.flag.await_count == 1     # staff hear about it once, "couldn't delete"
        assert (a.log.await_args or b.log.await_args).args[7] is False   # recorded as not deleted
    run(go())


def test_an_unreachable_claim_table_still_removes_the_scam(monkeypatch):
    async def go():
        async def boom():
            raise ConnectionError("db down")
        monkeypatch.setattr(ss, "_pool", boom)
        a = make_cog(monkeypatch, 999)
        m = make_message()
        assert await a._inspect(m) is True
        m.delete.assert_awaited_once()
    run(go())


def test_normal_messages_never_touch_the_claim_table(shared_db, monkeypatch):
    async def go():
        a = make_cog(monkeypatch, 999)
        m = make_message()
        m.content = "anyone up for a game tonight?"
        assert await a._inspect(m) is False
        assert shared_db.keys == {} and shared_db.sql == []      # zero extra database work for clean chat
    run(go())


# ── the join warning ─────────────────────────────────────────────────────

def make_join(monkeypatch, cog):
    sent = AsyncMock()
    ch = SimpleNamespace(send=sent)
    guild = SimpleNamespace(id=555, get_channel=lambda cid: ch, me=SimpleNamespace())
    member = SimpleNamespace(id=42, bot=False, guild=guild, joined_at=dt.datetime(2026, 10, 14, 9, 0, tzinfo=dt.timezone.utc),
                             __str__=lambda s: "scammer")
    monkeypatch.setattr(cog_mod.ss, "guild_settings", AsyncMock(return_value={"enabled": True}))
    monkeypatch.setattr(cog_mod.ss, "is_enabled", lambda: True)
    monkeypatch.setattr(cog_mod.rep, "lookup", AsyncMock(return_value={"servers": 2, "catches": 3, "last_at": None, "kinds": []}))
    monkeypatch.setattr(cog_mod.db, "get_automod_config", AsyncMock(return_value={"log_channel_id": 10}))
    monkeypatch.setattr(cog_mod.ScamShieldCog, "_can_post", staticmethod(lambda ch, g: True))
    return member, sent


def test_staff_get_one_join_warning_not_one_per_bot(shared_db, monkeypatch):
    async def go():
        main, clone = make_cog(monkeypatch, 999), make_cog(monkeypatch, 998, 5)
        member, sent = make_join(monkeypatch, main)
        assert await main._warn_staff_about_join(member) is True
        cog_mod._warned.clear()                                  # the clone is a separate process: its own memory
        assert await clone._warn_staff_about_join(member) is False
        sent.assert_awaited_once()
    run(go())


def test_a_rejoin_later_is_a_new_join_and_warns_again(shared_db, monkeypatch):
    async def go():
        main = make_cog(monkeypatch, 999)
        member, sent = make_join(monkeypatch, main)
        assert await main._warn_staff_about_join(member) is True
        cog_mod._warned.clear()
        member.joined_at = member.joined_at + dt.timedelta(days=1)
        assert await main._warn_staff_about_join(member) is True
        assert sent.await_count == 2
    run(go())
