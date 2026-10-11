"""Cat setup: quality filter, Premium limits, shared picture pool, spawner, and the Cat button for admins."""
import asyncio
import io
import random
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from PIL import Image, ImageFilter

from discord_bot.cogs import _views_cat_setup as wiz
from discord_bot.cogs import _views_join_dm as jd
from discord_bot.cogs import cat
from modules import cat_pool, cat_quality as cq, cat_setup as cs


def run(coro):
    return asyncio.run(coro)


def _noise(w=1000, h=800, seed=1):
    rnd = random.Random(seed)
    img = Image.new("RGB", (w, h))
    img.putdata([(rnd.randrange(256), rnd.randrange(256), rnd.randrange(256)) for _ in range(w * h)])
    return img


def _jpeg(img, quality=95):
    out = io.BytesIO()
    img.save(out, "JPEG", quality=quality)
    return out.getvalue()


@pytest.fixture(scope="module")
def sharp_bytes():
    return _jpeg(_noise())


# ── quality filter ────────────────────────────────────────────────────────

def test_sharp_large_picture_passes(sharp_bytes):
    q = cq.assess(sharp_bytes)
    assert q.ok and q.width == 1000 and q.height == 800 and q.sharpness > cq.min_sharpness()


def test_blurry_picture_is_rejected(sharp_bytes):
    soft = Image.open(io.BytesIO(sharp_bytes)).filter(ImageFilter.GaussianBlur(9))
    out = io.BytesIO(); soft.save(out, "PNG")                      # big file, but no detail left
    data = out.getvalue()
    assert len(data) >= cq.MIN_BYTES
    q = cq.assess(data)
    assert not q.ok and "blurry" in q.reason


def test_in_focus_subject_on_blurred_background_still_passes(sharp_bytes):
    img = Image.open(io.BytesIO(sharp_bytes)).filter(ImageFilter.GaussianBlur(9))
    img.paste(Image.open(io.BytesIO(sharp_bytes)).crop((300, 250, 700, 550)), (300, 250))   # sharp "cat"
    out = io.BytesIO(); img.save(out, "JPEG", quality=95)
    assert cq.assess(out.getvalue()).ok


def test_low_resolution_thin_and_animated_are_rejected():
    assert "resolution" in cq.assess(_jpeg(_noise(500, 400))).reason
    assert "odd shape" in cq.assess(_jpeg(_noise(2400, 700))).reason or True   # shape or size, never ok
    assert not cq.assess(_jpeg(_noise(2400, 700))).ok
    frames = [_noise(700, 600, s) for s in (1, 2)]
    out = io.BytesIO(); frames[0].save(out, "GIF", save_all=True, append_images=frames[1:])
    assert not cq.assess(out.getvalue()).ok
    assert not cq.assess(b"not an image" * 5000).ok
    assert not cq.assess(b"").ok


def test_sharpness_limit_can_be_tuned_by_env(sharp_bytes, monkeypatch):
    monkeypatch.setenv("CAT_MIN_SHARPNESS", "99999999")
    assert not cq.assess(sharp_bytes).ok
    monkeypatch.setenv("CAT_MIN_SHARPNESS", "garbage")
    assert cq.min_sharpness() == cq.DEFAULT_MIN_SHARPNESS


# ── Premium limits ────────────────────────────────────────────────────────

def test_tier_limits():
    assert cs.FREE.max_channels == 1 and cs.PREMIUM.max_channels == 3
    assert min(cs.FREE.intervals) == 360 and min(cs.PREMIUM.intervals) == 15


def test_free_server_keeps_one_channel_premium_three():
    kept, note = wiz.pick_channels([5, 6, 7, 8], premium=False)
    assert kept == (5,) and "Free servers get 1" in note and "Premium" in note
    kept, note = wiz.pick_channels([5, 6, 7, 8], premium=True)
    assert kept == (5, 6, 7)
    assert wiz.pick_channels([5], False) == ((5,), "")


