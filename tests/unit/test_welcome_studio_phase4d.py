"""Welcome Card Studio Phase 4d: picture brightness (brightness), default normal = unchanged."""
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


def test_parser():
    assert wc.parse_ultra_options(None)["brightness"] == "normal"
    assert wc.parse_ultra_options({"brightness": "nope"})["brightness"] == "normal"
    assert wc.parse_ultra_options({"brightness": 3})["brightness"] == "normal"
    for k in wc.ULTRA_BRIGHTNESS:
        assert wc.parse_ultra_options({"brightness": k})["brightness"] == k


def test_normal_is_pixel_identical_to_before():
    assert card(None) == card({"brightness": "normal"})


def mean(b):
    img = Image.open(io.BytesIO(b)).convert("L")
    return sum(img.getdata()) / (img.width * img.height)


def test_levels_order_by_brightness_and_only_the_picture_changes():
    m = [mean(card({"brightness": k})) for k in ("darker", "dark", "normal", "light", "lighter")]
    assert m == sorted(m) and len(set(m)) == 5
    out = wc._apply_brightness(Image.new("RGBA", (10, 10), (100, 100, 100, 77)), "lighter")
    assert out.size == (10, 10) and out.getpixel((0, 0))[3] == 77 and out.getpixel((0, 0))[0] > 100
    assert wc._apply_brightness("junk", "dark") == "junk"


def test_gate_strips_it_without_premium(monkeypatch):
    assert "brightness" in ws.STUDIO_KEYS and ws.STUDIO_DEFAULTS["brightness"] == "normal"
    monkeypatch.setattr(ws, "studio_unlocked", AsyncMock(return_value=False))
    out = run(ws.options_for_render({"guild_id": 1, "clone_id": None,
                                     "ultra_card_json": json.dumps({"brightness": "lighter", "font": "tall"})}))
    p = wc.parse_ultra_options(out)
    assert p["brightness"] == "normal" and p["font"] == "tall"


def build(tab, more, opts=None):
    cfg = {"ultra_pack_unlocked": True, "ultra_trial_active": True, "custom_background_url": "https://x/y.png"}
    if opts:
        cfg["ultra_card_json"] = json.dumps(opts)
    return cc.build_customize_view(1, None, 42, cfg, tab=tab, more=more, has_tv=True, note="x")


def test_button_is_on_the_text_step_in_more_mode_only_and_inside_limits():
    assert "bright" in kinds(build("layout", True))
    for tab, more in (("layout", False), ("bg", True), ("text", True), ("send", False)):
        assert "bright" not in kinds(build(tab, more)), (tab, more)
    v = build("layout", True, {"frame": "thick", "brightness": "dark"})
    assert len(list(v.walk_children())) < 40
    for row in v.walk_children():
        if isinstance(row, discord.ui.ActionRow):
            assert len(row.children) <= 5


def test_label_registered_reset():
    assert "Normal" in cc.CardBrightnessButton(1, None, 42).item.label
    assert "Lighter" in cc.CardBrightnessButton(1, None, 42, "lighter").item.label
    assert cc.CardBrightnessButton in cc.DYNAMIC_ITEMS
    assert "brightness" in cc._STEP_RESET_FIELDS["layout"]


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
    run(cc.CardBrightnessButton(1, None, 42).callback(inter))
    return fake


def test_cycles_through_every_level_then_back(monkeypatch):
    cfg, seen = {"ultra_pack_unlocked": True}, []
    for _ in range(len(wc.ULTRA_BRIGHTNESS)):
        f = press(monkeypatch, True, cfg)
        cfg = f.cfg
        seen.append(wc.parse_ultra_options(f.writes[-1]["ultra_card_json"])["brightness"])
    assert seen == ["light", "lighter", "darker", "dark", "normal"]


def test_ultra_only_previews_never_stores_and_undo(monkeypatch):
    f = press(monkeypatch, False, {"ultra_pack_unlocked": True})
    assert f.writes == []
    eff = cc._effective_config(f.cfg, 1, None, 42)
    assert wc.parse_ultra_options(eff["ultra_card_json"])["brightness"] == "light"
    cc._STUDIO_PREVIEW.clear()
    cc._UNDO.clear()
    f = press(monkeypatch, True, {"ultra_pack_unlocked": True})
    monkeypatch.setattr(cc, "db", f)
    assert run(cc._undo_last(1, None, 42)) is True
    assert wc.parse_ultra_options(f.writes[-1]["ultra_card_json"])["brightness"] == "normal"
