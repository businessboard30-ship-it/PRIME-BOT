"""Status card: rendering, per-tile isolation, fallback, and how open_status sends it."""

import asyncio
import io
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from PIL import Image

import discord_bot.cogs._views_catch_status as views
from modules import catch_status_card as sc
from modules.catch_card import W
from modules.catch_i18n import text
from modules.catch_status import PlayerStatus as P
from tests.unit.test_catch_interaction_timing import Recorder, make_interaction, slow

PNG = b"\x89PNG\r\n\x1a\n"
NOW = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)
BASE = P(coins=100, total_catches=20, catch_streak=3, best_streak=5, daily_streak=4, owned=10, shinies=1,
         favourites=2, dex_caught=5, dex_seen=6, dex_total=40)


def run(coro):
    return asyncio.run(coro)


def png(status=BASE):
    return run(sc.status_card_png(status))


def img(status=BASE):
    return Image.open(io.BytesIO(png(status))).convert("RGB")


def tile(status, i):
    x, y = sc._tile_xy(i)
    return img(status).crop((int(x) - 2, int(y) - 2, int(x) + sc._TILE_W + 2, int(y) + sc._TILE_H + 2)).tobytes()


def changed(**over):
    return P(**{**{f: getattr(BASE, f) for f in BASE.__slots__}, **over})


# field -> (new value, the one tile that draws it)
FIELDS = {
    "coins": (101, 0), "total_catches": (21, 1), "owned": (11, 2), "shinies": (2, 2), "favourites": (3, 2),
    "catch_streak": (4, 3), "best_streak": (6, 3), "daily_streak": (5, 4), "daily_ready_at": (NOW, 4),
    "dex_caught": (6, 5), "dex_seen": (7, 5), "dex_total": (41, 5),
}


@pytest.fixture(autouse=True)
def fresh_cache():
    sc._cached.cache_clear()
    yield
    sc._cached.cache_clear()


# ---------------------------------------------------------------- rendering

def test_card_is_a_real_png_of_the_documented_size():
    data = png()
    assert data.startswith(PNG) and Image.open(io.BytesIO(data)).size == (W, sc.STATUS_H)


def test_every_number_changes_the_card():
    seen = {png()}
    for name, (value, _tile) in FIELDS.items():
        seen.add(png(changed(**{name: value})))
    assert len(seen) == 1 + len(FIELDS)


@pytest.mark.parametrize("name", list(FIELDS))
def test_each_number_only_changes_its_own_tile(name):
    value, own = FIELDS[name]
    other = changed(**{name: value})
    for i in range(6):
        if i == own:
            assert tile(BASE, i) != tile(other, i), f"{name} not drawn in tile {i}"
        else:
            assert tile(BASE, i) == tile(other, i), f"{name} leaked into tile {i}"


def test_negative_numbers_are_drawn_as_zero_and_extremes_do_not_crash():
    assert sc._key(P(coins=-5, dex_total=-1)) == sc._key(P())
    assert png(P(coins=-5, total_catches=-1, owned=-2)) == png(P())
    big = P(coins=10**15, total_catches=10**12, owned=10**9, shinies=10**9, favourites=10**9, catch_streak=10**6,
            best_streak=10**6, daily_streak=10**6, dex_caught=10**6, dex_seen=10**6, dex_total=10**6)
    assert png(big).startswith(PNG)
    assert png(P(dex_caught=9, dex_seen=9, dex_total=0)).startswith(PNG)    # a total of 0 never divides
    assert png(P(dex_caught=50, dex_seen=50, dex_total=40)).startswith(PNG)  # more than the total never overflows


def test_the_daily_countdown_is_never_drawn_only_ready_or_not():
    assert sc._key(P(daily_ready_at=NOW))[8] is False and sc._key(P())[8] is True
    assert png(P(daily_ready_at=NOW)) == png(P(daily_ready_at=datetime(2030, 1, 1, tzinfo=timezone.utc)))
    assert png(P(daily_ready_at=NOW)) != png(P())


def test_renders_are_cached_and_each_send_gets_a_fresh_file():
    png(BASE)
    png(changed())
    assert sc._cached.cache_info().currsize == 1
    a, b = sc.status_file(png()), sc.status_file(png())
    assert a is not b and a.filename == sc.STATUS_FILE == "status.png"


def test_ready_chip_is_green_and_waiting_chip_is_not():
    x, y = sc._tile_xy(4)
    px = (int(x) + 27, int(y) + 91)
    ready, waiting = img(BASE).getpixel(px), img(changed(daily_ready_at=NOW)).getpixel(px)
    assert ready[1] > 180 and ready[0] < 120      # state "success" green
    assert not (waiting[1] > 180 and waiting[0] < 120) and waiting != ready


def test_dex_bar_shows_caught_then_seen_then_empty():
    x, y = sc._tile_xy(5)
    left, right, row = int(x) + 24, int(x) + sc._TILE_W - 20, int(y) + 84
    at = lambda f: img(P(dex_caught=10, dex_seen=10, dex_total=40)).convert("L").getpixel((int(left + (right - left) * f), row))  # noqa: E731
    caught, seen, empty = at(0.12), at(0.37), at(0.75)
    assert caught > seen > empty


