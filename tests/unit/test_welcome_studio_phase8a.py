"""Welcome Card Studio Phase 8a: photo-print and film-strip frames, avatar glow."""
import io
import json

import discord
from PIL import Image

from discord_bot.cogs import _views_card_customize as cc
from modules import welcome_card as wc
from modules import welcome_studio as ws


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


def img(b):
    return Image.open(io.BytesIO(b)).convert("RGB")


def test_values_parse_and_defaults_untouched():
    assert wc.parse_ultra_options({"frame": "polaroid"})["frame"] == "polaroid"
    assert wc.parse_ultra_options({"frame": "film"})["frame"] == "film"
    assert wc.parse_ultra_options({"avatar_glow": True})["avatar_glow"] is True
    assert wc.parse_ultra_options({"avatar_glow": "yes"})["avatar_glow"] is False
    assert wc.parse_ultra_options(None)["avatar_glow"] is False
    assert card(None) == card({"avatar_glow": False, "frame": "none"})


def test_new_keys_are_studio_gated():
    assert "avatar_glow" in ws.STUDIO_KEYS and ws.STUDIO_DEFAULTS["avatar_glow"] is False
    assert ws.uses_studio_options({"avatar_glow": True})
    stripped = ws.effective_studio_options(wc.parse_ultra_options({"avatar_glow": True, "frame": "film"}), False, ws.STUDIO_KEYS)
    assert "avatar_glow" not in stripped and "frame" not in stripped
    back = wc.parse_ultra_options(stripped)
    assert back["avatar_glow"] is False and back["frame"] == "none"


def test_polaroid_keeps_size_and_has_a_thicker_bottom():
    base, pol = img(card(None)), img(card({"frame": "polaroid", "frame_color": "gold"}))
    assert pol.size == base.size
    W, H = pol.size
    gold = wc.ULTRA_TEXT_COLORS["gold"]
    assert pol.getpixel((5, H // 2)) == tuple(gold)            # side border
    assert pol.getpixel((W // 2, H - 20)) == tuple(gold)       # thick bottom
    assert pol.getpixel((W // 2, H - 100)) == tuple(gold)      # still border at 100 px up
    assert pol.getpixel((W // 2, 10)) == tuple(gold)           # top border


def test_polaroid_shows_the_whole_card_scaled_down():
    base, pol = img(card(None)), img(card({"frame": "polaroid"}))
    W, H = base.size
    # the picture area (centre) is still the blue background colour family, nothing got cut
    r, g, b = pol.getpixel((W // 2, H // 3))
    assert b > r and (r, g, b) != (255, 255, 255)


def test_film_strip_has_dark_sides_and_light_holes():
    f = img(card({"frame": "film", "frame_color": "white"}))
    W, H = f.size
    assert f.getpixel((4, 4)) == (24, 24, 24)
    assert f.getpixel((W - 4, H - 4)) == (24, 24, 24)
    holes = [f.getpixel((36, y)) for y in range(22, 56)]
    assert any(p == (255, 255, 255) for p in holes)


def test_frames_never_raise_on_tiny_images():
    for frame in ("polaroid", "film"):
        out = wc._draw_frame(Image.new("RGBA", (50, 30), (0, 0, 0, 255)), frame, (255, 255, 255))
        assert out.size == (50, 30)


def test_avatar_glow_adds_light_around_the_avatar_only():
    plain = img(card({"text_color": "gold"}))
    glow = img(card({"text_color": "gold", "avatar_glow": True}))
    assert plain.size == glow.size and plain != glow
    diff = [(x, y) for x in range(0, plain.width, 4) for y in range(0, plain.height, 4)
            if plain.getpixel((x, y)) != glow.getpixel((x, y))]
    assert diff
    # the right half of the card (text and picture, far from the left avatar) is untouched
    assert all(x < plain.width // 2 for x, _ in diff)


def test_avatar_glow_works_in_centered_layout_and_with_a_moved_avatar():
    for o in ({"layout": "centered"}, {"avatar_pos": "top-right"}, {"ring": True}):
        a, b = card(o), card({**o, "avatar_glow": True})
        assert a != b and img(b).size == img(a).size


def test_wizard_labels_and_style_extra():
    assert cc._FRAME_LABELS["polaroid"] == "Photo print" and cc._FRAME_LABELS["film"] == "Film strip"
    assert "avatar_glow" in cc._STYLE_EXTRAS
    assert "avatar_glow" in cc._STEP_RESET_FIELDS["text"]


def test_look_and_text_steps_stay_inside_discord_limits():
    for tab in ("layout", "text"):
        for opts in (None, {"frame": "film", "avatar_glow": True}):
            cfg = {"ultra_pack_unlocked": True, "ultra_trial_active": True, "custom_background_url": "https://x/y.png"}
            if opts:
                cfg["ultra_card_json"] = json.dumps(opts)
            v = cc.build_customize_view(1, None, 42, cfg, tab=tab, more=True, has_tv=True)
            assert len(list(v.walk_children())) <= 40
            assert sum(isinstance(c, discord.ui.Select) for c in v.walk_children()) <= 5
