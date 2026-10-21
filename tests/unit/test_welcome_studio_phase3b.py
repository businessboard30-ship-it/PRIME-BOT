"""Welcome Card Studio Phase 3b: picture frames (none, thin, thick, double, rounded) + frame color."""
import asyncio
import io
import json
import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from PIL import Image

from discord_bot.cogs import _views_card_customize as cc
from modules import welcome_card as wc
from modules import welcome_studio as ws


def run(c):
    return asyncio.run(c)


@pytest.fixture(autouse=True)
def _clean():
    cc._STUDIO_PREVIEW.clear()
    cc._UNDO.clear()
    yield
    cc._STUDIO_PREVIEW.clear()
    cc._UNDO.clear()


def png(w=800, h=400):
    b = io.BytesIO()
    Image.new("RGB", (w, h), (30, 90, 200)).save(b, format="PNG")
    return b.getvalue()


def card(opts=None, bg=None):
    out, _ = wc.render_welcome_card(png(64, 64), "Tester", "Member #7", guild_name="G", use_template=True,
                                    custom_background_bytes=bg or png(), ultra_options=opts)
    return out


def kinds(v):
    return {re.match(r"cardwz_([a-z]+):", c.custom_id).group(1) for c in v.walk_children() if getattr(c, "custom_id", None)}


# ---------- parser ----------

def test_defaults_and_bad_values():
    o = wc.parse_ultra_options(None)
    assert o["frame"] == "none" and o["frame_color"] == "white"
    o = wc.parse_ultra_options({"frame": "polaroid", "frame_color": "purple"})
    assert o["frame"] == "none" and o["frame_color"] == "white"
    o = wc.parse_ultra_options({"frame": "double", "frame_color": "gold"})
    assert o["frame"] == "double" and o["frame_color"] == "gold"


# ---------- renderer ----------

def test_default_frame_is_pixel_identical_to_before():
    assert card(None) == card({"frame": "none", "frame_color": "gold"})


