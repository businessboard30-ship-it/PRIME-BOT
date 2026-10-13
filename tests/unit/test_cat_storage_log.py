"""Cat storage channel: stored pictures name the server they were fetched for, and cat events are logged there."""
import asyncio
import contextvars
from types import SimpleNamespace
from unittest.mock import MagicMock

import discord
import pytest

from modules import cat_log, cat_pool
from modules.cat_quality import Quality


def run(c):
    return asyncio.run(c)


# ---------- names ----------
@pytest.mark.parametrize("raw,expect", [
    ("Cat Lovers", "Cat Lovers"), ("@everyone party", "everyone party"), ("**bold** `code` [x](y)", "bold code xy"),
    ("a\nb\tc", "a b c"), ("<@123> hi", "123 hi"), ("see https://evil.example/x now", "see now"),
    ("join discord.gg/abc", "join"), ("", "fallback"), (None, "fallback"), ("   ", "fallback")])
def test_clean_name(raw, expect):
    assert cat_log.clean_name(raw, "fallback") == expect


def test_clean_name_is_length_capped():
    assert len(cat_log.clean_name("x" * 500)) == cat_log.NAME_MAX


def test_caption_names_the_server_and_has_no_pings():
    t = cat_log.pool_caption(800, 600, 123.4, "thecatapi", (42, "@everyone **Cats** HQ"))
    assert "fetched for server: everyone Cats HQ (42)" in t and "800x600" in t and "@" not in t and "*" not in t.replace("**", "")
    assert "fetched for" not in cat_log.pool_caption(800, 600, 1, "thecatapi", None)
    assert "fetched for" not in cat_log.pool_caption(800, 600, 1, "thecatapi", (None, None))


# ---------- the stored message ----------
class _Host:
    id = 7

    def __init__(self):
        self.sent = []

    async def send(self, **kw):
        self.sent.append(kw)
        return SimpleNamespace(id=99, attachments=[SimpleNamespace(url="https://cdn/x.jpg")])


def _store(monkeypatch, hint):
    host = _Host()
    from modules import image_host

    async def resolve(bot):
        return host
    monkeypatch.setattr(image_host, "resolve_cat_channel", resolve)

    class Conn:
        async def fetchval(self, sql, *a):
            return None if "SELECT 1" in sql else 5

    class Acq:
        async def __aenter__(self): return Conn()
        async def __aexit__(self, *a): return False

    class Pool:
        def acquire(self): return Acq()

    async def pool():
        return Pool()
    monkeypatch.setattr(cat_pool, "_pool", pool)
    q = Quality(ok=True, reason="", width=800, height=600, sharpness=99.0, size=1000, fmt="JPEG")
    token = cat_pool.server_hint.set(hint)
    try:
        run(cat_pool.store(MagicMock(clone_id=None), b"bytes", q, "thecatapi", "abc"))
    finally:
        cat_pool.server_hint.reset(token)
    return host


def test_store_puts_the_server_name_in_the_channel_message(monkeypatch):
    host = _store(monkeypatch, (42, "Cat Lovers"))
    assert "fetched for server: Cat Lovers (42)" in host.sent[0]["content"]


def test_store_without_a_server_still_works(monkeypatch):
    host = _store(monkeypatch, None)
    assert "fetched for" not in host.sent[0]["content"] and host.sent[0]["content"].startswith("🐱 cat pool")


def test_hint_does_not_leak_between_tasks():
    seen = []

    async def a():
        cat_pool.server_hint.set((1, "A")); await asyncio.sleep(0); seen.append(("a", cat_pool.server_hint.get()))

    async def b():
        await asyncio.sleep(0); seen.append(("b", cat_pool.server_hint.get()))

    async def go():
        await asyncio.gather(a(), b())
    run(go())
    assert dict(seen) == {"a": (1, "A"), "b": None}


# ---------- event lines ----------
F = dict(guild_id=42, guild_name="Cat Lovers", user_id=5, user_name="alice", cat_name="Mochi", rarity="rare", trait="Sleepy")


def test_event_lines():
    assert cat_log.format_event("adopt", F) == "💖 **Adopted** · **Mochi** (rare · Sleepy) · by alice (5) · server: Cat Lovers (42)"
    assert cat_log.format_event("release", F).startswith("👋 **Released** · **Mochi**")
    g = cat_log.format_event("gift", dict(F, other_id=6, other_name="bob"))
    assert "alice (5) → bob (6)" in g and g.startswith("🎁 **Gifted**")
    t = cat_log.format_event("trade", dict(F, other_id=6, other_name="bob", other_cat_name="Luna", other_rarity="common", other_trait="Gentle"))
    assert "**Mochi** (rare · Sleepy) ⇄ **Luna** (common · Gentle)" in t and "alice (5) ⇄ bob (6)" in t
    assert "Left home" in cat_log.format_event("runaway", F) and "Back in the spawn" in cat_log.format_event("returned", F)


