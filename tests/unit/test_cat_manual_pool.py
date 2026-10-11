"""/cat and the Cat button serve a stored picture and never call the cat API (when the pool has enough), and they
don't use up the pictures the automatic spawns still need."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from discord_bot.cogs import cat
from modules import cat_pool
from tests.unit.test_cat_pool_seen import FakeSession, env, run  # noqa: F401  (shared fixtures)
from tests.unit.test_cat_setup import sharp_bytes  # noqa: F401


def _fetch(manual: bool):
    token = cat_pool.manual_request.set(manual)
    try:
        return run(cat.fetch_cat_url(FakeSession(), bot=MagicMock(), guild_id=5))
    finally:
        cat_pool.manual_request.reset(token)


def test_manual_cat_never_calls_the_api_even_when_the_server_has_seen_everything(env):
    env.unseen_available = False                       # this server has seen every pooled picture
    url = _fetch(manual=True)
    assert "pooled.jpg" in url
    assert env.api_calls == 0 and env.stored == []


def test_manual_cat_does_not_mark_pictures_seen_or_use_the_server_history(env):
    env.unseen_available = False
    _fetch(manual=True)
    assert env.picks == [(None, False)]                # picked WITHOUT a server: no seen-tracking at all
    assert env.marks == []


def test_automatic_spawn_still_uses_the_api_when_everything_was_seen(env):
    """The old behaviour for the spawner is unchanged (that is what keeps the pool growing)."""
    env.unseen_available = False
    env.repeat_available = True
    url = _fetch(manual=False)
    assert env.api_calls == 1 and "new.jpg" in url


def test_manual_cat_with_a_tiny_pool_still_bootstraps_from_the_api(env):
    env.count = cat.POOL_MIN - 1
    env.unseen_available = False
    url = _fetch(manual=True)
    assert env.api_calls == 1 and "new.jpg" in url


def test_manual_cat_falls_back_to_the_old_path_if_the_pool_gives_nothing(env):
    env.repeat_available = False                       # e.g. the hosting channel can't be read
    env.unseen_available = False
    url = _fetch(manual=True)
    assert env.api_calls == 1 and url                  # a cat still appears


def test_manual_cat_never_starts_a_background_top_up(env, monkeypatch):
    monkeypatch.setattr(cat.random, "randrange", lambda n: 0)        # would trigger a top-up on the normal path
    started = []
    monkeypatch.setattr(cat, "_grow_in_background", lambda bot: started.append(1) or asyncio.sleep(0))
    _fetch(manual=True)
    assert started == [] and env.api_calls == 0


def test_flag_is_off_by_default_and_reset_after_send_cat(env, monkeypatch):
    assert cat_pool.manual_request.get() is False
    seen = []

    async def fake_fetch(session=None, bot=None, guild_id=None):
        seen.append(cat_pool.manual_request.get())
        return cat_pool.PooledUrl("https://cdn.discordapp.com/attachments/1/2/c.jpg", 3)
    monkeypatch.setattr(cat, "fetch_cat_url", fake_fetch)
    cat._last_use.clear()
    i = MagicMock()
    i.user = SimpleNamespace(id=424242)
    i.guild_id, i.guild, i.client = 5, SimpleNamespace(name="G"), MagicMock()
    i.response.send_message = AsyncMock(); i.response.defer = AsyncMock(); i.followup.send = AsyncMock()
    run(cat.send_cat(i))
    assert seen == [True]                              # during the fetch
    assert cat_pool.manual_request.get() is False      # and cleaned up afterwards
    i.followup.send.assert_awaited()


def test_flag_is_reset_even_if_the_fetch_raises(env, monkeypatch):
    async def boom(session=None, bot=None, guild_id=None): raise RuntimeError("x")
    monkeypatch.setattr(cat, "fetch_cat_url", boom)
    cat._last_use.clear()
    i = MagicMock()
    i.user = SimpleNamespace(id=424243)
    i.guild_id, i.guild, i.client = 5, SimpleNamespace(name="G"), MagicMock()
    i.response.defer = AsyncMock(); i.response.send_message = AsyncMock(); i.followup.send = AsyncMock()
    try:
        run(cat.send_cat(i))
    except RuntimeError:
        pass
    assert cat_pool.manual_request.get() is False
