"""Cat pool: a server is shown a pooled picture it hasn't seen lately; the cat API is only called when it has
seen them all. Mocked logic tests always run; the real-SQL test runs when CAT_TEST_PG_DSN points at a Postgres."""
import asyncio
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from discord_bot.cogs import cat
from modules import cat_pool
from tests.unit.test_cat_setup import sharp_bytes  # noqa: F401  (shared fixture: a sharp test JPEG)

ROOT = Path(__file__).resolve().parents[2]


def run(c):
    return asyncio.run(c)


class FakeSession:
    pass


@pytest.fixture()
def env(monkeypatch, sharp_bytes):
    cat._recent.clear()
    st = SimpleNamespace(count=cat.POOL_MIN, picks=[], windows=[], marks=[], api_calls=0, stored=[],
                         unseen_available=True, repeat_available=True)

    async def count():
        return st.count

    async def pick(bot, guild_id=None, unseen_only=False, seen_days=None):
        st.picks.append((guild_id, unseen_only))
        st.windows.append(seen_days)
        if unseen_only and not st.unseen_available:
            return None
        if not unseen_only and not st.repeat_available:
            return None
        return cat_pool.PooledUrl("https://cdn.discordapp.com/attachments/1/2/pooled.jpg", 11)

    async def mark_seen(guild_id, pool_id):
        st.marks.append((guild_id, pool_id))

    async def store(bot, data, q, source, source_id):
        st.stored.append(source_id)
        return cat_pool.PooledUrl(f"https://cdn.discordapp.com/attachments/1/{len(st.stored)}/new.jpg", 55)

    async def known(source, ids):
        return set()

    async def cands(session):
        st.api_calls += 1
        return [{"url": "https://cdn2.thecatapi.com/images/a.jpg", "id": "a", "pixels": 800000}]

    async def dl(session, url):
        return sharp_bytes

    for name, fn in (("pool_count", count), ("pick", pick), ("mark_seen", mark_seen),
                     ("store", store), ("known_source_ids", known)):
        monkeypatch.setattr(cat_pool, name, fn)
    monkeypatch.setattr(cat, "_candidates_thecatapi", cands)
    monkeypatch.setattr(cat, "_download", dl)
    monkeypatch.setattr(cat.random, "randrange", lambda n: 1)              # no background top-up
    return st


def test_server_gets_an_unseen_pooled_picture_without_calling_the_api(env):
    url = run(cat.fetch_cat_url(FakeSession(), bot=MagicMock(), guild_id=5))
    assert "pooled.jpg" in url
    assert env.api_calls == 0 and env.picks == [(5, True)]


def test_server_that_has_seen_everything_triggers_one_api_call_and_the_new_cat_is_marked_seen(env):
    env.unseen_available = False
    url = run(cat.fetch_cat_url(FakeSession(), bot=MagicMock(), guild_id=5))
    assert url.endswith("new.jpg") and env.api_calls == 1
    assert env.marks == [(5, 55)]


def test_api_down_and_everything_seen_falls_back_to_a_repeat_not_no_cat(env, monkeypatch):
    env.unseen_available = False

    async def boom(session):
        raise RuntimeError("down")
    monkeypatch.setattr(cat, "_candidates_thecatapi", boom)
    url = run(cat.fetch_cat_url(FakeSession(), bot=MagicMock(), guild_id=5))
    assert "pooled.jpg" in url
    assert env.picks == [(5, True), (5, False)]                            # unseen first, then allow a repeat


def test_api_with_nothing_new_also_falls_back_to_a_repeat(env, monkeypatch):
    env.unseen_available = False

    async def empty(session):
        env.api_calls += 1
        return []
    monkeypatch.setattr(cat, "_candidates_thecatapi", empty)
    assert "pooled.jpg" in run(cat.fetch_cat_url(FakeSession(), bot=MagicMock(), guild_id=5))
    assert env.api_calls == 1


def test_no_pictures_anywhere_and_api_down_means_no_cat(env, monkeypatch):
    env.unseen_available = env.repeat_available = False

    async def boom(session):
        raise RuntimeError("down")
    monkeypatch.setattr(cat, "_candidates_thecatapi", boom)
    monkeypatch.setattr(cat, "_from_thecatapi", boom)
    assert run(cat.fetch_cat_url(FakeSession(), bot=MagicMock(), guild_id=5)) is None