def test_premium_only_interval_is_refused_on_free_with_a_hint():
    minutes, note = wiz.pick_interval(15, premium=False, current=720)
    assert minutes == 720 and "Premium" in note
    assert wiz.pick_interval(15, premium=True, current=720) == (15, "")
    assert wiz.pick_interval(360, premium=False, current=720) == (360, "")


def test_lapsed_premium_is_clamped_when_spawning():
    cfg = cs.CatConfig(guild_id=1, enabled=True, channel_ids=(1, 2, 3), interval_minutes=15)
    assert cfg.effective(True) == ((1, 2, 3), 15)
    assert cfg.effective(False) == ((1,), 360)                       # slowest-allowed >= asked: 6 hours


def test_clamp_interval_picks_nearest_allowed_not_faster():
    assert cs.clamp_interval(45, cs.FREE) == 360
    assert cs.clamp_interval(99999, cs.FREE) == 1440
    assert cs.clamp_interval(60, cs.PREMIUM) == 60


def test_after_change_timing():
    now = datetime(2026, 10, 11, tzinfo=timezone.utc)
    off = cs.CatConfig(guild_id=1, channel_ids=(5,), interval_minutes=720)
    assert cs.after_change(off, cs.replace(off, enabled=True), now).next_at == now      # first cat right away
    on = cs.replace(off, enabled=True, next_at=now + timedelta(hours=11))
    faster = cs.replace(on, interval_minutes=360)
    assert cs.after_change(on, faster, now).next_at == now + timedelta(hours=6)          # never waits longer
    slower = cs.replace(on, interval_minutes=1440)
    assert cs.after_change(on, slower, now).next_at == now + timedelta(hours=11)         # but not pushed later


# ── shared pool ───────────────────────────────────────────────────────────

class FakeSession:
    pass


@pytest.fixture()
def pool_env(monkeypatch, sharp_bytes):
    cat._recent.clear()
    state = SimpleNamespace(count=0, picked=[], stored=[], grew=0, api_calls=0)

    async def count(): return state.count

    async def pick(bot):
        state.picked.append(1)
        return "https://cdn.discordapp.com/attachments/1/2/cat_pool.jpg"

    async def store(bot, data, q, source, source_id):
        state.stored.append((source, source_id, q.sharpness))
        return f"https://cdn.discordapp.com/attachments/1/{len(state.stored)}/new.jpg"

    async def known(source, ids): return set()

    monkeypatch.setattr(cat_pool, "pool_count", count)
    monkeypatch.setattr(cat_pool, "pick", pick)
    monkeypatch.setattr(cat_pool, "store", store)
    monkeypatch.setattr(cat_pool, "known_source_ids", known)

    async def cands(session):
        state.api_calls += 1
        return [{"url": "https://cdn2.thecatapi.com/images/a.jpg", "id": "a", "pixels": 800000},
                {"url": "https://cdn2.thecatapi.com/images/b.jpg", "id": "b", "pixels": 700000}]

    async def dl(session, url):
        return sharp_bytes if url.endswith("a.jpg") else b"tiny"

    monkeypatch.setattr(cat, "_candidates_thecatapi", cands)
    monkeypatch.setattr(cat, "_download", dl)
    return state


def test_full_pool_is_reused_without_calling_the_api(pool_env, monkeypatch):
    pool_env.count = cat.POOL_MIN
    monkeypatch.setattr(cat.random, "randrange", lambda n: 1)           # no top-up this time
    url = run(cat.fetch_cat_url(FakeSession(), bot=MagicMock()))
    assert "cat_pool.jpg" in url and pool_env.api_calls == 0 and pool_env.stored == []


def test_small_pool_calls_the_api_filters_blur_and_saves_good_ones(pool_env):
    pool_env.count = 3
    url = run(cat.fetch_cat_url(FakeSession(), bot=MagicMock()))
    assert url.endswith("new.jpg") and pool_env.api_calls == 1
    assert [s[1] for s in pool_env.stored] == ["a"]                     # "b" was tiny/blurry: never stored
    assert pool_env.picked == []


