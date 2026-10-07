"""Guide banner: rendering, the optional-card rules, and how open_guide uses it."""

import asyncio
import io
from types import SimpleNamespace

import discord
import pytest
from PIL import Image

import discord_bot.cogs._views_catch_guide as views
from modules import catch_guide_card as gc
from tests.unit.test_catch_interaction_timing import Recorder, assert_response_first, make_interaction, slow

REAL_CACHED = gc._cached_guide  # the failure tests replace the module attribute
PNG = b"\x89PNG\r\n\x1a\n"
TITLE = (20, 20, 300, 62)
STEPS = (500, 28, 700, 54)
DOTS = (300, 190, 420, 206)  # the rarity dot row (inside tile 3)


@pytest.fixture(autouse=True)
def _fresh_cache():
    clear = getattr(REAL_CACHED, "cache_clear", lambda: None)  # not the patched attribute
    clear()
    yield
    clear()


def img(data=None):
    return Image.open(io.BytesIO(data or REAL_CACHED())).convert("RGB")


def tile_box(i):
    gap = 12
    tw = (720 - 48 - gap * 4) / 5
    x = 24 + i * (tw + gap)
    return (int(x), gc.TILE_Y[0], int(x + tw), gc.TILE_Y[1])


def run(coro):
    return asyncio.run(coro)


def test_card_is_a_720_wide_png():
    data = REAL_CACHED()
    assert data.startswith(PNG) and img(data).size == (720, gc.GUIDE_H)


def test_one_tile_per_guide_section_in_order():
    assert tuple(k for k, _, _ in gc.TILES) == views.GUIDE_SECTIONS


def test_render_is_cached_and_deterministic():
    assert REAL_CACHED() is REAL_CACHED()
    assert gc._draw_guide() == gc._draw_guide()


def test_every_tile_is_drawn_and_tiles_differ():
    crops = [img().crop(tile_box(i)).tobytes() for i in range(5)]
    assert len(set(crops)) == 5
    for i in range(5):
        lo, hi = img().crop(tile_box(i)).convert("L").getextrema()
        assert hi - lo > 80  # label and sigil are bright on a dark tile


def test_title_and_step_count_are_drawn():
    for box in (TITLE, STEPS):
        assert img().crop(box).convert("L").getextrema()[1] > 150


def test_rarity_dots_are_drawn(monkeypatch):
    with_dots = img().crop(DOTS).tobytes()
    monkeypatch.setattr(gc, "RARITY_ORDER", ())
    without = img(gc._draw_guide()).crop(DOTS).tobytes()
    assert with_dots != without


def test_colours_follow_the_theme(monkeypatch):
    shots = []
    for value in (0xC03030, 0x3030C0):
        monkeypatch.setattr(gc.catch_theme, "element_color", lambda e, v=value: discord.Colour(v))
        shots.append(gc._draw_guide())
    assert shots[0] != shots[1]


def test_sigils_are_drawn(monkeypatch):
    real = img(gc._draw_guide())
    monkeypatch.setattr(gc, "_sigil", lambda *a, **k: None)
    blank = img(gc._draw_guide())
    for i in range(5):
        x0, _, x1, _ = tile_box(i)
        box = (x0 + 20, gc.TILE_Y[0] + 36, x1 - 20, gc.TILE_Y[0] + 88)
        assert real.crop(box).tobytes() != blank.crop(box).tobytes()


def test_each_tile_label_is_drawn_from_its_own_text(monkeypatch):
    base = img(gc._draw_guide())
    monkeypatch.setattr(gc, "TILES", tuple((k, "ZZ", e) for k, _, e in gc.TILES))
    changed = img(gc._draw_guide())
    for i in range(5):
        x0, _, x1, _ = tile_box(i)
        box = (x0, gc.TILE_Y[1] - 32, x1, gc.TILE_Y[1] - 4)
        assert base.crop(box).tobytes() != changed.crop(box).tobytes()


def test_art_sets_the_image_and_returns_a_fresh_file():
    e1, e2 = discord.Embed(title="t"), discord.Embed(title="t")
    f1, f2 = run(gc.guide_art(e1)), run(gc.guide_art(e2))
    assert f1 is not f2 and f1.filename == gc.GUIDE_FILE
    assert e1.image.url == f"attachment://{gc.GUIDE_FILE}"


def test_failed_render_leaves_the_embed_untouched(monkeypatch):
    def boom():
        raise RuntimeError("no font")

    monkeypatch.setattr(gc, "_cached_guide", boom)
    embed = discord.Embed(title="t", description="d")
    assert run(gc.guide_art(embed)) is None and embed.image.url is None


def open_guide(monkeypatch, rec=None):
    rec = rec or Recorder()
    monkeypatch.setattr(views, "check_player_allowed", slow(rec, "gate", SimpleNamespace(allowed=True, reason=None)))
    inter = make_interaction(rec)
    sent = []

    async def followup_send(*args, **kwargs):
        sent.append((args, kwargs))

    inter.followup = SimpleNamespace(send=followup_send)
    run(views.open_guide(inter))
    return sent


def test_open_guide_sends_the_card_and_keeps_all_the_text(monkeypatch):
    (args, kwargs), = open_guide(monkeypatch)
    plain = views.guide_embed()
    assert kwargs["file"].filename == gc.GUIDE_FILE and kwargs["ephemeral"] is True
    embed = kwargs["embed"]
    assert embed.image.url == f"attachment://{gc.GUIDE_FILE}"
    assert [(f.name, f.value) for f in embed.fields] == [(f.name, f.value) for f in plain.fields]
    assert embed.title == plain.title and embed.description == plain.description


def test_open_guide_falls_back_to_the_plain_embed(monkeypatch):
    async def none(embed):
        return None

    monkeypatch.setattr(views, "guide_art", none)
    (args, kwargs), = open_guide(monkeypatch)
    assert "file" not in kwargs and kwargs["embed"].image.url is None
    assert len(kwargs["embed"].fields) == len(views.GUIDE_SECTIONS)


def test_open_guide_still_responds_first_and_gates(monkeypatch):
    rec = Recorder()
    open_guide(monkeypatch, rec)
    assert_response_first(rec)


def test_refusal_stays_plain_text(monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(views, "check_player_allowed", slow(rec, "gate", SimpleNamespace(allowed=False, reason="game_disabled")))
    inter = make_interaction(rec)
    sent = []

    async def followup_send(*a, **k):
        sent.append((a, k))

    inter.followup = SimpleNamespace(send=followup_send)
    run(views.open_guide(inter))
    assert "embed" not in sent[0][1] and "file" not in sent[0][1]