def test_each_frame_draws_in_the_chosen_color_and_keeps_the_size():
    base = Image.open(io.BytesIO(card(None)))
    gold = (255, 215, 90)
    for f in ("thin", "thick", "double", "rounded"):
        im = Image.open(io.BytesIO(card({"frame": f, "frame_color": "gold"})))
        assert im.size == base.size
        assert im.getpixel((2, 2)) == gold, f
        assert im.getpixel((im.size[0] // 2, im.size[1] // 2)) == base.getpixel((base.size[0] // 2, base.size[1] // 2))


def test_frames_differ_from_each_other_and_color_matters():
    cards = {f: card({"frame": f}) for f in wc.ULTRA_FRAMES}
    assert len(set(cards.values())) == len(wc.ULTRA_FRAMES)
    assert card({"frame": "thick", "frame_color": "red"}) != card({"frame": "thick", "frame_color": "cyan"})


def test_frames_render_on_every_ratio_and_layout():
    for f in wc.ULTRA_FRAMES:
        for ratio in ("3:2", "3:1", "1:1"):
            for layout in ("banner", "centered"):
                assert card({"frame": f, "ratio": ratio, "layout": layout})[:4] == b"\x89PNG"


def test_double_frame_leaves_a_gap_showing_the_picture():
    im = Image.open(io.BytesIO(card({"frame": "double", "frame_color": "gold"})))
    assert im.getpixel((12, 200)) != (255, 215, 90)        # the gap between the two lines


# ---------- gate ----------

def test_frame_keys_are_studio_keys_and_stripped_for_non_premium(monkeypatch):
    assert {"frame", "frame_color"} <= ws.STUDIO_KEYS
    assert set(ws.STUDIO_DEFAULTS) == set(ws.STUDIO_KEYS)
    assert ws.uses_studio_options(json.dumps({"frame": "thin"})) is True
    monkeypatch.setattr(ws, "studio_unlocked", AsyncMock(return_value=False))
    out = run(ws.options_for_render({"guild_id": 1, "clone_id": None, "ultra_card_json": json.dumps({"frame": "thick", "font": "tall"})}))
    p = wc.parse_ultra_options(out)
    assert p["frame"] == "none" and p["font"] == "tall"


# ---------- wizard ----------

def build(more, cfg=None):
    return cc.build_customize_view(1, None, 42, cfg or {"ultra_pack_unlocked": True}, tab="layout", more=more)


def test_frame_buttons_in_more_mode_only_and_inside_limits():
    assert "frame" in kinds(build(True)) and "frame" not in kinds(build(False))
    assert "framecolor" not in kinds(build(True))                      # no color until a frame is chosen
    v = build(True, {"ultra_pack_unlocked": True, "ultra_card_json": json.dumps({"frame": "thin"})})
    assert {"frame", "framecolor"} <= kinds(v)
    assert len(list(v.walk_children())) <= 40
    for row in v.walk_children():
        if isinstance(row, discord.ui.ActionRow):
            assert len(row.children) <= 5


def test_registered_labels_and_reset():
    assert cc.CardFrameButton in cc.DYNAMIC_ITEMS and cc.CardFrameColorButton in cc.DYNAMIC_ITEMS
    assert "Thick" in cc.CardFrameButton(1, None, 42, "thick").item.label
    assert "Gold" in cc.CardFrameColorButton(1, None, 42, "gold").item.label
    assert {"frame", "frame_color"} <= set(cc._STEP_RESET_FIELDS["layout"])


class FakeDB:
    def __init__(self, cfg):
        self.cfg, self.writes = dict(cfg), []

    async def get_welcome_config(self, g, clone_id=None):
        return dict(self.cfg)

    async def set_welcome_config(self, g, clone_id=None, **kw):
        self.writes.append(kw)
        self.cfg.update(kw)


def press(monkeypatch, button, premium, cfg):
    fake = FakeDB(cfg)
    monkeypatch.setattr(cc, "db", fake)
    monkeypatch.setattr(ws, "studio_unlocked", AsyncMock(return_value=premium))
    monkeypatch.setattr(cc, "_check_access", AsyncMock(return_value=True))
    monkeypatch.setattr(cc, "_rerender", AsyncMock())
    inter = MagicMock()
    inter.user.id = 42
    inter.response.defer = AsyncMock()
    run(button.callback(inter))
    return fake


def test_frame_button_cycles_and_wraps(monkeypatch):
    fake = press(monkeypatch, cc.CardFrameButton(1, None, 42), True,
                 {"ultra_pack_unlocked": True, "ultra_card_json": json.dumps({"frame": "medium"})})
    assert json.loads(fake.writes[-1]["ultra_card_json"])["frame"] == "thick"
    fake = press(monkeypatch, cc.CardFrameButton(1, None, 42), True,
                 {"ultra_pack_unlocked": True, "ultra_card_json": json.dumps({"frame": "glow"})})
    assert json.loads(fake.writes[-1]["ultra_card_json"])["frame"] == "none"


def test_frame_color_button_cycles(monkeypatch):
    fake = press(monkeypatch, cc.CardFrameColorButton(1, None, 42), True,
                 {"ultra_pack_unlocked": True, "ultra_card_json": json.dumps({"frame": "thin", "frame_color": "white"})})
    assert json.loads(fake.writes[-1]["ultra_card_json"])["frame_color"] == "gold"


def test_ultra_only_server_previews_but_never_stores_a_frame(monkeypatch):
    fake = press(monkeypatch, cc.CardFrameButton(1, None, 42), False, {"ultra_pack_unlocked": True})
    assert fake.writes == []
    eff = cc._effective_config(fake.cfg, 1, None, 42)
    assert wc.parse_ultra_options(eff["ultra_card_json"])["frame"] == "thin"
