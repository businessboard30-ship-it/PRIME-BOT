"""Welcome Card Studio Phase 3d: 3x3 text position (text_pos) on top of the 3c nudge and safe-area guard."""
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


def png(w=800, h=400):
    b = io.BytesIO()
    Image.new("RGB", (w, h), (30, 90, 200)).save(b, format="PNG")
    return b.getvalue()


def card(opts=None):
    out, _ = wc.render_welcome_card(png(64, 64), "Tester", "Member #7", guild_name="My Server", use_template=True,
                                    custom_background_bytes=png(), ultra_options=opts)
    return out


def white_box(opts):
    """Bounding box of the pure-white text pixels (name + heading; the member number is grey, the picture is blue)."""
    im = Image.open(io.BytesIO(card(opts))).convert("RGB")
    mask = im.convert("L").point(lambda v: 255 if v > 245 else 0)
    l, t, r, b = mask.getbbox()
    return (l, t, r - 1, b - 1), im.size


def kinds(v):
    return {re.match(r"cardwz_([a-z]+):", c.custom_id).group(1) for c in v.walk_children() if getattr(c, "custom_id", None)}


# ---------- parser ----------

def test_defaults_and_bad_values():
    assert wc.parse_ultra_options(None)["text_pos"] == "auto"
    assert wc.parse_ultra_options({"text_pos": "middle-ish"})["text_pos"] == "auto"
    assert wc.parse_ultra_options({"text_pos": 5})["text_pos"] == "auto"
    for k in wc.ULTRA_TEXT_POS:
        assert wc.parse_ultra_options({"text_pos": k})["text_pos"] == k
    assert len(wc.ULTRA_TEXT_POS) == 9


# ---------- renderer ----------

def test_auto_is_pixel_identical_to_before():
    assert card(None) == card({"text_pos": "auto"})


def test_every_cell_renders_on_every_ratio_layout_and_side():
    for pos in wc.ULTRA_TEXT_POS:
        for ratio in ("3:2", "3:1", "1:1"):
            for layout in ("banner", "centered"):
                for side in ("left", "right"):
                    out = card({"text_pos": pos, "ratio": ratio, "layout": layout, "avatar_side": side,
                                "ring": True, "shadow": True, "text_dx": 40, "text_dy": -40})
                    assert out[:4] == b"\x89PNG"


def test_cells_put_the_text_in_the_expected_corner():
    pad = wc.ULTRA_TEXT_POS_PAD
    # avatar on the right keeps the left side free; avatar on the left keeps the right side free
    (l, t, r, b), (W, H) = white_box({"text_pos": "top-left", "avatar_side": "right", "banner": "none"})
    assert abs(l - pad) <= 12 and pad - 6 <= t <= pad + 140
    (l, t, r, b), (W, H) = white_box({"text_pos": "bottom-right", "avatar_side": "left", "banner": "none"})
    assert abs(r - (W - pad)) <= 12 and b <= H - pad + 6 and b >= H - pad - 60   # +-12: glyph side bearing
    (l, t, r, b), (W, H) = white_box({"text_pos": "center", "layout": "centered", "banner": "none"})
    assert abs((l + r) / 2 - W / 2) <= 12


def test_cells_differ_from_each_other():
    cards = {k: card({"text_pos": k, "avatar_side": "right"}) for k in wc.ULTRA_TEXT_POS}
    assert len(set(cards.values())) >= 7        # guard may fold a few cells together on a given layout


def test_nudge_adds_on_top_of_the_cell():
    base = card({"text_pos": "top-right", "avatar_side": "left"})
    assert card({"text_pos": "top-right", "avatar_side": "left", "text_dy": 60}) != base


def test_cell_never_overlaps_the_avatar_on_the_left():
    # asking for the left column with the avatar on the left stops next to the avatar, left of it stays untouched
    base = Image.open(io.BytesIO(card({"text_pos": "left"}))).convert("RGB")
    plain = Image.open(io.BytesIO(card(None))).convert("RGB")
    (l, t, r, b), _ = white_box({"text_pos": "left"})
    assert l >= 60 + 247 + 24 - 2               # avatar right edge + the guard gap (classic card: 247 px avatar)
    assert base.size == plain.size