def test_small_pool_still_calls_the_api_first(env):
    env.count = 3
    url = run(cat.fetch_cat_url(FakeSession(), bot=MagicMock(), guild_id=5))
    assert url.endswith("new.jpg") and env.api_calls == 1 and env.picks == []
    assert env.marks == [(5, 55)]


def test_without_a_server_the_original_pick_is_used(env, monkeypatch):
    seen = []

    async def old_pick(bot):                                               # the original one-argument signature
        seen.append(1)
        return "https://cdn.discordapp.com/attachments/1/2/pooled.jpg"
    monkeypatch.setattr(cat_pool, "pick", old_pick)
    assert "pooled.jpg" in run(cat.fetch_cat_url(FakeSession(), bot=MagicMock()))
    assert seen == [1] and env.api_calls == 0 and env.marks == []


def test_posting_and_the_cat_button_pass_the_server_id(monkeypatch):
    got = []

    async def fake(session=None, bot=None, guild_id=None):
        got.append(guild_id)
        return None
    monkeypatch.setattr(cat, "fetch_cat_url", fake)

    import discord
    ch = MagicMock(spec=discord.TextChannel)
    ch.permissions_for.return_value = SimpleNamespace(view_channel=True, send_messages=True, embed_links=True)
    guild = MagicMock(); guild.id = 77; guild.get_channel.return_value = ch
    run(cat.post_cat(MagicMock(), guild, 1))

    sent = []

    class I:
        user = SimpleNamespace(id=900001); guild_id = 88; client = MagicMock()
        response = SimpleNamespace()
    i = I()

    async def defer(**k): pass

    async def send(*a, **k): sent.append(1)
    i.response.defer = defer
    i.followup = SimpleNamespace(send=send)
    cat._last_use.clear() if hasattr(cat, "_last_use") else None
    run(cat.send_cat(i))
    assert got == [77, 88]


def test_migration_and_schema_wiring():
    sql = (ROOT / "database/migrations/034_cat_seen.sql").read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS cat_pool_seen" in sql and "PRIMARY KEY (guild_id, pool_id)" in sql
    db = (ROOT / "database.py").read_text(encoding="utf-8")
    import re
    # registered, and the schema number is at least the one that introduced it (later migrations bump it further)
    assert "034_cat_seen.sql" in db and int(re.search(r'^SCHEMA_VERSION = "(\d+)"', db, re.M).group(1)) >= 79


# ── real SQL (skipped unless a Postgres is provided) ──────────────────────

PG = os.getenv("CAT_TEST_PG_DSN")


