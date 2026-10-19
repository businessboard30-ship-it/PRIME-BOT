import asyncio
import inspect
from types import SimpleNamespace

from discord_bot.cogs import cat
from modules import cat_adopt, cat_care, cat_pool


def test_take_runaway_rechecks_flags_in_the_outer_update():
    src = inspect.getsource(cat_care.take_runaway)
    assert "WHERE returned_at IS NULL AND claimed_at IS NULL AND pool_id" in src


def test_open_picture_is_not_posted_twice(monkeypatch):
    import discord
    sent = []

    class Ch(discord.TextChannel):
        def __init__(self): pass
        def permissions_for(self, m): return SimpleNamespace()
        async def send(self, **kw):
            sent.append(1)
            return SimpleNamespace(id=9)
    ch = Ch()
    guild = SimpleNamespace(id=1, name="g", me=object(), get_channel=lambda i: ch)
    monkeypatch.setattr(cat.cat_perms, "missing_names", lambda p: [])

    async def none(_): return None
    monkeypatch.setattr(cat.cat_care, "take_runaway", none)
    urls = iter([cat_pool.PooledUrl("https://x/1.jpg", 1), cat_pool.PooledUrl("https://x/2.jpg", 2)])

    async def fetch(**kw): return next(urls)
    monkeypatch.setattr(cat, "fetch_cat_url", fetch)

    async def open_(g, p): return p == 1          # picture 1 is already open here
    monkeypatch.setattr(cat_adopt, "picture_already_open", open_)
    made = []

    async def create(*a): made.append(a[3]); return 77
    monkeypatch.setattr(cat_adopt, "create_spawn", create)

    async def setmsg(*a): pass
    monkeypatch.setattr(cat_adopt, "set_spawn_message", setmsg)
    monkeypatch.setattr(cat, "spawn_post", lambda *a, **k: None, raising=False)
    import discord_bot.cogs._views_cat_adopt as va
    monkeypatch.setattr(va, "spawn_post", lambda *a, **k: object())
    ok = asyncio.run(cat.post_cat(SimpleNamespace(clone_id=None), guild, 3))
    assert ok and made == [2]                      # the duplicate (1) was skipped, picture 2 posted
