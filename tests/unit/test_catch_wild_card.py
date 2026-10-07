"""Wild zone banner: rendering, the optional-card rules, and how open_wild_zone uses it."""

import asyncio
import io
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import discord
import pytest
from PIL import Image

import discord_bot.cogs._views_catch_wild as views
from modules import catch_wild_card as wc
from modules.catch_theme import rarity_color
from modules.catch_wild import ActiveSpawn
from tests.unit.test_catch_interaction_timing import Recorder, assert_response_first, make_interaction, slow

REAL_CACHED = wc._cached_wild  # the failure tests replace the module attribute
PNG = b"\x89PNG\r\n\x1a\n"
TITLE = (20, 20, 300, 62)
COUNT = (480, 28, 700, 54)
MORE = (590, 162, 700, 184)
NOW = datetime(2026, 10, 7, 12, tzinfo=timezone.utc)

FULL = tuple((r, False) for r in ("common", "uncommon", "rare", "epic", "mythic", "common", "rare", "common", "uncommon", "epic"))


@pytest.fixture(autouse=True)
def _fresh_cache():
    clear = getattr(REAL_CACHED, "cache_clear", lambda: None)  # not the patched attribute
    clear()
    yield
    clear()


def img(data):
    return Image.open(io.BytesIO(data)).convert("RGB")


def orb_box(i, pad=4):
    cx = wc._orb_x(i)
    return (int(cx - wc.ORB_R + pad), wc.ORB_Y - wc.ORB_R + pad, int(cx + wc.ORB_R - pad), wc.ORB_Y + wc.ORB_R - pad)


def centre(image, i):
    return image.getpixel((int(wc._orb_x(i)), wc.ORB_Y + 8))


def run(coro):
    return asyncio.run(coro)


def test_card_is_a_720_wide_png():
    data = REAL_CACHED(FULL, 10)
    assert data.startswith(PNG) and img(data).size == (720, wc.WILD_H)


def test_render_is_cached_and_deterministic():
    assert REAL_CACHED(FULL, 10) is REAL_CACHED(FULL, 10)
    assert wc._draw_wild(FULL, 10) == wc._draw_wild(FULL, 10)
    assert REAL_CACHED(FULL, 10) is not REAL_CACHED(FULL[:3], 3)


def test_title_and_count_are_drawn():
    image = img(wc._draw_wild(FULL, 10))
    for box in (TITLE, COUNT):
        assert image.crop(box).convert("L").getextrema()[1] > 150


def test_count_text_follows_the_real_total():
    few = img(wc._draw_wild(FULL[:3], 3)).crop(COUNT).tobytes()
    many = img(wc._draw_wild(FULL[:3], 31)).crop(COUNT).tobytes()
    assert few != many


def test_quiet_zone_has_dim_slots_and_a_muted_label():
    image = img(wc._draw_wild((), 0))
    for i in range(wc.SLOTS):
        lo, hi = image.crop(orb_box(i)).convert("L").getextrema()
        assert hi < 60  # nothing lit
    quiet = image.crop(COUNT).convert("L").getextrema()[1]
    busy = img(wc._draw_wild(FULL, 10)).crop(COUNT).convert("L").getextrema()[1]
    assert 100 < quiet < busy


def test_empty_slots_are_drawn_as_outlined_circles():
    image = img(wc._draw_wild((), 0)).convert("L")
    for i in range(wc.SLOTS):
        cx = wc._orb_x(i)
        lo, hi = image.crop((int(cx - wc.ORB_R - 3), wc.ORB_Y - wc.ORB_R - 3, int(cx + wc.ORB_R + 3), wc.ORB_Y + wc.ORB_R + 3)).getextrema()
        assert hi - lo > 25  # ring against a darker fill; bare background varies by under 10


def test_each_live_spawn_lights_its_own_orb_in_its_rarity_colour():
    image = img(wc._draw_wild(FULL, 10))
    seen = set()
    for i, (rarity, _) in enumerate(FULL):
        want = rarity_color(rarity)
        got = centre(image, i)
        assert max(abs(got[0] - want.r), abs(got[1] - want.g), abs(got[2] - want.b)) < 110
        seen.add(got)
    assert len(seen) >= 5  # five rarities, five different colours


