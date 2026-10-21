"""Welcome Card Studio Phase 3e: move the avatar to one of nine spots (avatar_pos) with a safe-area guard."""
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


def png(w=800, h=400, color=(30, 90, 200)):
    b = io.BytesIO()
    Image.new("RGB", (w, h), color).save(b, format="PNG")
    return b.getvalue()


RED = (255, 0, 0)


def card(opts=None):
    out, _ = wc.render_welcome_card(png(64, 64, RED), "Tester", "Member #7", guild_name="G", use_template=True,
                                    custom_background_bytes=png(), ultra_options=opts)
    return out


def avatar_box(opts):
    """Bounding box of the (pure red) avatar pixels on the rendered card, plus the card size."""
    im = Image.open(io.BytesIO(card(opts))).convert("RGB")
    xs, ys = [], []
    px = im.load()
    for y in range(0, im.height, 2):
        for x in range(0, im.width, 2):
            r, g, b = px[x, y]
            if r > 230 and g < 40 and b < 40:
                xs.append(x)
                ys.append(y)
    assert xs, "avatar not found"
    return min(xs), min(ys), max(xs), max(ys), im.width, im.height


def kinds(v):
    return {re.match(r"cardwz_([a-z]+):", c.custom_id).group(1) for c in v.walk_children() if getattr(c, "custom_id", None)}


# ---------- parser ----------

def test_default_and_bad_values():
    assert wc.parse_ultra_options(None)["avatar_pos"] == "auto"
    assert wc.parse_ultra_options({"avatar_pos": "nowhere"})["avatar_pos"] == "auto"
    assert wc.parse_ultra_options({"avatar_pos": 5})["avatar_pos"] == "auto"
    for pos in wc.ULTRA_AVATAR_POS:
        assert wc.parse_ultra_options({"avatar_pos": pos})["avatar_pos"] == pos
    assert len(wc.ULTRA_AVATAR_POS) == 10


# ---------- guard (pure function) ----------

TEXT = [(400, 700, 300, 40)]


def test_guard_keeps_the_avatar_inside_the_card_margin():
    m = wc.ULTRA_NUDGE_MARGIN
    rect = (100, 100, 200)
    assert wc._clamp_avatar_nudge(rect, -900, 0, 1536, 1024, []) == (m - 100, 0)
    assert wc._clamp_avatar_nudge(rect, 9000, 0, 1536, 1024, []) == (1536 - m - 200 - 100, 0)
    assert wc._clamp_avatar_nudge(rect, 0, -900, 1536, 1024, []) == (0, m - 100)
    assert wc._clamp_avatar_nudge(rect, 0, 9000, 1536, 1024, []) == (0, 1024 - m - 200 - 100)


def test_guard_stops_before_the_text_and_cannot_jump_over_it():
    rect = (100, 650, 100)            # avatar left of the text line at x 400..700
    dx, dy = wc._clamp_avatar_nudge(rect, 1000, 0, 1536, 1024, TEXT, gap=24)
    assert dx > 0 and 100 + dx + 100 + 24 <= 400      # stopped short of the text, gap kept
    assert dy == 0
    # a spot on the far side of the text is not reachable by sliding through it
    assert wc._clamp_avatar_nudge(rect, 1200, 0, 1536, 1024, TEXT)[0] < 300


def test_guard_never_moves_a_default_card_and_handles_junk():
    assert wc._clamp_avatar_nudge((100, 100, 200), 0, 0, 1536, 1024, TEXT) == (0, 0)
    assert wc._clamp_avatar_nudge("junk", 10, 10, 1536, 1024, TEXT) == (0, 0)
    # already closer to the edge than the margin: that edge does not pull it back
    assert wc._clamp_avatar_nudge((5, 100, 200), -50, 0, 1536, 1024, []) == (0, 0)


# ---------- renderer ----------

def test_auto_is_pixel_identical_to_before():
    assert card(None) == card({"avatar_pos": "auto"})


def test_each_spot_puts_the_avatar_in_its_corner_or_edge_and_on_the_card():
    base = avatar_box(None)
    W, H = base[4], base[5]
    cx, cy = lambda b: (b[0] + b[2]) / 2, lambda b: (b[1] + b[3]) / 2
    for layout in ("banner", "centered"):
        seen = {}
        for pos in wc.ULTRA_AVATAR_POS[1:]:
            b = avatar_box({"avatar_pos": pos, "layout": layout})
            assert b[0] >= 0 and b[1] >= 0 and b[2] <= W and b[3] <= H, (layout, pos)
            seen[pos] = b
        # the top row sits above the bottom row, left above... left of right (text may stop the avatar, never reverse it)
        assert cy(seen["top"]) < cy(seen["bottom"]), layout
        assert cx(seen["top-left"]) <= cx(seen["top-right"]), layout
        assert cx(seen["bottom-left"]) <= cx(seen["bottom-right"]), layout


def test_a_spot_changes_the_card_and_different_spots_differ():
    base = card(None)
    a, b = card({"avatar_pos": "top-right"}), card({"avatar_pos": "bottom-left"})
    assert a != base and b != base and a != b


def test_avatar_never_lands_on_the_text():
    # draw the same card with and without the avatar spot and compare text-colored pixels is overkill: check the avatar's
    # box against the guard directly for every spot in both layouts (text boxes from a render of the default card)
    for layout in ("banner", "centered"):
        for pos in wc.ULTRA_AVATAR_POS[1:]:
            b = avatar_box({"avatar_pos": pos, "layout": layout})
            im = Image.open(io.BytesIO(card({"avatar_pos": pos, "layout": layout}))).convert("RGB")
            # white-ish text pixels must not sit inside the avatar's red box
            px = im.load()
            for y in range(b[1], b[3], 6):
                for x in range(b[0], b[2], 6):
                    r, g, bl = px[x, y]
                    assert not (r > 230 and g > 230 and bl > 230), (layout, pos, x, y)


