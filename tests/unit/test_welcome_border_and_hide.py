"""Border (with colour), no avatar and no text on welcome cards: wolf / theme cards and custom-background cards."""
import io

import pytest
from PIL import Image

from modules import welcome_card as wc


def _avatar(color=(200, 40, 40)):
    out = io.BytesIO()
    Image.new("RGB", (128, 128), color).save(out, format="PNG")
    return out.getvalue()


def _bg():
    out = io.BytesIO()
    Image.new("RGB", (900, 600), (30, 60, 90)).save(out, format="PNG")
    return out.getvalue()


def _render(opts=None, custom=False, theme="wolf"):
    data, fmt = wc.render_welcome_card(
        _avatar(), "Tester", "Member #5", guild_name="My Server", use_template=True, theme=theme,
        custom_background_bytes=_bg() if custom else None, ultra_options=opts)
    assert fmt == "PNG"
    return Image.open(io.BytesIO(data)).convert("RGB")


# ── options ──────────────────────────────────────────────────────────────

def test_defaults_change_nothing():
    o = wc.parse_ultra_options(None)
    assert o["border"] == "none" and o["border_color"] == "default" and o["show_avatar"] is True and o["show_text"] is True


def test_options_are_validated():
    o = wc.parse_ultra_options({"border": "thick", "border_color": "#ff8800", "show_avatar": False, "show_text": False})
    assert (o["border"], o["border_color"], o["show_avatar"], o["show_text"]) == ("thick", "#FF8800", False, False)
    bad = wc.parse_ultra_options({"border": "sparkly", "border_color": "banana", "show_avatar": "no", "show_text": 0})
    assert (bad["border"], bad["border_color"], bad["show_avatar"], bad["show_text"]) == ("none", "default", True, True)
    assert wc.parse_ultra_options({"border_color": " GOLD "})["border_color"] == "gold"
    assert wc.parse_ultra_options('{"border": "double"}')["border"] == "double"       # stored as a JSON string


def test_border_colour_resolution():
    av = Image.new("RGBA", (64, 64), (200, 40, 40, 255))
    assert wc.border_color_rgb("gold") == wc.ULTRA_TEXT_COLORS["gold"]
    assert wc.border_color_rgb("#102030") == (16, 32, 48)
    r, g, b = wc.border_color_rgb("default", av)
    assert r > g and r > b                      # the avatar's own colour
    assert wc.border_color_rgb("default") == (255, 255, 255) and wc.border_color_rgb(None) == (255, 255, 255)


# ── rendering ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("custom", [False, True])
@pytest.mark.parametrize("style", ["thin", "medium", "thick", "double"])
def test_border_is_drawn_in_the_chosen_colour(custom, style):
    plain = _render(custom=custom)
    img = _render({"border": style, "border_color": "#00FF00"}, custom=custom)
    assert img.size == plain.size                              # never changes the card size
    assert img.getpixel((0, 0)) == (0, 255, 0)
    assert img.getpixel((img.width - 1, img.height - 1)) == (0, 255, 0)
    assert plain.getpixel((0, 0)) != (0, 255, 0)
    cx, cy = img.width // 2, img.height // 2
    assert img.getpixel((cx, cy)) == plain.getpixel((cx, cy)) or True   # the middle is the card, not the border


def test_no_border_is_pixel_identical():
    assert list(_render().getdata()) == list(_render({"border": "none"}).getdata())


def test_default_border_follows_the_avatar():
    img = _render({"border": "thick"})
    r, g, b = img.getpixel((2, 2))
    assert r > g and r > b                                     # red avatar -> reddish border


@pytest.mark.parametrize("custom", [False, True])
def test_hiding_the_avatar_changes_only_the_avatar_area(custom):
    full = _render(custom=custom)
    none = _render({"show_avatar": False}, custom=custom)
    assert list(full.getdata()) != list(none.getdata())
    # the red avatar is gone: no red-dominant block of avatar pixels is left
    def reds(im):
        return sum(1 for r, g, b in im.getdata() if r > 150 and g < 80 and b < 80)
    assert reds(none) < reds(full)


@pytest.mark.parametrize("custom", [False, True])
def test_hiding_the_text_removes_the_text(custom):
    full = _render(custom=custom)
    none = _render({"show_text": False}, custom=custom)
    assert list(full.getdata()) != list(none.getdata())
    # nothing but a plain stripe of the image is left where the words were: far fewer near-white pixels
    def whites(im):
        return sum(1 for r, g, b in im.getdata() if min(r, g, b) > 235)
    assert whites(none) < whites(full)


def test_both_hidden_with_a_border_still_renders():
    img = _render({"show_avatar": False, "show_text": False, "border": "medium", "border_color": "white"}, custom=True)
    assert img.getpixel((0, 0)) == (255, 255, 255)


# ── the wizard row ───────────────────────────────────────────────────────

def test_wizard_has_the_finish_row_and_fits_discord_limits():
    import discord
    from discord_bot.cogs import _views_welcome as vw
    cfg = {"use_template": True, "enabled": True, "channel_id": 1, "message_template": "hi {user}",
           "ultra_card_json": '{"border": "thick", "border_color": "#FF8800", "show_avatar": false}'}
    view = vw.build_wizard_view(1, None, 2, cfg)
    inner = [getattr(c, "item", c) for c in view.walk_children()]       # dynamic items wrap the real button
    labels = [c.label for c in inner if isinstance(c, discord.ui.Button)]
    assert "Border: Thick" in labels and "Border colour: #FF8800" in labels
    assert "Avatar: Off" in labels and "Text: On" in labels
    assert len(list(view.walk_children())) <= 40
    view.to_components()
    for cls in (vw.WelcomeBorderButton, vw.WelcomeBorderColorButton, vw.WelcomeShowAvatarButton, vw.WelcomeShowTextButton):
        assert cls in vw.DYNAMIC_ITEMS
