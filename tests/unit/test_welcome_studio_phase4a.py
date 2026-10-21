"""Welcome Card Studio Phase 4a: one look for the background picture (effect), default none."""
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


def photo():
    im = Image.new("RGB", (800, 400))
    px = im.load()
    for y in range(400):
        for x in range(800):
            px[x, y] = (40 + x % 200, 90 + y % 120, 200 - (x // 4) % 150)
    b = io.BytesIO()
    im.save(b, format="PNG")
    return b.getvalue()


def avatar():
    b = io.BytesIO()
    Image.new("RGB", (64, 64), (255, 0, 0)).save(b, format="PNG")
    return b.getvalue()


def card(opts=None):
    out, _ = wc.render_welcome_card(avatar(), "Tester", "Member #7", guild_name="G", use_template=True,
                                    custom_background_bytes=photo(), ultra_options=opts)
    return out


def kinds(v):
    return {re.match(r"cardwz_([a-z]+):", c.custom_id).group(1) for c in v.walk_children() if getattr(c, "custom_id", None)}


def test_parser_default_and_bad_values():
    assert wc.parse_ultra_options(None)["effect"] == "none"
    assert wc.parse_ultra_options({"effect": "lasers"})["effect"] == "none"
    assert wc.parse_ultra_options({"effect": 3})["effect"] == "none"
    for e in wc.ULTRA_EFFECTS:
        assert wc.parse_ultra_options({"effect": e})["effect"] == e


def test_none_is_pixel_identical_to_before():
    assert card(None) == card({"effect": "none"})


def test_every_effect_changes_the_card_and_keeps_its_size():
    base = card(None)
    outs = {}
    for e in wc.ULTRA_EFFECTS[1:]:
        outs[e] = card({"effect": e})
        assert outs[e] != base, e
        assert Image.open(io.BytesIO(outs[e])).size == Image.open(io.BytesIO(base)).size
    assert len(set(outs.values())) == len(outs)       # all different from each other


def test_effects_touch_the_picture_not_the_avatar():
    # the pure red avatar stays pure red under every effect, including black and white
    for e in wc.ULTRA_EFFECTS[1:]:
        im = Image.open(io.BytesIO(card({"effect": e}))).convert("RGB")
        px = im.load()
        red = sum(1 for y in range(0, im.height, 4) for x in range(0, im.width, 4)
                  if px[x, y][0] > 240 and px[x, y][1] < 15 and px[x, y][2] < 15)
        assert red > 200, e


def test_bw_picture_has_no_color_and_helper_never_raises():
    out = wc._apply_effect(Image.new("RGBA", (50, 50), (200, 30, 90, 255)), "bw")
    r, g, b, a = out.getpixel((5, 5))
    assert r == g == b and a == 255
    img = Image.new("RGBA", (20, 20), (1, 2, 3, 200))
    assert wc._apply_effect(img, "nonsense").getpixel((0, 0)) == (1, 2, 3, 200)
    assert wc._apply_effect("not an image", "blur") == "not an image"
    # alpha is kept
    assert wc._apply_effect(img, "blur").getpixel((3, 3))[3] == 200


def test_works_with_every_layout_ratio_and_other_studio_options():
    for ratio in wc.ULTRA_RATIOS:
        for layout in ("banner", "centered"):
            out = card({"effect": "vignette", "ratio": ratio, "layout": layout, "avatar_pos": "top", "frame": "thin",
                        "zoom": 150, "text_dx": 20})
            assert Image.open(io.BytesIO(out)).size[0] == 1536


def test_is_a_studio_key_and_stripped_for_non_premium(monkeypatch):
    assert "effect" in ws.STUDIO_KEYS and ws.STUDIO_DEFAULTS["effect"] == "none"
    assert set(ws.STUDIO_DEFAULTS) == set(ws.STUDIO_KEYS)
    assert ws.uses_studio_options(json.dumps({"effect": "blur"})) is True
    monkeypatch.setattr(ws, "studio_unlocked", AsyncMock(return_value=False))
    out = run(ws.options_for_render({"guild_id": 1, "clone_id": None,
                                     "ultra_card_json": json.dumps({"effect": "blur", "font": "tall"})}))
    p = wc.parse_ultra_options(out)
    assert p["effect"] == "none" and p["font"] == "tall"


def build(tab, more, opts=None, unlocked=True):
    cfg = {"ultra_pack_unlocked": unlocked, "ultra_trial_active": True, "custom_background_url": "https://x/y.png"}
    if opts:
        cfg["ultra_card_json"] = json.dumps(opts)
    return cc.build_customize_view(1, None, 42, cfg, tab=tab, more=more)


def test_button_is_on_the_look_step_in_more_mode_only_and_inside_limits():
    assert "effect" in kinds(build("layout", True))
    for tab, more in (("layout", False), ("bg", True), ("text", True), ("send", False)):
        assert "effect" not in kinds(build(tab, more)), (tab, more)
    for opts in (None, {"frame": "thick", "frame_color": "gold"}):
        v = build("layout", True, opts)
        assert len(list(v.walk_children())) < 40
        for row in v.walk_children():
            if isinstance(row, discord.ui.ActionRow):
                assert len(row.children) <= 5


def test_label_registered_and_reset():
    assert "Blur" in cc.CardEffectButton(1, None, 42, "blur").item.label
    assert "None" in cc.CardEffectButton(1, None, 42).item.label
    assert cc.CardEffectButton in cc.DYNAMIC_ITEMS
    assert "effect" in cc._STEP_RESET_FIELDS["layout"]
    assert set(cc._EFFECT_LABELS) == set(wc.ULTRA_EFFECTS)


class FakeDB:
    def __init__(self, cfg):
        self.cfg, self.writes = dict(cfg), []

    async def get_welcome_config(self, g, clone_id=None):
        return dict(self.cfg)

    async def set_welcome_config(self, g, clone_id=None, **kw):
        self.writes.append(kw)
        self.cfg.update(kw)


def press(monkeypatch, premium, cfg):
    fake = FakeDB(cfg)
    monkeypatch.setattr(cc, "db", fake)
    monkeypatch.setattr(ws, "studio_unlocked", AsyncMock(return_value=premium))
    monkeypatch.setattr(cc, "_check_access", AsyncMock(return_value=True))
    monkeypatch.setattr(cc, "_rerender", AsyncMock())
    inter = MagicMock()
    inter.user.id = 42
    inter.response.defer = AsyncMock()
    run(cc.CardEffectButton(1, None, 42).callback(inter))
    return fake


def test_cycles_through_all_values_and_back(monkeypatch):
    cfg, seen = {"ultra_pack_unlocked": True}, []
    for _ in range(len(wc.ULTRA_EFFECTS)):
        f = press(monkeypatch, True, cfg)
        cfg = f.cfg
        seen.append(wc.parse_ultra_options(f.writes[-1]["ultra_card_json"])["effect"])
    assert seen == list(wc.ULTRA_EFFECTS[1:]) + ["none"]


def test_ultra_only_previews_never_stores_and_undo_works(monkeypatch):
    f = press(monkeypatch, False, {"ultra_pack_unlocked": True})
    assert f.writes == []
    eff = cc._effective_config(f.cfg, 1, None, 42)
    assert wc.parse_ultra_options(eff["ultra_card_json"])["effect"] == "blur"
    cc._STUDIO_PREVIEW.clear()
    cc._UNDO.clear()
    f = press(monkeypatch, True, {"ultra_pack_unlocked": True})
    assert wc.parse_ultra_options(f.writes[-1]["ultra_card_json"])["effect"] == "blur"
    monkeypatch.setattr(cc, "db", f)
    assert run(cc._undo_last(1, None, 42)) is True
    assert wc.parse_ultra_options(f.writes[-1]["ultra_card_json"])["effect"] == "none"
