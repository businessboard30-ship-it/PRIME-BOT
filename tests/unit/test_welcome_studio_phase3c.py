"""Welcome Card Studio Phase 3c: nudge the text block (text_dx / text_dy) with a safe-area guard."""
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
    out, _ = wc.render_welcome_card(png(64, 64), "Tester", "Member #7", guild_name="G", use_template=True,
                                    custom_background_bytes=png(), ultra_options=opts)
    return out


def kinds(v):
    return {re.match(r"cardwz_([a-z]+):", c.custom_id).group(1) for c in v.walk_children() if getattr(c, "custom_id", None)}


# ---------- parser ----------

def test_defaults_clamp_and_bad_values():
    o = wc.parse_ultra_options(None)
    assert o["text_dx"] == 0 and o["text_dy"] == 0
    o = wc.parse_ultra_options({"text_dx": 9999, "text_dy": -9999})
    assert o["text_dx"] == wc.ULTRA_NUDGE_LIMIT and o["text_dy"] == -wc.ULTRA_NUDGE_LIMIT
    o = wc.parse_ultra_options({"text_dx": "20", "text_dy": True})
    assert o["text_dx"] == 0 and o["text_dy"] == 0
    o = wc.parse_ultra_options({"text_dx": 40, "text_dy": -60})
    assert (o["text_dx"], o["text_dy"]) == (40, -60)


# ---------- safe-area guard (pure function) ----------

LINES = [(300, 700, 200, 30), (300, 740, 250, 50)]      # left 300, right 550, top 700, bottom 790


def test_guard_keeps_the_block_inside_the_card_margin():
    m = wc.ULTRA_NUDGE_MARGIN
    assert wc._clamp_text_nudge(LINES, -900, 0, 1536, 1024) == (m - 300, 0)
    assert wc._clamp_text_nudge(LINES, 9000, 0, 1536, 1024) == (1536 - m - 550, 0)
    assert wc._clamp_text_nudge(LINES, 0, -9000, 1536, 1024) == (0, m - 700)
    assert wc._clamp_text_nudge(LINES, 0, 9000, 1536, 1024) == (0, 1024 - m - 790)


def test_guard_keeps_text_off_the_avatar():
    assert wc._clamp_text_nudge(LINES, -900, 0, 1536, 1024, avoid_left=280)[0] == -20        # avatar on the left
    assert wc._clamp_text_nudge(LINES, 900, 0, 1536, 1024, avoid_right=600)[0] == 50         # avatar on the right
    assert wc._clamp_text_nudge(LINES, 0, -900, 1536, 1024, avoid_above=680)[1] == -20       # avatar above (centered)


def test_guard_never_moves_a_default_card_and_handles_no_lines():
    # a block already past the limit may stay where it is: the no-move position is always allowed
    assert wc._clamp_text_nudge([(5, 5, 100, 20)], 0, 0, 1536, 1024) == (0, 0)
    assert wc._clamp_text_nudge([(5, 5, 100, 20)], 50, 50, 1536, 1024) == (50, 50)
    assert wc._clamp_text_nudge([], 40, 40, 1536, 1024) == (0, 0)


# ---------- renderer ----------

def test_default_nudge_is_pixel_identical_to_before():
    assert card(None) == card({"text_dx": 0, "text_dy": 0})


def test_nudge_moves_the_text():
    base = card(None)
    assert card({"text_dy": -100}) != base
    assert card({"text_dx": 100}) != base
    assert card({"text_dx": 100}) != card({"text_dy": -100})


def test_text_cannot_leave_the_bottom_of_the_card():
    # the classic card already has its last line near the bottom: pushing it further down changes nothing
    assert card({"text_dy": 300}) == card(None)


def test_text_stays_off_the_avatar_on_the_left():
    base = Image.open(io.BytesIO(card(None))).convert("RGB")
    moved = Image.open(io.BytesIO(card({"text_dx": -300}))).convert("RGB")
    assert card({"text_dx": -300}) == card({"text_dx": -200})            # both cut to the same guard limit
    band = (0, 760, 320, 1024)                                           # the avatar's side of the banner
    assert base.crop(band).tobytes() == moved.crop(band).tobytes()


def test_centered_text_cannot_climb_over_the_avatar():
    opts = {"layout": "centered"}
    assert card({**opts, "text_dy": -300}) == card({**opts, "text_dy": -200})


def test_nudge_works_with_ring_shadow_and_every_ratio_layout_side():
    for ratio in ("3:2", "3:1", "1:1"):
        for layout in ("banner", "centered"):
            for side in ("left", "right"):
                for extreme in (-300, 300):
                    out = card({"ratio": ratio, "layout": layout, "avatar_side": side, "ring": True, "shadow": True,
                                "text_dx": extreme, "text_dy": extreme})
                    assert out[:4] == b"\x89PNG"
                    assert Image.open(io.BytesIO(out)).size[0] == 1536


# ---------- gate ----------

def test_nudge_keys_are_studio_keys_and_stripped_for_non_premium(monkeypatch):
    assert {"text_dx", "text_dy"} <= ws.STUDIO_KEYS
    assert set(ws.STUDIO_DEFAULTS) == set(ws.STUDIO_KEYS)
    assert ws.uses_studio_options(json.dumps({"text_dx": 20})) is True
    monkeypatch.setattr(ws, "studio_unlocked", AsyncMock(return_value=False))
    out = run(ws.options_for_render({"guild_id": 1, "clone_id": None,
                                     "ultra_card_json": json.dumps({"text_dx": 40, "text_dy": -20, "font": "tall"})}))
    p = wc.parse_ultra_options(out)
    assert p["text_dx"] == 0 and p["text_dy"] == 0 and p["font"] == "tall"