def test_event_lines_are_clean_and_ping_free():
    t = cat_log.format_event("adopt", dict(F, user_name="@everyone <@1> `x`", guild_name="@here **x**", cat_name="@everyone"))
    assert "@" not in t and "<" not in t and "`" not in t


# ---------- queue + worker ----------
@pytest.fixture
def host(monkeypatch):
    h = _Host()
    from modules import image_host

    async def resolve(bot):
        return h
    monkeypatch.setattr(image_host, "resolve_cat_channel", resolve)

    class Conn:
        async def fetchrow(self, sql, *a):
            return {"channel_id": 7, "message_id": 99} if a[0] == 5 else None

    class Acq:
        async def __aenter__(self): return Conn()
        async def __aexit__(self, *a): return False

    class Pool:
        def acquire(self): return Acq()

    async def pool():
        return Pool()
    import database
    monkeypatch.setattr(database, "get_pool", pool)
    monkeypatch.setattr(cat_log, "SEND_GAP_SECONDS", 0)
    cat_log._queue = None; cat_log._worker = None
    return h


def test_event_is_a_reply_to_the_pictures_message_with_no_mentions(host):
    async def go():
        cat_log.record(MagicMock(), "adopt", pool_id=5, **F)
        await asyncio.sleep(0.05)
    run(go())
    sent = host.sent[0]
    assert sent["reference"].message_id == 99 and sent["reference"].channel_id == 7 and sent["reference"].fail_if_not_exists is False
    assert sent["allowed_mentions"].everyone is False and sent["allowed_mentions"].users is False
    assert sent["content"].endswith("picture #5") and "Adopted" in sent["content"]


def test_event_for_an_unknown_picture_is_still_logged_without_a_reply(host):
    async def go():
        cat_log.record(MagicMock(), "release", pool_id=777, **F)
        await asyncio.sleep(0.05)
    run(go())
    assert host.sent[0]["reference"] is None and "Released" in host.sent[0]["content"]


def test_a_failing_channel_never_raises_and_the_next_event_still_goes(host):
    calls = []

    async def flaky(**kw):
        calls.append(kw)
        if len(calls) == 1:
            raise discord.HTTPException(SimpleNamespace(status=500, reason="x"), "boom")
    host.send = flaky

    async def go():
        cat_log.record(MagicMock(), "adopt", **F)
        cat_log.record(MagicMock(), "adopt", **F)
        await asyncio.sleep(0.1)
    run(go())
    assert len(calls) == 2


def test_no_storage_channel_means_the_event_is_silently_dropped(monkeypatch):
    from modules import image_host

    async def none(bot):
        return None
    monkeypatch.setattr(image_host, "resolve_cat_channel", none)
    monkeypatch.setattr(cat_log, "SEND_GAP_SECONDS", 0)
    cat_log._queue = None; cat_log._worker = None

    async def go():
        cat_log.record(MagicMock(), "adopt", **F)
        await asyncio.sleep(0.05)
    run(go())


def test_full_queue_drops_instead_of_blocking(monkeypatch):
    monkeypatch.setattr(cat_log, "QUEUE_MAX", 2)
    cat_log._queue = None; cat_log._worker = None; cat_log.dropped = 0

    async def go():
        cat_log._queue = asyncio.Queue(maxsize=2)
        cat_log._worker = asyncio.get_running_loop().create_future()      # a "worker" that never drains
        for _ in range(5):
            cat_log.record(MagicMock(), "adopt", **F)
    run(go())
    assert cat_log.dropped == 3


def test_record_outside_an_event_loop_never_raises():
    cat_log.record(MagicMock(), "adopt", **F)


# ---------- wiring ----------
def test_adopt_release_and_trade_are_logged_from_the_views():
    import inspect
    from discord_bot.cogs import _views_cat_adopt as v
    assert 'cat_log.record(interaction.client, "adopt"' in inspect.getsource(v.AdoptButton.callback)
    assert 'cat_log.record(interaction.client, "release"' in inspect.getsource(v.CollectionView._release)
    assert '"trade" if self.take else "gift"' in inspect.getsource(v.TradeOfferView._accept)
    # the adopt log comes after the cat exists, never before a failed adopt
    src = inspect.getsource(v.AdoptButton.callback)
    assert src.index('res.status == "limit"') < src.index("cat_log.record")


def test_release_is_only_logged_when_something_was_actually_released():
    import inspect
    from discord_bot.cogs import _views_cat_adopt as v
    assert "if await ca.release(cat.id, interaction.user.id):" in inspect.getsource(v.CollectionView._release)


def test_posting_a_cat_sets_and_resets_the_server_hint():
    import inspect
    from discord_bot.cogs import cat
    for fn in (cat.post_cat, cat.send_cat):
        s = inspect.getsource(fn)
        assert "cat_pool.server_hint.set(" in s and "cat_pool.server_hint.reset(" in s and "finally:" in s
