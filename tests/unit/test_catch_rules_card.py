"""Rules banner: rendering, the optional-card rules, and how open_rules uses it."""

import asyncio
import io
from types import SimpleNamespace

import discord
import pytest
from PIL import Image

import discord_bot.cogs._views_catch_rules as views
from modules import catch_rules_card as rc
from modules.catch_setup import CatchSetup
from tests.unit.test_catch_interaction_timing import Recorder, assert_response_first, make_interaction, slow

REAL_CACHED = rc._cached_rules  # the failure tests replace the module attribute
PNG = b"\x89PNG\r\n\x1a\n"
TITLE = (120, 28, 420, 62)
PILL_INNER = (585, 36, 650, 54)  # inside the pill, clear of its outline
SPEED = (120, 68, 420, 90)


@pytest.fixture(autouse=True)
def _fresh_cache():
    clear = getattr(REAL_CACHED, "cache_clear", lambda: None)  # not the patched attribute
    clear()
    yield
    clear()


def run(coro):
    return asyncio.run(coro)


def setup(**kw):
    base = dict(enabled=True, speed_preset="normal", spawn_every_n_messages=25,
                min_seconds_between_spawns=90, despawn_seconds=300)
    base.update(kw)
    return CatchSetup(**base)


def drawn(s):
    return rc._cached_rules(*rc._key(s.enabled, s.speed_preset, s.spawn_every_n_messages,
                                     s.min_seconds_between_spawns, s.despawn_seconds))


def img(data):
    return Image.open(io.BytesIO(data)).convert("RGB")


def tile_box(i):
    gap = 14
    tw = (720 - 48 - gap * 2) / 3
    x = 24 + i * (tw + gap)
    return (int(x), 120, int(x + tw), 226)


def test_card_is_a_720_wide_png():
    data = drawn(setup())
    assert data.startswith(PNG) and img(data).size == (720, rc.RULES_H)


def test_durations_read_as_minutes_only_when_whole():
    assert rc._duration(90) == "90 SEC" and rc._duration(300) == "5 MIN" and rc._duration(60) == "1 MIN"
    assert rc._duration(0) == "0 SEC" and rc._duration(-5) == "0 SEC"
    assert rc._duration(45) == "45 SEC"


def test_key_normalises_odd_values():
    assert rc._key(True, "bogus", 25, 90, 300) == rc._key(True, "normal", 25, 90, 300)
    assert rc._key(1, "fast", -3, 90, 300)[2] == 0
    assert rc._key(True, "fast", 10**12, 90, 300)[2] == rc.MAX_SHOWN
    assert rc._key(True, "fast", 12, 10**12, 300)[3].endswith("MIN")


def test_on_and_off_look_different_and_off_is_not_green():
    on, off = img(drawn(setup(enabled=True))), img(drawn(setup(enabled=False)))
    assert on.tobytes() != off.tobytes()
    r, g, b = on.getpixel((360, 10))
    assert g > r and g > b
    r, g, b = off.getpixel((360, 10))
    assert abs(g - r) < 25


def test_each_value_changes_its_own_tile_only():
    base = img(drawn(setup()))
    cases = (("spawn_every_n_messages", 12, 0), ("min_seconds_between_spawns", 45, 1), ("despawn_seconds", 120, 2))
    for field, value, tile in cases:
        other = img(drawn(setup(**{field: value})))
        for i in range(3):
            same = base.crop(tile_box(i)).tobytes() == other.crop(tile_box(i)).tobytes()
            assert same == (i != tile), (field, i)


def test_preset_name_is_drawn_and_changes():
    a, b = img(drawn(setup(speed_preset="fast"))), img(drawn(setup(speed_preset="slow")))
    assert a.crop(SPEED).tobytes() != b.crop(SPEED).tobytes()


def test_title_and_state_pill_are_drawn():
    for enabled in (True, False):
        data = img(drawn(setup(enabled=enabled)))
        assert data.crop(TITLE).convert("L").getextrema()[1] > 150
        assert data.crop(PILL_INNER).convert("L").getextrema()[1] > 200  # the label, not the outline


def test_pill_label_differs_between_on_and_off():
    on, off = img(drawn(setup(enabled=True))), img(drawn(setup(enabled=False)))
    a, b = on.crop(PILL_INNER).convert("L"), off.crop(PILL_INNER).convert("L")
    bright = lambda im: im.point(lambda p: 255 if p > 200 else 0).tobytes()
    assert bright(a) != bright(b)