def test_only_as_many_orbs_as_spawns_are_lit():
    image = img(wc._draw_wild(FULL[:4], 4))
    lit = [image.crop(orb_box(i)).convert("L").getextrema()[1] > 100 for i in range(wc.SLOTS)]
    assert lit == [True] * 4 + [False] * 6


def test_shiny_spawn_gets_a_sparkle_and_the_shiny_colour():
    plain = img(wc._draw_wild((("common", False),), 1))
    shiny = img(wc._draw_wild((("common", True),), 1))
    cx = wc._orb_x(0)
    spark = (int(cx + wc.ORB_R * 0.8 - 10), int(wc.ORB_Y - wc.ORB_R * 0.8 - 10), int(cx + wc.ORB_R * 0.8 + 10), int(wc.ORB_Y - wc.ORB_R * 0.8 + 10))
    assert plain.crop(spark).tobytes() != shiny.crop(spark).tobytes()
    assert centre(plain, 0) != centre(shiny, 0)
    tip = (int(cx + wc.ORB_R * 0.8 - 2), int(wc.ORB_Y - wc.ORB_R * 0.8 - 8), int(cx + wc.ORB_R * 0.8 + 2), int(wc.ORB_Y - wc.ORB_R * 0.8 - 4))
    assert shiny.crop(tip).convert("L").getextrema()[1] > 200 > plain.crop(tip).convert("L").getextrema()[1] + 100


def test_more_label_only_when_every_slot_is_full_and_there_are_extra():
    def bright(slots, total):
        return img(wc._draw_wild(slots, total)).crop(MORE).convert("L").getextrema()[1]

    assert bright(FULL, 10) < 60       # exactly full: nothing to add
    assert bright(FULL[:7], 14) < 60   # inconsistent input never claims hidden creatures
    assert bright(FULL, 23) > 100      # 13 more than the slots show
    assert img(wc._draw_wild(FULL, 23)).crop(MORE).tobytes() != img(wc._draw_wild(FULL, 31)).crop(MORE).tobytes()


def drawn_texts(monkeypatch, slots, total):
    real, texts = wc.ImageDraw.Draw, []

    class Spy:
        def __init__(self, image):
            self._d = real(image)

        def text(self, xy, string, **kw):
            texts.append(string)
            return self._d.text(xy, string, **kw)

        def __getattr__(self, name):
            return getattr(self._d, name)

    monkeypatch.setattr(wc.ImageDraw, "Draw", Spy)
    wc._draw_wild(slots, total)
    return texts


def test_the_exact_words_drawn(monkeypatch):
    assert drawn_texts(monkeypatch, FULL, 23) == ["WILD ZONE", "23 IN THE WILD", "+13 MORE"]
    assert drawn_texts(monkeypatch, FULL[:3], 3) == ["WILD ZONE", "3 IN THE WILD"]
    assert drawn_texts(monkeypatch, (), 0) == ["WILD ZONE", "QUIET"]


def test_colours_follow_the_theme(monkeypatch):
    shots = []
    for value in (0xC03030, 0x3030C0):
        monkeypatch.setattr(wc.catch_theme, "rarity_color", lambda r, shiny=False, v=value: discord.Colour(v))
        shots.append(wc._draw_wild(FULL, 10))
    assert shots[0] != shots[1]
    base = []
    for value in (0xC03030, 0x3030C0):
        monkeypatch.setattr(wc.catch_theme, "element_color", lambda e, v=value: discord.Colour(v))
        base.append(wc._draw_wild((), 0))
    assert base[0] != base[1]


def spawns(n, **over):
    out = []
    for i in range(n):
        base = dict(id=i, channel_id=6, message_id=70 + i, name="Emberpup", rarity="rare", level=12, shiny=False,
                    personal=False, expires_at=NOW + timedelta(minutes=i + 1))
        base.update(over)
        out.append(ActiveSpawn(**base))
    return out