def test_centered_cell_stays_below_the_avatar():
    (l, t, r, b), (W, H) = white_box({"layout": "centered", "text_pos": "top"})
    (l0, t0, r0, b0), _ = white_box({"layout": "centered"})
    assert t >= t0 - 30                         # cannot climb over the big avatar


# ---------- gate ----------

def test_text_pos_is_a_studio_key_and_stripped_for_non_premium(monkeypatch):
    assert "text_pos" in ws.STUDIO_KEYS
    assert set(ws.STUDIO_DEFAULTS) == set(ws.STUDIO_KEYS)
    assert ws.uses_studio_options(json.dumps({"text_pos": "top"})) is True
    monkeypatch.setattr(ws, "studio_unlocked", AsyncMock(return_value=False))
    out = run(ws.options_for_render({"guild_id": 1, "clone_id": None,
                                     "ultra_card_json": json.dumps({"text_pos": "bottom", "font": "tall"})}))
    p = wc.parse_ultra_options(out)
    assert p["text_pos"] == "auto" and p["font"] == "tall"


# ---------- wizard ----------

def build(tab, more, cfg=None):
    return cc.build_customize_view(1, None, 42, cfg or {"ultra_pack_unlocked": True}, tab=tab, more=more)


def test_button_is_on_the_text_step_in_more_mode_only_and_inside_the_strict_limits():
    assert "textpos" in kinds(build("text", True))
    for tab, more in (("text", False), ("bg", True), ("layout", True), ("layout", False), ("send", False)):
        assert "textpos" not in kinds(build(tab, more)), (tab, more)
    # same strict budget as the Phase 1 test: preview attached, under 40 components, at most 5 selects
    for tab in ("bg", "layout", "text", "send"):
        for unlocked in (True, False):
            v = cc.build_customize_view(1, 7, 42, {"ultra_pack_unlocked": unlocked, "ultra_trial_active": True,
                                                    "custom_background_url": "https://x/y.png"},
                                        tab=tab, has_tv=True, note="x", more=True)
            comps = list(v.walk_children())
            assert len(comps) < 40, (tab, unlocked, len(comps))
            assert sum(isinstance(c, discord.ui.Select) for c in comps) <= 5, tab
            for row in (c for c in comps if isinstance(c, discord.ui.ActionRow)):
                assert len(row.children) <= 5


def test_button_label_registration_and_reset():
    b = cc.CardTextPosButton(1, None, 42, "top-right")
    assert "Top right" in b.item.label and "Automatic" in cc.CardTextPosButton(1, None, 42).item.label
    assert cc.CardTextPosButton in cc.DYNAMIC_ITEMS
    assert "text_pos" in cc._STEP_RESET_FIELDS["text"]
    assert list(cc._TEXT_POS_LABELS) == ["auto"] + list(wc.ULTRA_TEXT_POS)       # cycle order: auto, then the grid


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
    run(cc.CardTextPosButton(1, None, 42).callback(inter))
    return fake


def cfg_with(pos):
    return {"ultra_pack_unlocked": True, "ultra_card_json": json.dumps({"text_pos": pos})}


def test_button_cycles_through_every_cell_and_wraps(monkeypatch):
    order = ["auto"] + list(wc.ULTRA_TEXT_POS)
    for i, cur in enumerate(order):
        f = press(monkeypatch, True, cfg_with(cur))
        assert wc.parse_ultra_options(f.writes[-1]["ultra_card_json"])["text_pos"] == order[(i + 1) % len(order)]


def test_premium_saves_the_cell_and_undo_puts_it_back(monkeypatch):
    f = press(monkeypatch, True, {"ultra_pack_unlocked": True})
    assert wc.parse_ultra_options(f.writes[-1]["ultra_card_json"])["text_pos"] == "top-left"
    monkeypatch.setattr(cc, "db", f)
    assert run(cc._undo_last(1, None, 42)) is True
    assert wc.parse_ultra_options(f.writes[-1]["ultra_card_json"])["text_pos"] == "auto"


def test_ultra_only_server_previews_but_never_stores_a_cell(monkeypatch):
    f = press(monkeypatch, False, {"ultra_pack_unlocked": True})
    assert f.writes == []
    eff = cc._effective_config(f.cfg, 1, None, 42)
    assert wc.parse_ultra_options(eff["ultra_card_json"])["text_pos"] == "top-left"
