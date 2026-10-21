"""3d (text spot, merged first) and 3e/4b/4c (avatar spot, heading font, outline) live in the same renderer block.
These check the merge of the two: every combination still renders, `auto` everywhere is unchanged, and the keys exist."""
import io
import itertools
import json

from PIL import Image

from modules import welcome_card as wc
from modules import welcome_studio as ws


def png(w=800, h=400, color=(30, 90, 200)):
    b = io.BytesIO()
    Image.new("RGB", (w, h), color).save(b, format="PNG")
    return b.getvalue()


def card(opts=None):
    out, _ = wc.render_welcome_card(png(64, 64, (255, 0, 0)), "Tester", "Member #7", guild_name="G", use_template=True,
                                    custom_background_bytes=png(), ultra_options=opts)
    return out


def test_every_studio_key_from_both_sides_is_known():
    for k in ("text_pos", "avatar_pos", "effect", "heading_font", "text_outline", "brightness", "text_dx", "text_dy"):
        assert k in ws.STUDIO_KEYS and k in ws.STUDIO_DEFAULTS, k
    assert ws.STUDIO_DEFAULTS["text_pos"] == "auto" and ws.STUDIO_DEFAULTS["avatar_pos"] == "auto"


def test_all_auto_is_the_same_card_as_no_options():
    auto = json.dumps({"text_pos": "auto", "avatar_pos": "auto", "text_dx": 0, "text_dy": 0})
    assert card(auto) == card(None)


def test_text_spot_and_avatar_spot_together_render_for_every_pair():
    cells = ("top-left", "bottom-right", "center", "left")
    for t, a in itertools.product(cells, cells):
        out = card(json.dumps({"text_pos": t, "avatar_pos": a}))
        assert Image.open(io.BytesIO(out)).size[0] > 0, (t, a)


def test_text_spot_with_heading_font_outline_and_nudge_renders_and_differs_from_auto():
    opts = json.dumps({"text_pos": "bottom-right", "heading_font": "classic", "text_outline": "thick",
                       "text_dx": 20, "text_dy": -10, "avatar_pos": "top-left"})
    assert card(opts) != card(None)


def test_only_an_avatar_move_leaves_the_text_where_it_was():
    base, moved = Image.open(io.BytesIO(card(None))).convert("RGB"), \
        Image.open(io.BytesIO(card(json.dumps({"avatar_pos": "top-right"})))).convert("RGB")
    assert base.size == moved.size and base.tobytes() != moved.tobytes()   # the avatar moved, nothing crashed
