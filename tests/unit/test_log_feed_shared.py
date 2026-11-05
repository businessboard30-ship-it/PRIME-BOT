"""Shared log feed: DB store, id parsing, and the main-bot / clone behaviour of the cog (fakes, no Discord)."""
import asyncio
import logging
from types import SimpleNamespace as N

import discord

from discord_bot.cogs import log_channel as lc
from modules import log_feed_store as st

GOOD = "https://discord.com/api/" + "webhooks/" + "123456789012345678" + "/" + "abc_DEF-123"   # built at runtime: never a literal URL in the repo


def run(c):
    return asyncio.run(c)


class Conn:
    def __init__(self): self.row = None
    async def execute(self, sql, *a):
        if sql.startswith("INSERT INTO bot_log_feed"): self.row = {"channel_id": a[0], "webhook_url": a[1]}
    async def fetchrow(self, sql, *a): return self.row


class Pool:
    def __init__(self, conn): self.conn = conn
    def acquire(self): return self
    async def __aenter__(self): return self.conn
    async def __aexit__(self, *a): return False


def fresh(monkeypatch):
    conn = Conn()

    async def pool(): return Pool(conn)
    monkeypatch.setattr(st, "_pool", pool)
    monkeypatch.setattr(st, "_cache", (0.0, None, None))
    return conn


def test_parse_channel_id():
    assert st.parse_channel_id("123456789012345678") == 123456789012345678
    assert st.parse_channel_id("<#123456789012345678>") == 123456789012345678
    assert st.parse_channel_id("  123456789012345678 ") == 123456789012345678
    for bad in ("", "hello", "123", "12345678901234567 8", "https://discord.com/channels/1/2"):
        assert st.parse_channel_id(bad) is None


def test_webhook_url_validation():
    assert st.valid_webhook_url(GOOD)
    for bad in (None, "", "http://discord.com/api/webhooks/1/x", "https://evil.com/api/webhooks/123456789012345678/abc",
                "https://discord.com/api/webhooks/abc/x"):
        assert not st.valid_webhook_url(bad)


def test_store_roundtrip_drops_a_bad_webhook_and_caches(monkeypatch):
    conn = fresh(monkeypatch)
    assert run(st.get()) == (None, None)
    run(st.save(111111111111111111, GOOD))
    assert conn.row == {"channel_id": 111111111111111111, "webhook_url": GOOD}
    run(st.save(222222222222222222, "https://evil.example/hook"))
    assert conn.row["webhook_url"] is None                                      # never store a non-Discord URL
    conn.row = {"channel_id": 333333333333333333, "webhook_url": GOOD}
    assert run(st.get()) == (222222222222222222, None)                          # served from the 60 s cache
    assert run(st.get(force=True)) == (333333333333333333, GOOD)


def test_store_never_raises_when_the_database_fails(monkeypatch):
    monkeypatch.setattr(st, "_cache", (0.0, 5, GOOD))

    async def boom(): raise RuntimeError("db down")
    monkeypatch.setattr(st, "_pool", boom)
    assert run(st.get()) == (5, GOOD)                                           # last known value, not an exception


def test_forget_webhook_keeps_the_channel(monkeypatch):
    conn = fresh(monkeypatch)
    run(st.save(444444444444444444, GOOD))
    run(st.forget_webhook())
    assert conn.row == {"channel_id": 444444444444444444, "webhook_url": None}


# ── cog ──

class Chan:
    def __init__(self, cid, name="bot-logs"): self.id, self.name, self.sent = cid, name, []
    async def send(self, text, **kw): self.sent.append(text)


def make_cog(clone_id=None, channels=None):
    bot = N(clone_id=clone_id, is_ready=lambda: True, get_channel=lambda i: (channels or {}).get(i), loop=None,
            get_guild=lambda i: None)
    holder = {}

    async def go():
        holder["cog"] = lc.LogChannelCog(bot)
        holder["cog"].flush.cancel()
    run(go())
    return holder["cog"]


