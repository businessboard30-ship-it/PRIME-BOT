"""Welcome Card Studio Phase 4f (owner approved glow and fonts): soft glow edge, text glow, sparkle, four more OFL fonts."""
import asyncio
import io
import os
import json
import re
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


def png(w, h, color):
    b = io.BytesIO()
    Image.new("RGB", (w, h), color).save(b, format="PNG")
    return b.getvalue()


def card(opts=None, layout=None):
    o = dict(opts or {})
    if layout:
        o["layout"] = layout
    out, _ = wc.render_welcome_card(png(64, 64, (255, 0, 0)), "Tester", "Member #7", guild_name="Some Server",
                                    use_template=True, custom_background_bytes=png(800, 400, (30, 90, 200)),
                                    ultra_options=o or None)
    return out


def kinds(v):
    return {re.match(r"cardwz_([a-z]+):", c.custom_id).group(1) for c in v.walk_children() if getattr(c, "custom_id", None)}


def test_values_parse_and_defaults_are_untouched():
    assert wc.parse_ultra_options({"frame": "glow"})["frame"] == "glow"
    assert wc.parse_ultra_options({"effect": "sparkle"})["effect"] == "sparkle"
    assert wc.parse_ultra_options({"text_outline": "glow"})["text_outline"] == "glow"
    assert card(None) == card({"frame": "none", "effect": "none", "text_outline": "auto"})


def dark_free(b):
    return Image.open(io.BytesIO(b)).convert("RGB")


def test_glow_frame_is_soft_and_in_the_frame_color():
    img = wc._draw_frame(Image.new("RGBA", (600, 300), (0, 0, 0, 255)), "glow", (255, 0, 0))
    assert img.size == (600, 300)
    assert img.getpixel((300, 1))[:3] == (255, 0, 0)            # thin solid line on the edge
    mid = img.getpixel((300, 20))[:3]
    far = img.getpixel((300, 150))[:3]
    assert 0 < mid[0] < 255 and far == (0, 0, 0)                 # fades out, never reaches the middle


def test_sparkle_is_repeatable_light_and_keeps_alpha():
    src = Image.new("RGBA", (900, 400), (20, 20, 20, 90))
    a, b = wc._apply_effect(src, "sparkle"), wc._apply_effect(src, "sparkle")
    assert a.tobytes() == b.tobytes() and a.tobytes() != src.tobytes()
    assert a.getpixel((3, 3))[3] == 90
    bright = sum(1 for p in a.convert("RGB").getdata() if max(p) > 100)
    assert 0 < bright < 900 * 400 * 0.02                         # a few stars, not a wash


def test_text_glow_adds_light_around_text_and_outline_none_does_not():
    o = {"banner": "none", "text_color": "white"}
    glow, plain = card({**o, "text_outline": "glow"}), card({**o, "text_outline": "none"})
    assert glow != plain
    assert Image.open(io.BytesIO(glow)).size == Image.open(io.BytesIO(plain)).size


def test_new_fonts_are_bundled_licensed_labelled_and_render():
    root = os.path.join(os.path.dirname(wc.__file__), "..", "assets", "fonts")
    for key, fname, lic in (("cursive", "Lobster-Regular.ttf", "OFL-lobster.txt"), ("poster", "Anton-Regular.ttf", "OFL-anton.txt"),
                            ("wide", "Audiowide-Regular.ttf", "OFL-audiowide.txt"), ("marquee", "Monoton-Regular.ttf", "OFL-monoton.txt")):
        assert wc.ULTRA_FONTS[key] == fname and os.path.exists(os.path.join(root, fname))
        assert "SIL OPEN FONT LICENSE" in open(os.path.join(root, lic), encoding="utf-8").read().upper()
        assert cc._FONT_LABELS[key] and wc._style_font(key, 40) is not None
        assert card({"font": key}) != card(None) and card({"heading_font": key}) != card(None)


def test_labels_and_cycle_order():
    assert cc._FRAME_LABELS["glow"] == "Soft glow" and list(cc._FRAME_LABELS)[6] == "glow"
    assert cc._EFFECT_LABELS["sparkle"] == "Sparkle"
    assert "Glow" in cc.CardOutlineButton(1, None, 42, "glow").item.label
    assert list(wc.ULTRA_OUTLINES)[-1] == "glow"


def test_gate_strips_them_without_premium(monkeypatch):
    monkeypatch.setattr(ws, "studio_unlocked", AsyncMock(return_value=False))
    out = run(ws.options_for_render({"guild_id": 1, "clone_id": None, "ultra_card_json": json.dumps(
        {"frame": "glow", "effect": "sparkle", "text_outline": "glow", "heading_font": "marquee", "font": "tall"})}))
    p = wc.parse_ultra_options(out)
    assert (p["frame"], p["effect"], p["text_outline"], p["heading_font"]) == ("none", "none", "auto", "same") and p["font"] == "tall"


def test_steps_stay_inside_limits_with_everything_on():
    cfg = {"ultra_pack_unlocked": True, "ultra_trial_active": True, "custom_background_url": "https://x/y.png",
           "ultra_card_json": json.dumps({"frame": "glow", "effect": "sparkle", "text_outline": "glow", "font": "marquee"})}
    for tab in ("layout", "text"):
        v = cc.build_customize_view(1, None, 42, cfg, tab=tab, more=True, has_tv=True, note="x")
        assert len(list(v.walk_children())) < 40, tab
        assert sum(1 for c in v.walk_children() if isinstance(c, discord.ui.Select)) <= 5, tab
