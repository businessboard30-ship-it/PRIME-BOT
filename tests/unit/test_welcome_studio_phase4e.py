"""Welcome Card Studio Phase 4e: film grain effect and medium frame (finishes the cheap part of Phase 4)."""
import asyncio
import io
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


def test_new_values_parse_and_old_ones_still_do():
    assert "grain" in wc.ULTRA_EFFECTS and "medium" in wc.ULTRA_FRAMES
    assert wc.parse_ultra_options({"effect": "grain"})["effect"] == "grain"
    assert wc.parse_ultra_options({"frame": "medium"})["frame"] == "medium"
    for e in ("none", "blur", "vignette", "bw", "warm", "cool", "fade"):
        assert wc.parse_ultra_options({"effect": e})["effect"] == e
    for f in ("none", "thin", "thick", "double", "rounded"):
        assert wc.parse_ultra_options({"frame": f})["frame"] == f


def test_old_values_render_pixel_identical():
    # grain and medium are new cases only: none/default untouched
    assert card(None) == card({"effect": "none", "frame": "none"})


def test_grain_changes_the_picture_but_is_repeatable_and_keeps_size_and_alpha():
    a, b = card({"effect": "grain"}), card({"effect": "grain"})
    assert a == b and a != card(None)
    src = Image.new("RGBA", (700, 300), (120, 120, 120, 90))
    out = wc._apply_effect(src, "grain")
    assert out.size == src.size and out.getpixel((5, 5))[3] == 90
    assert len({out.getpixel((x, 7))[0] for x in range(300)}) > 5    # real noise, not a flat tint
    assert wc._apply_effect("junk", "grain") == "junk"


def test_medium_frame_is_between_thin_and_thick():
    def border_px(frame):
        img = wc._draw_frame(Image.new("RGBA", (400, 200), (0, 0, 0, 255)), frame, (255, 255, 255))
        return sum(1 for x in range(60) if img.getpixel((200, x))[0] == 255)
    assert border_px("thin") < border_px("medium") < border_px("thick")


def test_labels_and_cycle_order():
    assert cc._FRAME_LABELS["medium"] == "Medium" and cc._EFFECT_LABELS["grain"] == "Film grain"
    assert list(cc._FRAME_LABELS)[:4] == ["none", "thin", "medium", "thick"]
    assert list(cc._EFFECT_LABELS)[-2:] == ["grain", "sparkle"]
    assert "Film grain" in cc.CardEffectButton(1, None, 42, "grain").item.label


def test_gate_strips_both_without_premium(monkeypatch):
    monkeypatch.setattr(ws, "studio_unlocked", AsyncMock(return_value=False))
    out = run(ws.options_for_render({"guild_id": 1, "clone_id": None,
                                     "ultra_card_json": json.dumps({"effect": "grain", "frame": "medium", "font": "tall"})}))
    p = wc.parse_ultra_options(out)
    assert p["effect"] == "none" and p["frame"] == "none" and p["font"] == "tall"


def test_look_step_stays_inside_limits_with_the_new_values():
    cfg = {"ultra_pack_unlocked": True, "ultra_trial_active": True, "custom_background_url": "https://x/y.png",
           "ultra_card_json": json.dumps({"effect": "grain", "frame": "medium"})}
    v = cc.build_customize_view(1, None, 42, cfg, tab="layout", more=True, has_tv=True, note="x")
    assert len(list(v.walk_children())) < 40
