"""Welcome Card Studio Phase 4b: the heading line gets its own font (heading_font), default same as the name."""
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
    assert wc.parse_ultra_options(None)["heading_font"] == "same"
    assert wc.parse_ultra_options({"heading_font": "nope"})["heading_font"] == "same"
    assert wc.parse_ultra_options({"heading_font": 4})["heading_font"] == "same"
    for f in ["same", *wc.ULTRA_FONTS]:
        assert wc.parse_ultra_options({"heading_font": f})["heading_font"] == f


def test_same_is_pixel_identical_to_before_with_and_without_a_name_font():
    assert card(None) == card({"heading_font": "same"})
    assert card({"font": "tall"}) == card({"font": "tall", "heading_font": "same"})
    # asking for the same font the name already has changes nothing either
    assert card({"font": "tall"}) == card({"font": "tall", "heading_font": "tall"})


def test_a_different_heading_font_changes_only_the_heading_line():
    base = card({"font": "clean"})
    other = card({"font": "clean", "heading_font": "script"})
    assert other != base
    a = Image.open(io.BytesIO(base)).convert("RGB")
    b = Image.open(io.BytesIO(other)).convert("RGB")
    diff = [(x, y) for y in range(0, a.height, 2) for x in range(0, a.width, 2) if a.getpixel((x, y)) != b.getpixel((x, y))]
    assert diff
    ys = [y for _, y in diff]
    # banner layout: the heading is the lowest line in the band; the name sits well above it
    assert min(ys) > 800


def test_every_font_works_in_both_layouts_with_nudge_and_avatar_spot():
    for layout in ("banner", "centered"):
        for f in wc.ULTRA_FONTS:
            out = card({"heading_font": f, "text_dx": 20, "avatar_pos": "top"}, layout)
            assert Image.open(io.BytesIO(out)).size[0] == 1536


def test_works_when_classic_name_font_and_styled_heading():
    # name stays the classic font while the heading is styled (and the other way round)
    assert card({"heading_font": "pixel"}) != card(None)
    assert card({"font": "pixel", "heading_font": "classic"}) != card({"font": "pixel"})


def test_studio_key_gate(monkeypatch):
    assert "heading_font" in ws.STUDIO_KEYS and ws.STUDIO_DEFAULTS["heading_font"] == "same"
    assert set(ws.STUDIO_DEFAULTS) == set(ws.STUDIO_KEYS)
    assert ws.uses_studio_options(json.dumps({"heading_font": "gothic"})) is True
    monkeypatch.setattr(ws, "studio_unlocked", AsyncMock(return_value=False))
    out = run(ws.options_for_render({"guild_id": 1, "clone_id": None,
                                     "ultra_card_json": json.dumps({"heading_font": "gothic", "font": "tall"})}))
    p = wc.parse_ultra_options(out)
    assert p["heading_font"] == "same" and p["font"] == "tall"


def build(tab, more, opts=None):
    cfg = {"ultra_pack_unlocked": True, "ultra_trial_active": True, "custom_background_url": "https://x/y.png"}
    if opts:
        cfg["ultra_card_json"] = json.dumps(opts)
    return cc.build_customize_view(1, None, 42, cfg, tab=tab, more=more, has_tv=True, note="x")


def test_button_is_on_the_text_step_in_more_mode_only_and_inside_limits():
    assert "headfont" in kinds(build("text", True))
    for tab, more in (("text", False), ("bg", True), ("layout", True), ("send", False)):
        assert "headfont" not in kinds(build(tab, more)), (tab, more)
    v = build("text", True, {"heading_font": "elegant"})
    assert len(list(v.walk_children())) < 40
    for row in v.walk_children():
        if isinstance(row, discord.ui.ActionRow):
            assert len(row.children) <= 5


def test_label_registered_reset():
    assert "Same as name" in cc.CardHeadingFontButton(1, None, 42).item.label
    assert "Script" in cc.CardHeadingFontButton(1, None, 42, "script").item.label
    assert cc.CardHeadingFontButton in cc.DYNAMIC_ITEMS
    assert "heading_font" in cc._STEP_RESET_FIELDS["text"]


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
    run(cc.CardHeadingFontButton(1, None, 42).callback(inter))
    return fake


def test_cycles_same_then_every_font_then_back(monkeypatch):
    cfg, seen = {"ultra_pack_unlocked": True}, []
    for _ in range(len(wc.ULTRA_FONTS) + 1):
        f = press(monkeypatch, True, cfg)
        cfg = f.cfg
        seen.append(wc.parse_ultra_options(f.writes[-1]["ultra_card_json"])["heading_font"])
    assert seen == list(wc.ULTRA_FONTS) + ["same"]


def test_ultra_only_previews_never_stores_and_undo(monkeypatch):
    f = press(monkeypatch, False, {"ultra_pack_unlocked": True})
    assert f.writes == []
    eff = cc._effective_config(f.cfg, 1, None, 42)
    assert wc.parse_ultra_options(eff["ultra_card_json"])["heading_font"] == "classic"
    cc._STUDIO_PREVIEW.clear()
    cc._UNDO.clear()
    f = press(monkeypatch, True, {"ultra_pack_unlocked": True})
    monkeypatch.setattr(cc, "db", f)
    assert run(cc._undo_last(1, None, 42)) is True
    assert wc.parse_ultra_options(f.writes[-1]["ultra_card_json"])["heading_font"] == "same"