@pytest.mark.skipif(not PG, reason="set CAT_TEST_PG_DSN to run the real-Postgres pick test")
def test_real_sql_unseen_first_per_server_then_oldest_seen(monkeypatch):
    import asyncpg

    async def scenario():
        pool = await asyncpg.create_pool(PG, min_size=1, max_size=3)
        async with pool.acquire() as c:
            await c.execute("DROP TABLE IF EXISTS cat_pool_seen, cat_adoptions, cat_spawns, cat_image_pool CASCADE")
            for f in ("032_cat_setup.sql", "033_cat_adopt.sql", "034_cat_seen.sql"):
                await c.execute((ROOT / "database/migrations" / f).read_text(encoding="utf-8"))
            for n in range(3):
                await c.execute(
                    "INSERT INTO cat_image_pool (sha256, source, width, height, sharpness, bytes, channel_id, message_id)"
                    " VALUES ($1,'t',800,800,200,1000,1,$2)", f"sha{n}", 100 + n)

        async def _pool():
            return pool

        class Msg:
            attachments = [SimpleNamespace(url="https://cdn.discordapp.com/attachments/1/2/x.jpg")]

        class Ch:
            async def fetch_message(self, mid):
                return Msg()

        async def _channel(bot, cid):
            return Ch()
        monkeypatch.setattr(cat_pool, "_pool", _pool)
        monkeypatch.setattr(cat_pool, "_channel", _channel)
        cat_pool._url_cache.clear()
        bot = MagicMock()

        first = [await cat_pool.pick(bot, guild_id=1, unseen_only=True) for _ in range(3)]
        ids = [u.pool_id for u in first]
        assert len(set(ids)) == 3                                        # three different pictures for server 1
        assert await cat_pool.pick(bot, guild_id=1, unseen_only=True) is None   # seen them all
        other = await cat_pool.pick(bot, guild_id=2, unseen_only=True)           # server 2 is independent
        assert other is not None
        repeat = await cat_pool.pick(bot, guild_id=1)                    # repeat allowed: the one seen longest ago
        assert repeat.pool_id == ids[0]
        async with pool.acquire() as c:
            assert await c.fetchval("SELECT count(*) FROM cat_pool_seen WHERE guild_id = 1") == 3
            await c.execute("UPDATE cat_pool_seen SET seen_at = NOW() - interval '30 days' WHERE guild_id = 1")
        assert await cat_pool.pick(bot, guild_id=1, unseen_only=True) is not None   # the window expired
        async with pool.acquire() as c:                                  # the window is adjustable per call
            await c.execute("INSERT INTO cat_pool_seen (guild_id, pool_id, seen_at) "
                            "SELECT 7, id, NOW() - interval '2 days' FROM cat_image_pool")
        assert await cat_pool.pick(bot, guild_id=7, unseen_only=True, seen_days=1) is not None   # 2 days ago > 1 day
        async with pool.acquire() as c:
            await c.execute("UPDATE cat_pool_seen SET seen_at = NOW() - interval '2 days' WHERE guild_id = 7")
        assert await cat_pool.pick(bot, guild_id=7, unseen_only=True, seen_days=5) is None       # still inside 5 days
        await cat_pool._forget(ids[1])
        async with pool.acquire() as c:
            assert await c.fetchval("SELECT count(*) FROM cat_pool_seen WHERE pool_id = $1", ids[1]) == 0
            await c.execute("DROP TABLE cat_pool_seen")                  # migration not applied yet
        assert await cat_pool.pick(bot, guild_id=1, unseen_only=True) is None
        assert await cat_pool.pick(bot, guild_id=1) is not None          # still gets a cat, old behaviour
        await pool.close()

    run(scenario())


# ── adaptive window, pool size, caps ──────────────────────────────────────

def test_seen_window_fits_the_pool_so_a_busy_server_does_not_exhaust_it():
    busiest = cat.cat_setup.PREMIUM.daily_max
    assert cat.seen_window(0) == 1 and cat.seen_window(400) == 1             # tiny pool: shortest window
    assert cat.seen_window(cat.POOL_MAX) == cat.POOL_MAX // busiest          # full pool: several days
    assert cat.seen_window(10 ** 6) == cat_pool.SEEN_DAYS                    # never longer than the configured max
    # the window never holds more pictures than the pool has, for the busiest server
    for n in (400, 900, cat.POOL_MAX):
        assert cat.seen_window(n) * busiest <= max(n, busiest)


def test_pool_is_big_enough_for_several_days_of_the_busiest_server():
    assert cat.POOL_MAX // cat.cat_setup.PREMIUM.daily_max >= 5


def test_the_window_is_passed_to_the_pick(env):
    env.count = 900
    run(cat.fetch_cat_url(FakeSession(), bot=MagicMock(), guild_id=5))
    assert env.windows == [cat.seen_window(900)]


def test_caps_and_every_text_that_quotes_them_agree():
    cs = cat.cat_setup
    assert cs.PREMIUM.daily_min > cs.FREE.daily_max                          # Premium stays clearly above free
    texts = [
        (ROOT / "modules/ai_features.py").read_text(encoding="utf-8"),
        (ROOT / "discord_bot/cogs/_views_cat_setup.py").read_text(encoding="utf-8"),
        (ROOT / "modules/cat_setup.py").read_text(encoding="utf-8"),
    ]
    free, prem = f"{cs.FREE.daily_min} to {cs.FREE.daily_max}", f"{cs.PREMIUM.daily_min} to {cs.PREMIUM.daily_max}"
    for t in texts:
        flat = " ".join(t.replace('"\n    "', "").split())
        assert "80 to 90" not in flat and "150 to 200" not in flat           # the old numbers are gone
    assert free in texts[1] and prem in texts[1]
