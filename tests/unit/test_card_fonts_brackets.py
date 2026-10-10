"""Customize Card: extra fonts and username brackets (both optional, defaults unchanged)."""
import io, os
from PIL import Image
from modules import welcome_card as wc
from discord_bot.cogs import _views_card_customize as cc


def test_defaults_unchanged():
    o = wc.parse_ultra_options(None)
    assert o["font"] == "classic" and o["bracket"] == "none"


def test_every_font_file_is_bundled_and_has_a_license_and_a_label():
    root = os.path.join(os.path.dirname(wc.__file__), "..", "assets", "fonts")
    assert set(wc.ULTRA_FONTS) == set(cc._FONT_LABELS)
    for key, fname in wc.ULTRA_FONTS.items():
        if fname:
            assert os.path.exists(os.path.join(root, fname)), key
            assert wc._style_font(key, 40) is not None, key


def test_brackets_validated_labelled_and_latin1_only():
    assert set(wc.ULTRA_BRACKETS) == set(cc._BRACKET_LABELS)
    for l, r in wc.ULTRA_BRACKETS.values():
        assert wc._styled_ok(l + r)
    assert wc.parse_ultra_options({"bracket": "square"})["bracket"] == "square"
    assert wc.parse_ultra_options({"bracket": "<script>"})["bracket"] == "none"
    assert wc.parse_ultra_options({"font": "nope"})["font"] == "classic"


def _png(color, size=(900, 400)):
    b = io.BytesIO(); Image.new("RGB", size, color).save(b, "PNG"); return b.getvalue()


def test_every_font_and_bracket_renders():
    av, bgb = _png((200, 60, 90), (128, 128)), _png((30, 40, 90))
    for font in wc.ULTRA_FONTS:
        for br in ("none", "square", "guillemet"):
            for layout in ("banner", "centered"):
                data, fmt = wc.render_welcome_card(av, "Maxwell_99", "MEMBER #42", guild_name="Prime Hub",
                                                   custom_background_bytes=bgb,
                                                   ultra_options={"font": font, "bracket": br, "layout": layout})
                assert fmt == "PNG" and Image.open(io.BytesIO(data)).size[0] > 0


def test_bracket_is_drawn_around_the_name():
    av, bgb = _png((200, 60, 90), (128, 128)), _png((30, 40, 90))
    a, _ = wc.render_welcome_card(av, "Max", "MEMBER #1", guild_name="G", custom_background_bytes=bgb, ultra_options={"bracket": "none"})
    b, _ = wc.render_welcome_card(av, "Max", "MEMBER #1", guild_name="G", custom_background_bytes=bgb, ultra_options={"bracket": "square"})
    assert a != b