def test_art_sets_the_image_and_returns_a_fresh_file():
    e1, e2 = discord.Embed(title="t"), discord.Embed(title="t")
    f1, f2 = run(wc.wild_art(e1, spawns(2), 2)), run(wc.wild_art(e2, spawns(2), 2))
    assert f1 is not f2 and f1.filename == wc.WILD_FILE
    assert e1.image.url == f"attachment://{wc.WILD_FILE}"


def test_art_uses_the_spawns_in_order_and_caps_at_the_slots(monkeypatch):
    got = []

    def spy(slots, total):
        got.append((slots, total))
        return REAL_CACHED(slots, total)

    monkeypatch.setattr(wc, "_cached_wild", spy)
    items = spawns(12)
    items[1] = spawns(1, rarity="epic", shiny=True)[0]
    run(wc.wild_art(discord.Embed(), items, 15))
    slots, total = got[0]
    assert len(slots) == wc.SLOTS and total == 15
    assert slots[0] == ("rare", False) and slots[1] == ("epic", True)


def test_negative_total_is_clamped(monkeypatch):
    got = []
    monkeypatch.setattr(wc, "_cached_wild", lambda s, t: got.append(t) or REAL_CACHED(s, max(t, 0)))
    run(wc.wild_art(discord.Embed(), [], -3))
    assert got == [0]


def test_failed_render_leaves_the_embed_untouched(monkeypatch):
    def boom(*a):
        raise RuntimeError("no font")

    monkeypatch.setattr(wc, "_cached_wild", boom)
    embed = discord.Embed(title="t", description="d")
    assert run(wc.wild_art(embed, spawns(1), 1)) is None and embed.image.url is None


def open_wild(monkeypatch, found, total, rec=None):
    rec = rec or Recorder()
    monkeypatch.setattr(views, "check_player_allowed", slow(rec, "gate", SimpleNamespace(allowed=True, reason=None)))
    monkeypatch.setattr(views, "list_active_spawns", slow(rec, "load", (found, total)))
    inter = make_interaction(rec)
    inter.guild_id = 5
    sent = []

    async def followup_send(*args, **kwargs):
        sent.append((args, kwargs))

    inter.followup = SimpleNamespace(send=followup_send)
    run(views.open_wild_zone(inter))
    return sent, rec


def test_open_wild_zone_sends_the_card_and_keeps_all_the_text(monkeypatch):
    found = spawns(3)
    (args, kwargs), = open_wild(monkeypatch, found, 3)[0]
    embed = kwargs["embed"]
    assert kwargs["file"].filename == wc.WILD_FILE and embed.image.url == f"attachment://{wc.WILD_FILE}"
    assert embed.description == views.wild_embed(5, found, 3).description and kwargs["ephemeral"] is True


def test_open_wild_zone_card_also_shows_on_an_empty_zone(monkeypatch):
    (args, kwargs), = open_wild(monkeypatch, [], 0)[0]
    assert kwargs["file"].filename == wc.WILD_FILE and kwargs["embed"].description


def test_open_wild_zone_falls_back_to_the_plain_embed(monkeypatch):
    def boom(*a):
        raise RuntimeError("no font")

    monkeypatch.setattr(wc, "_cached_wild", boom)
    (args, kwargs), = open_wild(monkeypatch, spawns(2), 2)[0]
    assert "file" not in kwargs and kwargs["embed"].image.url is None and kwargs["embed"].description


def test_open_wild_zone_still_responds_first_and_gates_before_loading(monkeypatch):
    _, rec = open_wild(monkeypatch, spawns(1), 1)
    assert_response_first(rec)
    assert rec.calls.index("gate") < rec.calls.index("load")


def test_open_wild_zone_hands_the_card_the_listed_spawns_and_the_real_total(monkeypatch):
    got = []

    def spy(slots, total):
        got.append((slots, total))
        return REAL_CACHED(slots, total)

    monkeypatch.setattr(wc, "_cached_wild", spy)
    found = [spawns(1, rarity="epic", shiny=True)[0], spawns(1, rarity="common")[0]]
    open_wild(monkeypatch, found, 15)
    assert got == [((("epic", True), ("common", False)), 15)]