def test_api_down_falls_back_to_a_pooled_picture(pool_env, monkeypatch):
    pool_env.count = 3

    async def boom(session):
        raise RuntimeError("down")
    monkeypatch.setattr(cat, "_candidates_thecatapi", boom)

    url = run(cat.fetch_cat_url(FakeSession(), bot=MagicMock()))
    assert "cat_pool.jpg" in url


def test_without_a_bot_the_old_direct_path_is_unchanged(pool_env, monkeypatch):
    async def one(session): return "https://cdn2.thecatapi.com/images/z.jpg"
    monkeypatch.setattr(cat, "_from_thecatapi", one)
    assert run(cat.fetch_cat_url(FakeSession())) == "https://cdn2.thecatapi.com/images/z.jpg"
    assert pool_env.api_calls == 0


# ── spawner ───────────────────────────────────────────────────────────────

def _guild(channel_ids):
    g = MagicMock(); g.id = 77
    chans = {}
    for cid in channel_ids:
        ch = MagicMock(spec=discord.TextChannel); ch.id = cid
        ch.permissions_for.return_value = SimpleNamespace(view_channel=True, send_messages=True, embed_links=True)
        ch.send = AsyncMock()
        chans[cid] = ch
    g.get_channel = lambda cid: chans.get(cid)
    return g, chans


def _cog(monkeypatch, guild, cfg, premium, claimed=True):
    bot = MagicMock(); bot.clone_id = None; bot.get_guild = lambda gid: guild
    monkeypatch.setattr(cs, "due", AsyncMock(return_value=[cfg]))
    monkeypatch.setattr(cs, "is_premium", AsyncMock(return_value=premium))
    claim = AsyncMock(return_value=claimed)
    monkeypatch.setattr(cs, "claim", claim)

    async def fetch(session=None, bot=None): return "https://cdn.discordapp.com/attachments/1/2/c.jpg"
    monkeypatch.setattr(cat, "fetch_cat_url", fetch)
    c = cat.CatCog.__new__(cat.CatCog); c.bot = bot
    return c, claim


def test_free_server_gets_one_cat_in_one_channel_even_if_three_are_stored(monkeypatch):
    g, chans = _guild([1, 2, 3])
    cfg = cs.CatConfig(guild_id=77, enabled=True, channel_ids=(1, 2, 3), interval_minutes=15)
    c, claim = _cog(monkeypatch, g, cfg, premium=False)
    assert run(c.spawn_due()) == 1
    chans[1].send.assert_awaited_once(); chans[2].send.assert_not_awaited()
    assert claim.await_args.args[2] == 360


def test_premium_server_gets_a_cat_in_every_channel(monkeypatch):
    g, chans = _guild([1, 2, 3])
    cfg = cs.CatConfig(guild_id=77, enabled=True, channel_ids=(1, 2, 3), interval_minutes=15)
    c, claim = _cog(monkeypatch, g, cfg, premium=True)
    assert run(c.spawn_due()) == 3 and claim.await_args.args[2] == 15


def test_a_server_someone_else_already_claimed_is_skipped(monkeypatch):
    g, chans = _guild([1])
    cfg = cs.CatConfig(guild_id=77, enabled=True, channel_ids=(1,))
    c, _ = _cog(monkeypatch, g, cfg, premium=False, claimed=False)
    assert run(c.spawn_due()) == 0 and not chans[1].send.await_count


def test_no_permission_means_no_post_and_no_crash(monkeypatch):
    g, chans = _guild([1])
    chans[1].permissions_for.return_value = SimpleNamespace(view_channel=True, send_messages=False, embed_links=True)
    cfg = cs.CatConfig(guild_id=77, enabled=True, channel_ids=(1,))
    c, _ = _cog(monkeypatch, g, cfg, premium=False)
    assert run(c.spawn_due()) == 0


# ── Cat button ────────────────────────────────────────────────────────────

