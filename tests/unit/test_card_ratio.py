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
    assert set(wc.ULTRA_RATIOS) | {"auto"} == set(cc._RATIO_LABELS)
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
    assert {o.value for o in sel[0].item.options} == set(wc.ULTRA_RATIOS) | {"auto"}
    assert cc.CardRatioSelect in cc.DYNAMIC_ITEMS and cc.CardRatioSelect.FIELD == "ratio"


def test_reset_and_presets_keep_working_with_a_ratio():
    for preset in cc.STYLE_PRESETS.values():
        assert "ratio" not in preset["opts"]                          # a style preset never changes the card's shape
    assert wc.parse_ultra_options({"ratio": "3:1"}) != wc.parse_ultra_options(None)



# ---------- auto: the bot reads the image and picks the shape and the crop ----------
@pytest.mark.parametrize("size,expected", [
    ((1500, 1000), "3:2"), ((1920, 1080), "16:9"), ((2000, 1000), "2:1"), ((3000, 1000), "3:1"), ((4000, 1000), "3:1"),
    ((1600, 1200), "4:3"), ((1000, 1000), "1:1"), ((1080, 1920), "1:1"), ((800, 1200), "1:1"), ((1200, 1100), "1:1"),
    ((1000, 640), "3:2"), ((1280, 720), "16:9"), ((0, 0), "3:2"),
])
def test_nearest_ratio_picks_the_closest_supported_shape(size, expected):
    assert wc.nearest_ratio(*size) == expected


@pytest.mark.parametrize("size", [(1920, 1080), (3000, 1000), (1000, 1000), (1600, 1200), (900, 1600)])
def test_auto_ratio_gives_the_card_the_images_own_shape(size):
    bg = _png(size)
    png, _ = wc.render_welcome_card(AVATAR, "Test Member", "Member #1", guild_name="S", custom_background_bytes=bg,
                                    ultra_options={"ratio": "auto"})
    assert Image.open(io.BytesIO(png)).size == (1536, wc.ratio_height(wc.nearest_ratio(*size)))


def test_auto_ratio_is_valid_labelled_and_never_the_default():
    assert wc.parse_ultra_options({"ratio": "auto"})["ratio"] == "auto"
    assert wc.ULTRA_DEFAULTS["ratio"] == "3:2" and "auto" in cc._RATIO_LABELS and "auto" in cc._FOCUS_LABELS
    assert wc.parse_ultra_options({"focus": "auto"})["focus"] == "auto"


def _two_part_image(detail_side):
    """A 2400x600 picture: one half flat colour, the other half busy stripes (the 'subject')."""
    im = Image.new("RGB", (2400, 600), (90, 90, 90))
    px = im.load()
    x0 = 1200 if detail_side == "right" else 0
    for x in range(x0, x0 + 1200):
        for y in range(600):
            px[x, y] = (230, 40, 40) if (x // 12 + y // 12) % 2 else (20, 20, 220)
    b = io.BytesIO()
    im.save(b, "PNG")
    return b.getvalue()


@pytest.mark.parametrize("side", ["left", "right"])
def test_smart_crop_keeps_the_busy_half_instead_of_the_flat_middle(side):
    from PIL import ImageStat

    def busy_share(focus):
        png, _ = wc.render_welcome_card(AVATAR, "Test Member", "Member #1", guild_name="S", custom_background_bytes=_two_part_image(side),
                                        ultra_options={"ratio": "3:2", "focus": focus, "banner": "none", "show_number": False, "heading": " "})
        img = Image.open(io.BytesIO(png)).convert("L").crop((0, 0, 1536, 600))
        return sum(ImageStat.Stat(img).stddev)                    # busy stripes = a big spread of brightness
    assert busy_share("auto") > busy_share("center") * 1.2


def test_smart_crop_start_stays_inside_the_image_and_handles_tiny_or_exact_fits():
    im = Image.new("RGB", (1000, 300), (10, 10, 10))
    for window in (1, 300, 999, 1000, 5000):
        start = wc._smart_crop_start(im, 0, window)
        assert 0 <= start <= max(0, 1000 - window)
    assert 0 <= wc._smart_crop_start(Image.new("RGB", (40, 900)), 1, 500) <= 400
    assert wc._smart_crop_start(Image.new("RGB", (2, 2)), 0, 1) in (0, 1)


def test_flat_images_crop_to_the_middle():
    im = Image.new("RGB", (2000, 400), (50, 50, 50))
    start = wc._smart_crop_start(im, 0, 1000)
    assert abs(start - 500) <= 25


def test_default_look_is_still_pixel_identical_with_the_new_options_available():
    a = render(layout="banner").convert("RGB").tobytes()
    b = render(layout="banner", ratio="3:2", focus="center").convert("RGB").tobytes()
    assert hashlib.sha256(a).digest() == hashlib.sha256(b).digest() and render().size == (1536, 1024)
