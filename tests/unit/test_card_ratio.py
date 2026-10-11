"""Customize Card: choose the card's ratio (shape). The default (3:2) renders exactly as before."""
import hashlib
import io

import pytest
from PIL import Image

from discord_bot.cogs import _views_card_customize as cc
from modules import welcome_card as wc


def _png(size, c=(40, 90, 160)):
    b = io.BytesIO()
    Image.new("RGB", size, c).save(b, "PNG")
    return b.getvalue()


AVATAR = _png((256, 256), (200, 100, 50))
BG = _png((1400, 600))


def render(**opts):
    png, fmt = wc.render_welcome_card(AVATAR, "Test Member", "Member #12", guild_name="My Server",
                                       custom_background_bytes=BG, ultra_options=opts)
    assert fmt == "PNG"
    return Image.open(io.BytesIO(png))


def test_default_ratio_is_the_classic_card():
    assert wc.ULTRA_DEFAULTS["ratio"] == "3:2" and wc.parse_ultra_options(None)["ratio"] == "3:2"
    assert wc.ratio_height("3:2") == wc.TEMPLATE_HEIGHT == 1024 and wc.TEMPLATE_WIDTH == 1536


def test_every_ratio_is_validated_labelled_and_has_the_right_height():
    assert set(wc.ULTRA_RATIOS) == set(cc._RATIO_LABELS)
    for key, (a, b) in wc.ULTRA_RATIOS.items():
        assert wc.parse_ultra_options({"ratio": key})["ratio"] == key
        assert abs(wc.ratio_height(key) - wc.TEMPLATE_WIDTH * b / a) <= 1
    assert wc.ratio_height("3:1") == 512 and wc.ratio_height("1:1") == 1536 and wc.ratio_height("16:9") == 864


@pytest.mark.parametrize("bad", ["", "7:3", "<b>", None, 5, ["3:1"], {"x": 1}, "3:1 "])
def test_junk_ratios_fall_back_to_the_default(bad):
    assert wc.parse_ultra_options({"ratio": bad})["ratio"] == "3:2"
    assert render(ratio=bad).size == (1536, 1024)


@pytest.mark.parametrize("key", list(wc.ULTRA_RATIOS))
@pytest.mark.parametrize("layout", wc.ULTRA_LAYOUTS)
def test_every_ratio_and_layout_renders_at_the_right_size(key, layout):
    assert render(ratio=key, layout=layout).size == (1536, wc.ratio_height(key))


@pytest.mark.parametrize("opts", [
    {"banner": "top"}, {"banner": "none"}, {"avatar_side": "right", "ring": True}, {"soft_edge": True, "glass": True, "shadow": True},
    {"big_name": True, "font": "tall", "bracket": "square"}, {"auto_contrast": True, "dim": "heavy"}, {"focus": "left"},
    {"layout": "centered", "big_name": True, "font": "pixel", "heading": "A very long welcome heading for testing it"},
])
@pytest.mark.parametrize("key", ["16:9", "3:1", "1:1"])
def test_other_options_still_render_on_non_classic_ratios(key, opts):
    assert render(ratio=key, **opts).size == (1536, wc.ratio_height(key))


def test_the_default_ratio_is_pixel_identical_to_not_setting_one():
    a = render(layout="centered", shadow=True).convert("RGB").tobytes()
    b = render(layout="centered", shadow=True, ratio="3:2").convert("RGB").tobytes()
    assert hashlib.sha256(a).digest() == hashlib.sha256(b).digest()


def test_the_background_is_cover_cropped_to_the_chosen_shape():
    wide = render(ratio="3:1")
    assert wide.size == (1536, 512)
    top_left, bottom_right = wide.getpixel((5, 5)), wide.getpixel((1530, 505))
    assert top_left != bottom_right                                   # the gradient is still there, not a flat fill


def test_the_text_band_keeps_a_usable_size_on_every_ratio():
    for key in wc.ULTRA_RATIOS:
        h = wc.ratio_height(key)
        band = min(int(wc.TEMPLATE_HEIGHT * 0.28), max(220, int(h * 0.28))) if h != wc.TEMPLATE_HEIGHT else int(h * 0.28)
        assert 220 <= band <= 287 and band < h


def test_the_wizard_offers_the_ratio_on_the_background_tab_and_saves_it():
    v = cc.build_customize_view(1, None, 5, {"ultra_pack_unlocked": True, "ultra_card_json": '{"ratio": "16:9"}'}, tab="bg")
    sel = [c for c in v.walk_children() if getattr(c, "custom_id", "").startswith("cardwz_ratio:")]
    assert len(sel) == 1 and [o.value for o in sel[0].item.options if o.default] == ["16:9"]
    assert {o.value for o in sel[0].item.options} == set(wc.ULTRA_RATIOS)
    assert cc.CardRatioSelect in cc.DYNAMIC_ITEMS and cc.CardRatioSelect.FIELD == "ratio"


def test_reset_and_presets_keep_working_with_a_ratio():
    for preset in cc.STYLE_PRESETS.values():
        assert "ratio" not in preset["opts"]                          # a style preset never changes the card's shape
    assert wc.parse_ultra_options({"ratio": "3:1"}) != wc.parse_ultra_options(None)