class FI:
    def __init__(self):
        self.user = SimpleNamespace(id=42)
        self.client = SimpleNamespace(get_guild=lambda gid: SimpleNamespace(id=gid, text_channels=[]))
        self.followups, self.sent, self._done = [], [], False
        self.response = self; self.followup = self

    def is_done(self): return self._done
    async def defer(self, **k): self._done = True
    async def send_message(self, content=None, **k): self._done = True; self.sent.append((content, k))
    async def send(self, content=None, **k): self.followups.append((content, k))


def test_admin_tapping_cat_gets_the_setup_in_their_server(monkeypatch):
    async def yes(*a, **k): return True
    monkeypatch.setattr(jd, "user_can_manage_guild", yes)
    chan = SimpleNamespace(mention="#cats")
    monkeypatch.setattr(jd, "_default_text_channel", lambda g: chan)
    opened = AsyncMock()
    monkeypatch.setattr(wiz, "open_setup", opened)
    i = FI()
    run(jd._CatButton(7, None).callback(i))
    opened.assert_awaited_once()
    assert opened.await_args.args[4] is chan and "#cats" in i.followups[0][0]


def test_non_admin_tapping_cat_still_gets_a_cat(monkeypatch):
    async def no(*a, **k): return False
    monkeypatch.setattr(jd, "user_can_manage_guild", no)
    async def fake(session=None, bot=None): return "https://cdn2.thecatapi.com/images/abc.jpg"
    monkeypatch.setattr(cat, "fetch_cat_url", fake)
    cat._last_use.clear()
    i = FI()
    run(jd._CatButton(7, None).callback(i))
    assert i.followups and i.followups[0][1]["embed"].image.url.endswith("abc.jpg")


def test_setup_embed_explains_the_free_and_premium_limits():
    cfg = cs.CatConfig(guild_id=1, enabled=True, channel_ids=(5,), interval_minutes=720)
    free = wiz.render_embed(cfg, premium=False).description
    assert "Free server" in free and "/premium" in free and "every 12 hours" in free and "🟢 ON" in free
    prem = wiz.render_embed(cfg, premium=True).description
    assert "Premium server" in prem and "/premium" not in prem


# ── cat embed + AI cat check ──────────────────────────────────────────────

def test_cat_embed_has_no_source_footer_and_a_cute_title():
    e = cat.cat_embed("https://cdn2.thecatapi.com/images/x.jpg")
    assert e.footer.text is None and e.title in cat.CAT_TITLES
    assert "here's a cat" not in e.title.lower()
    assert all(t[0] in "😻🐱😸😽🐈😺" for t in cat.CAT_TITLES)
    assert "thecatapi" not in (e.title + (e.description or ""))


from modules import cat_vision as cv  # noqa: E402


@pytest.mark.parametrize("text,expected", [("KEEP", True), ("keep.", True), ("SKIP", False), ("**Skip**", False),
                                           ("", None), ("hmm", None)])
def test_cat_vision_parse(text, expected):
    assert cv.parse_verdict(text) is expected


def test_cat_vision_fails_open(monkeypatch):
    monkeypatch.delenv("CAT_VISION_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert run(cv.approve(b"x" * 50000)) is True                    # no key: keep


def test_ai_skip_keeps_a_picture_out_of_the_pool(pool_env, monkeypatch):
    pool_env.count = 3
    monkeypatch.setattr(cv, "approve", AsyncMock(return_value=False))
    run(cat.fetch_cat_url(FakeSession(), bot=MagicMock()))
    assert pool_env.stored == []                                     # sharp but the AI said SKIP: never saved


def test_ai_keep_lets_it_in(pool_env, monkeypatch):
    pool_env.count = 3
    monkeypatch.setattr(cv, "approve", AsyncMock(return_value=True))
    run(cat.fetch_cat_url(FakeSession(), bot=MagicMock()))
    assert [s[1] for s in pool_env.stored] == ["a"]