def test_clone_posts_through_the_shared_webhook_and_never_sets_anything_up(monkeypatch):
    fresh(monkeypatch)
    monkeypatch.delenv("LOG_CHANNEL_ID", raising=False)
    run(st.save(555555555555555555, GOOD))
    sent = []

    class Hook:
        async def send(self, text, **kw): sent.append((text, kw["username"]))

    async def go():
        cog = lc.LogChannelCog(N(clone_id=7, is_ready=lambda: True, get_channel=lambda i: None, loop=None))
        cog.flush.cancel()

        async def hook(url): return Hook()
        cog._webhook = hook
        setup_calls = []

        async def no_setup(): setup_calls.append(1)
        cog._setup_main = no_setup
        ok = await cog._send("hello")
        return ok, setup_calls
    ok, calls = run(go())
    assert ok and sent == [("hello", "clone #7")] and calls == []


def test_send_falls_back_to_the_channel_when_the_webhook_is_gone(monkeypatch):
    fresh(monkeypatch)
    monkeypatch.delenv("LOG_CHANNEL_ID", raising=False)
    run(st.save(666666666666666666, GOOD))
    chan = Chan(666666666666666666)

    async def go():
        cog = lc.LogChannelCog(N(clone_id=None, is_ready=lambda: True, get_channel=lambda i: chan, loop=None))
        cog.flush.cancel()

        class Dead:
            async def send(self, *a, **kw): raise discord.NotFound(N(status=404, reason="x"), "gone")

        async def hook(url): return Dead()
        cog._webhook = hook
        return await cog._send("hi")
    assert run(go()) is True and chan.sent == ["hi"]
    assert run(st.get(force=True))[1] is None                                 # the dead webhook was forgotten


def test_nothing_configured_means_not_delivered_and_no_crash(monkeypatch):
    fresh(monkeypatch)
    monkeypatch.delenv("LOG_CHANNEL_ID", raising=False)

    async def go():
        cog = lc.LogChannelCog(N(clone_id=3, is_ready=lambda: True, get_channel=lambda i: None, loop=None))
        cog.flush.cancel()
        return await cog._send("x")
    assert run(go()) is False


def test_owner_dm_reply_saves_the_channel_for_everyone(monkeypatch):
    conn = fresh(monkeypatch)
    monkeypatch.delenv("LOG_CHANNEL_ID", raising=False)
    chan = Chan(777777777777777777, "my-logs")
    replies = []
    dm = N(send=lambda t: _a(replies.append(t)))

    async def _a(x): return x

    async def go():
        cog = lc.LogChannelCog(N(clone_id=None, is_ready=lambda: True, get_channel=lambda i: chan if i == chan.id else None, loop=None))
        cog.flush.cancel()
        cog._owner_ids = lambda: {42}

        async def mk(c): return GOOD
        cog._make_webhook = mk
        cog._awaiting_id = True
        msg = lambda text, uid=42: N(author=N(bot=False, id=uid), guild=None, content=text, channel=dm)
        await cog.on_message(msg("hello"))                                       # not an id: ignored, still waiting
        await cog.on_message(msg("777777777777777777", uid=99))                  # not the owner: ignored
        still = cog._awaiting_id
        await cog.on_message(msg("999999999999999999"))                          # a channel the bot can't see
        await cog.on_message(msg("777777777777777777"))                          # the owner's real answer
        return still, cog._awaiting_id
    still, now = run(go())
    assert still is True and now is False
    assert conn.row == {"channel_id": 777777777777777777, "webhook_url": GOOD}
    assert any("can't see" in r for r in replies) and any("Saved" in r for r in replies)


def test_clones_ignore_the_dm_flow_and_setup_is_main_only():
    src = open("discord_bot/cogs/log_channel.py").read()
    assert "self._is_main() and self._awaiting_id" in src and "if self._is_main() and log_feed.channel_id() is None" in src
    assert "logger." not in src.split("async def flush")[1].split("async def setup")[0]
    assert "webhook_url" not in src.replace("log_feed_store", "")                # the secret URL is never written into messages