def test_spot_works_with_ring_shape_nudge_frame_and_every_ratio_layout_side():
    for ratio in wc.ULTRA_RATIOS:
        for layout in ("banner", "centered"):
            for side in ("left", "right"):
                out = card({"avatar_pos": "bottom-right", "ratio": ratio, "layout": layout, "avatar_side": side,
                            "ring": True, "text_dx": 40, "text_dy": -20, "frame": "thick"})
                assert Image.open(io.BytesIO(out)).size[0] == 1536


# ---------- gate ----------

def test_avatar_pos_is_a_studio_key_and_stripped_for_non_premium(monkeypatch):
    assert "avatar_pos" in ws.STUDIO_KEYS and ws.STUDIO_DEFAULTS["avatar_pos"] == "auto"
    assert set(ws.STUDIO_DEFAULTS) == set(ws.STUDIO_KEYS)
    assert ws.uses_studio_options(json.dumps({"avatar_pos": "top"})) is True
    assert ws.uses_studio_options(json.dumps({"avatar_pos": "auto"})) is False
    monkeypatch.setattr(ws, "studio_unlocked", AsyncMock(return_value=False))
    out = run(ws.options_for_render({"guild_id": 1, "clone_id": None,
                                     "ultra_card_json": json.dumps({"avatar_pos": "top", "font": "tall"})}))
    p = wc.parse_ultra_options(out)
    assert p["avatar_pos"] == "auto" and p["font"] == "tall"
    monkeypatch.setattr(ws, "studio_unlocked", AsyncMock(return_value=True))
    kept = run(ws.options_for_render({"guild_id": 1, "clone_id": None,
                                      "ultra_card_json": json.dumps({"avatar_pos": "top"})}))
    assert wc.parse_ultra_options(kept)["avatar_pos"] == "top"


# ---------- wizard ----------

def build(tab, more, cfg=None, opts=None):
    cfg = dict(cfg or {"ultra_pack_unlocked": True})
    if opts:
        cfg["ultra_card_json"] = json.dumps(opts)
    return cc.build_customize_view(1, None, 42, cfg, tab=tab, more=more)


def test_button_is_on_the_look_step_in_more_mode_only():
    assert "avatarpos" in kinds(build("layout", True))
    for tab, more in (("layout", False), ("bg", True), ("text", True), ("send", False)):
        assert "avatarpos" not in kinds(build(tab, more)), (tab, more)


def test_look_step_stays_inside_discord_limits_even_with_a_frame_chosen():
    for opts in (None, {"frame": "thick", "frame_color": "gold"}):
        for unlocked in (True, False):
            v = build("layout", True, {"ultra_pack_unlocked": unlocked, "ultra_trial_active": True,
                                       "custom_background_url": "https://x/y.png"}, opts)
            assert len(list(v.walk_children())) <= 40
            assert sum(isinstance(c, discord.ui.Select) for c in v.walk_children()) <= 5
            for row in v.walk_children():
                if isinstance(row, discord.ui.ActionRow):
                    assert len(row.children) <= 5


def test_label_shows_the_current_spot():
    b = cc.CardAvatarPosButton(1, None, 42, "top-right")
    assert "Top right" in b.item.label
    assert "Automatic" in cc.CardAvatarPosButton(1, None, 42).item.label


def test_registered_and_reset():
    assert cc.CardAvatarPosButton in cc.DYNAMIC_ITEMS
    assert "avatar_pos" in cc._STEP_RESET_FIELDS["layout"]
    assert set(cc._AVATAR_POS_LABELS) == set(wc.ULTRA_AVATAR_POS)


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


def test_tapping_cycles_through_every_spot_and_back_to_auto(monkeypatch):
    cfg = {"ultra_pack_unlocked": True}
    seen = []
    for _ in range(len(wc.ULTRA_AVATAR_POS)):
        f = press(monkeypatch, cc.CardAvatarPosButton(1, None, 42), True, cfg)
        cfg = f.cfg
        seen.append(stored(f)["avatar_pos"])
    assert seen == list(wc.ULTRA_AVATAR_POS[1:]) + ["auto"]


def test_ultra_only_server_previews_but_never_stores(monkeypatch):
    f = press(monkeypatch, cc.CardAvatarPosButton(1, None, 42), False, {"ultra_pack_unlocked": True})
    assert f.writes == []
    eff = cc._effective_config(f.cfg, 1, None, 42)
    assert wc.parse_ultra_options(eff["ultra_card_json"])["avatar_pos"] == "top-left"


def test_spot_is_undoable_for_premium(monkeypatch):
    f = press(monkeypatch, cc.CardAvatarPosButton(1, None, 42), True, {"ultra_pack_unlocked": True})
    assert stored(f)["avatar_pos"] == "top-left"
    monkeypatch.setattr(cc, "db", f)
    assert run(cc._undo_last(1, None, 42)) is True
    assert stored(f)["avatar_pos"] == "auto"


def test_step_reset_puts_the_avatar_back(monkeypatch):
    f = FakeDB({"ultra_pack_unlocked": True, "ultra_card_json": json.dumps({"avatar_pos": "bottom", "font": "tall"})})
    monkeypatch.setattr(cc, "db", f)
    monkeypatch.setattr(ws, "studio_unlocked", AsyncMock(return_value=True))
    run(cc._save_option(1, None, 42, **{k: wc.ULTRA_DEFAULTS[k] for k in cc._STEP_RESET_FIELDS["layout"]
                                         if k in wc.ULTRA_DEFAULTS}))
    p = stored(f)
    assert p["avatar_pos"] == "auto" and p["font"] == "tall"