def test_each_tile_shows_the_right_numbers_in_the_right_order():
    big, sub = sc._lines(*sc._key(BASE)[:8], *sc._key(BASE)[9:])
    assert big == ("100", "20", "10", "3", "4", "5 / 40")
    assert sub == ("", "ALL TIME", "1 SHINY  \u00b7  2 FAV", "BEST 5", None, "6 SEEN")
    big, sub = sc._lines(1234567, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)
    assert big[0] == "1,234,567"  # thousands separators


def test_the_daily_chip_says_ready_or_waiting(monkeypatch):
    labels = []
    real = sc._pill
    monkeypatch.setattr(sc, "_pill", lambda draw, x, y, label, *a, **k: labels.append(label) or real(draw, x, y, label, *a, **k))
    png(P())
    png(P(daily_ready_at=NOW, coins=7))
    assert labels == ["READY", "WAITING"]


def test_every_tile_draws_its_icon():
    # An empty tile is a flat dark panel, so a drawn coin or sigil shows up as a wide brightness spread.
    zero = img(P()).convert("L")
    for i in range(6):
        x, y = sc._tile_xy(i)
        lo, hi = zero.crop((int(x + sc._TILE_W - 40), int(y + 18), int(x + sc._TILE_W - 20), int(y + 38))).getextrema()
        assert hi - lo > 40, f"no icon in tile {i}"


# ---------------------------------------------------------------- the wrapper

def test_status_art_sets_the_image_drops_the_replaced_fields_and_keeps_daily():
    embed = views.status_embed(BASE)
    file = run(sc.status_art(embed, BASE))
    names = [f.name for f in embed.fields]
    assert file.filename == "status.png" and embed.image.url == "attachment://status.png"
    assert names == [text("status.daily")]
    waiting = views.status_embed(changed(daily_ready_at=NOW))
    run(sc.status_art(waiting, changed(daily_ready_at=NOW)))
    assert f"<t:{int(NOW.timestamp())}:R>" in waiting.fields[0].value  # the live countdown stays as text


def test_a_failed_render_returns_none_and_leaves_the_embed_alone(monkeypatch):
    async def boom(status):
        raise RuntimeError("no font")

    monkeypatch.setattr(sc, "status_card_png", boom)
    embed = views.status_embed(BASE)
    before = [(f.name, f.value) for f in embed.fields]
    assert run(sc.status_art(embed, BASE)) is None
    assert embed.image.url is None and [(f.name, f.value) for f in embed.fields] == before and len(before) == 6


def test_the_plain_embed_is_unchanged():
    embed = views.status_embed(BASE)
    assert len(embed.fields) == 6 and embed.image.url is None


# ---------------------------------------------------------------- open_status

def setup(monkeypatch, rec, *, allowed=True, status=BASE, load_fails=False):
    monkeypatch.setattr(views, "check_player_allowed",
                        slow(rec, "gate", SimpleNamespace(allowed=allowed, reason=None if allowed else "off")))
    if load_fails:
        async def boom(*a, **k):
            raise RuntimeError("db down")
        monkeypatch.setattr(views, "load_status", boom)
    else:
        monkeypatch.setattr(views, "load_status", slow(rec, "load", status))
    seen = []

    async def spy(s):
        rec.add("render")
        seen.append(s)
        return await REAL(s)

    monkeypatch.setattr(sc, "status_card_png", spy)
    inter = make_interaction(rec)
    sent = []

    async def send(*a, **kw):
        rec.add("send")
        sent.append((a, kw))

    inter.followup = SimpleNamespace(send=send)
    return inter, sent, seen


REAL = sc.status_card_png


def test_open_status_sends_the_card_after_response_gate_load_and_render(monkeypatch):
    rec = Recorder()
    inter, sent, seen = setup(monkeypatch, rec)
    run(views.open_status(inter))
    assert rec.calls == ["response", "gate", "load", "render", "send"]
    kw = sent[0][1]
    assert kw["ephemeral"] is True and kw["file"].filename == "status.png"
    assert kw["embed"].image.url == "attachment://status.png"
    assert [f.name for f in kw["embed"].fields] == [text("status.daily")]
    assert seen[0] is BASE  # the loaded status, not a copy or a default, reaches the renderer


def test_a_failed_card_still_sends_the_full_plain_embed(monkeypatch):
    rec = Recorder()
    inter, sent, _ = setup(monkeypatch, rec)

    async def boom(s):
        raise RuntimeError("x")

    monkeypatch.setattr(sc, "status_card_png", boom)
    run(views.open_status(inter))
    kw = sent[0][1]
    assert "file" not in kw and kw["ephemeral"] is True
    assert len(kw["embed"].fields) == 6 and kw["embed"].image.url is None


def test_a_refused_player_gets_no_card_and_no_load(monkeypatch):
    rec = Recorder()
    inter, sent, _ = setup(monkeypatch, rec, allowed=False)
    run(views.open_status(inter))
    assert "load" not in rec.calls and "render" not in rec.calls
    assert sent[0][0][0] == text("catch.unavailable", reason="off")


def test_a_load_failure_is_reported_without_a_card(monkeypatch):
    rec = Recorder()
    inter, sent, _ = setup(monkeypatch, rec, load_fails=True)
    run(views.open_status(inter))
    assert "render" not in rec.calls and sent[0][0][0] == text("status.error")