def test_unknown_preset_draws_like_normal():
    assert drawn(setup(speed_preset="bogus")) == drawn(setup(speed_preset="normal"))


def test_card_is_cached_per_setup():
    s = setup()
    assert drawn(s) is drawn(s)
    assert drawn(s) is not drawn(setup(despawn_seconds=120))


def test_channels_and_roles_are_never_drawn():
    a = drawn(setup(spawn_channel_ids=(1, 2, 3), rare_ping_role_id=9, announce_channel_id=8))
    assert a == drawn(setup())


def test_art_sets_the_image_and_returns_a_fresh_file():
    e1, e2 = discord.Embed(title="t"), discord.Embed(title="t")
    f1, f2 = run(rc.rules_art(e1, setup())), run(rc.rules_art(e2, setup()))
    assert f1 is not f2 and f1.filename == rc.RULES_FILE
    assert e1.image.url == f"attachment://{rc.RULES_FILE}"


def test_failed_render_leaves_the_embed_untouched(monkeypatch):
    def boom(*a):
        raise RuntimeError("no font")

    monkeypatch.setattr(rc, "_cached_rules", boom)
    embed = discord.Embed(title="t", description="d")
    assert run(rc.rules_art(embed, setup())) is None and embed.image.url is None


def test_a_broken_setup_object_falls_back(monkeypatch):
    embed = discord.Embed(title="t")
    assert run(rc.rules_art(embed, SimpleNamespace())) is None and embed.image.url is None


def open_rules(monkeypatch, s=None, rec=None, allowed=True):
    rec = rec or Recorder()
    monkeypatch.setattr(views, "check_player_allowed", slow(rec, "gate", SimpleNamespace(allowed=allowed, reason="game_disabled")))
    monkeypatch.setattr(views, "load_setup", slow(rec, "load", s or setup()))
    inter = make_interaction(rec)
    inter.guild_id = 1
    sent = []

    async def followup_send(*args, **kwargs):
        sent.append((args, kwargs))

    inter.followup = SimpleNamespace(send=followup_send)
    run(views.open_rules(inter))
    return sent, rec


def test_open_rules_sends_the_card_and_keeps_all_the_text(monkeypatch):
    s = setup(spawn_channel_ids=(11, 12), encounter_channel_ids=(13,), announce_channel_id=14, rare_ping_role_id=15)
    sent, _ = open_rules(monkeypatch, s)
    (args, kwargs), = sent
    plain = views.rules_embed(s)
    assert kwargs["file"].filename == rc.RULES_FILE and kwargs["ephemeral"] is True
    embed = kwargs["embed"]
    assert embed.image.url == f"attachment://{rc.RULES_FILE}"
    assert [(f.name, f.value) for f in embed.fields] == [(f.name, f.value) for f in plain.fields]
    assert embed.title == plain.title and embed.description == plain.description


def test_open_rules_falls_back_to_the_plain_embed(monkeypatch):
    async def none(embed, s):
        return None

    monkeypatch.setattr(views, "rules_art", none)
    (args, kwargs), = open_rules(monkeypatch)[0]
    assert "file" not in kwargs and kwargs["embed"].image.url is None and kwargs["embed"].fields


def test_open_rules_still_responds_first_and_gates_before_loading(monkeypatch):
    _, rec = open_rules(monkeypatch)
    assert_response_first(rec)
    assert rec.calls.index("gate") < rec.calls.index("load")


def test_refusal_stays_plain_text_and_loads_nothing(monkeypatch):
    sent, rec = open_rules(monkeypatch, allowed=False)
    assert "embed" not in sent[0][1] and "file" not in sent[0][1] and "load" not in rec.calls


def test_load_error_stays_plain_text(monkeypatch):
    rec = Recorder()

    async def gate(*a, **k):
        return SimpleNamespace(allowed=True, reason=None)

    async def load(*a):
        raise RuntimeError("db")

    monkeypatch.setattr(views, "check_player_allowed", gate)
    monkeypatch.setattr(views, "load_setup", load)
    inter = make_interaction(rec)
    inter.guild_id = 1
    sent = []

    async def followup_send(*args, **kwargs):
        sent.append((args, kwargs))

    inter.followup = SimpleNamespace(send=followup_send)
    run(views.open_rules(inter))
    assert "embed" not in sent[0][1] and "file" not in sent[0][1]