# ---------- wizard ----------

def build(tab, more, cfg=None):
    return cc.build_customize_view(1, None, 42, cfg or {"ultra_pack_unlocked": True}, tab=tab, more=more)


NUDGE_KINDS = {"nudgeleft", "nudgeup", "nudgedown", "nudgeright", "nudgecenter"}


def test_nudge_row_is_on_the_text_step_in_more_mode_only_and_inside_limits():
    assert NUDGE_KINDS <= kinds(build("text", True))
    for tab, more in (("text", False), ("bg", True), ("layout", True), ("layout", False), ("send", False)):
        assert not (NUDGE_KINDS & kinds(build(tab, more))), (tab, more)
    for tab in ("bg", "layout", "text", "send"):
        for more in (False, True):
            v = build(tab, more)
            assert len(list(v.walk_children())) <= 40, (tab, more)
            for row in v.walk_children():
                if isinstance(row, discord.ui.ActionRow):
                    assert len(row.children) <= 5


def test_registered_and_reset():
    for cls in (cc.CardNudgeLeftButton, cc.CardNudgeUpButton, cc.CardNudgeDownButton, cc.CardNudgeRightButton,
                cc.CardNudgeCenterButton):
        assert cls in cc.DYNAMIC_ITEMS
    assert {"text_dx", "text_dy"} <= set(cc._STEP_RESET_FIELDS["text"])


class FakeDB:
    def __init__(self, cfg):
        self.cfg, self.writes = dict(cfg), []

    async def get_welcome_config(self, g, clone_id=None):
        return dict(self.cfg)

    async def set_welcome_config(self, g, clone_id=None, **kw):
        self.writes.append(kw)
        self.cfg.update(kw)


def press(monkeypatch, button, premium, cfg):
    fake = FakeDB(cfg)
    monkeypatch.setattr(cc, "db", fake)
    monkeypatch.setattr(ws, "studio_unlocked", AsyncMock(return_value=premium))
    monkeypatch.setattr(cc, "_check_access", AsyncMock(return_value=True))
    monkeypatch.setattr(cc, "_rerender", AsyncMock())
    inter = MagicMock()
    inter.user.id = 42
    inter.response.defer = AsyncMock()
    run(button.callback(inter))
    return fake


def stored(fake):
    return wc.parse_ultra_options(fake.writes[-1]["ultra_card_json"])


def test_arrows_move_by_the_step_and_center_resets(monkeypatch):
    base = {"ultra_pack_unlocked": True}
    f = press(monkeypatch, cc.CardNudgeRightButton(1, None, 42), True, base)
    assert stored(f)["text_dx"] == cc._NUDGE_STEP
    f = press(monkeypatch, cc.CardNudgeLeftButton(1, None, 42), True, base)
    assert stored(f)["text_dx"] == -cc._NUDGE_STEP
    f = press(monkeypatch, cc.CardNudgeUpButton(1, None, 42), True, base)
    assert stored(f)["text_dy"] == -cc._NUDGE_STEP
    f = press(monkeypatch, cc.CardNudgeDownButton(1, None, 42), True, base)
    assert stored(f)["text_dy"] == cc._NUDGE_STEP
    f = press(monkeypatch, cc.CardNudgeCenterButton(1, None, 42), True,
              {**base, "ultra_card_json": json.dumps({"text_dx": 60, "text_dy": -40, "font": "tall"})})
    p = stored(f)
    assert p["text_dx"] == 0 and p["text_dy"] == 0 and p["font"] == "tall"


def test_arrow_stops_at_the_stored_limit_without_writing(monkeypatch):
    f = press(monkeypatch, cc.CardNudgeRightButton(1, None, 42), True,
              {"ultra_pack_unlocked": True, "ultra_card_json": json.dumps({"text_dx": wc.ULTRA_NUDGE_LIMIT})})
    assert f.writes == []


def test_center_on_an_already_centered_card_writes_nothing(monkeypatch):
    f = press(monkeypatch, cc.CardNudgeCenterButton(1, None, 42), True, {"ultra_pack_unlocked": True})
    assert f.writes == []


def test_ultra_only_server_previews_but_never_stores_a_nudge(monkeypatch):
    f = press(monkeypatch, cc.CardNudgeRightButton(1, None, 42), False, {"ultra_pack_unlocked": True})
    assert f.writes == []
    eff = cc._effective_config(f.cfg, 1, None, 42)
    assert wc.parse_ultra_options(eff["ultra_card_json"])["text_dx"] == cc._NUDGE_STEP


def test_nudge_is_undoable_for_premium(monkeypatch):
    base = {"ultra_pack_unlocked": True}
    f = press(monkeypatch, cc.CardNudgeDownButton(1, None, 42), True, base)
    assert stored(f)["text_dy"] == cc._NUDGE_STEP
    monkeypatch.setattr(cc, "db", f)
    assert run(cc._undo_last(1, None, 42)) is True
    assert stored(f)["text_dy"] == 0
