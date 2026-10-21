"""Welcome Card Studio Phase 3a: zoom + pan arrows and text size (Premium/Plus at save and join)."""
import asyncio
import io
import json
import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

import discord
from PIL import Image

from discord_bot.cogs import _views_card_customize as cc
from modules import welcome_card as wc
from modules import welcome_studio as ws

ROOT = Path(__file__).resolve().parents[2]


def run(c):
    return asyncio.run(c)


@pytest.fixture(autouse=True)
def _clean_studio_state():
    cc._STUDIO_PREVIEW.clear()
    cc._UNDO.clear()
    yield
    cc._STUDIO_PREVIEW.clear()
    cc._UNDO.clear()


def kinds(v):
    return {re.match(r"cardwz_([a-z]+):", c.custom_id).group(1) for c in v.walk_children() if getattr(c, "custom_id", None)}


def png(w=800, h=400, color=(30, 90, 200)):
    b = io.BytesIO()
    im = Image.new("RGB", (w, h), color)
    for x in range(0, w, 37):                      # stripes (odd spacing) so different crops differ
        im.paste((250, 250, 20), (x, 0, x + 9, h))
    im.save(b, format="PNG")
    return b.getvalue()


def card(opts=None, bg=None):
    avatar = png(64, 64)
    out, _fmt = wc.render_welcome_card(avatar, "Tester", "Member #7", guild_name="G", use_template=True,
                                       custom_background_bytes=bg or png(), ultra_options=opts)
    return out


# ---------- parser ----------

def test_defaults_are_the_classic_values():
    o = wc.parse_ultra_options(None)
    assert (o["zoom"], o["pan_x"], o["pan_y"], o["text_size"]) == (100, 0, 0, "medium")


def test_bad_values_fall_back_and_pan_is_clamped():
    o = wc.parse_ultra_options({"zoom": 999, "pan_x": "x", "pan_y": 5000, "text_size": "huge"})
    assert o["zoom"] == 100 and o["pan_x"] == 0 and o["pan_y"] == 100 and o["text_size"] == "medium"
    o = wc.parse_ultra_options({"zoom": True, "pan_x": True, "pan_y": -7000})
    assert o["zoom"] == 100 and o["pan_x"] == 0 and o["pan_y"] == -100
    assert wc.parse_ultra_options({"zoom": 150, "pan_x": -40, "text_size": "xl"})["zoom"] == 150


# ---------- crop box ----------

def test_zoom_window_is_smaller_centered_and_inside_the_picture():
    assert wc._zoom_pan_box((0, 0, 1000, 500), 1000, 500, 200, 0, 0) == (250, 125, 750, 375)
    l, t, r, b = wc._zoom_pan_box((0, 0, 1000, 500), 1000, 500, 200, 100, 100)
    assert (r, b) == (1000, 500)
    l, t, r, b = wc._zoom_pan_box((0, 0, 1000, 500), 1000, 500, 150, -100, -100)
    assert (l, t) == (0, 0)


def test_pan_never_leaves_the_picture_and_keeps_the_card_shape():
    for z in wc.ULTRA_ZOOMS:
        for px in (-100, -30, 0, 60, 100):
            for py in (-100, 0, 100):
                l, t, r, b = wc._zoom_pan_box((100, 0, 700, 400), 800, 400, z, px, py)
                assert 0 <= l < r <= 800 and 0 <= t < b <= 400
                assert abs((r - l) / (b - t) - 600 / 400) < 0.02


def test_pan_moves_the_crop_on_a_wide_picture_without_zoom():
    base = wc._zoom_pan_box((100, 0, 700, 400), 800, 400, 100, 0, 0)
    assert base == (100, 0, 700, 400)
    assert wc._zoom_pan_box((100, 0, 700, 400), 800, 400, 100, 100, 0)[0] > 100
    assert wc._zoom_pan_box((100, 0, 700, 400), 800, 400, 100, -100, 0)[0] == 0


# ---------- renderer ----------

def test_default_options_render_pixel_identical_to_no_options():
    assert card(None) == card({"zoom": 100, "pan_x": 0, "pan_y": 0, "text_size": "medium"})


def test_zoom_pan_and_text_size_change_the_card():
    base = card(None)
    assert card({"zoom": 200}) != base
    assert card({"pan_x": 100}) != card({"pan_x": -100})
    assert card({"text_size": "xl"}) != base and card({"text_size": "small"}) != base


def test_every_text_size_and_layout_renders_without_error():
    for size in wc.ULTRA_TEXT_SIZES:
        for layout in ("banner", "centered"):
            for ratio in ("3:2", "3:1", "1:1"):
                assert card({"text_size": size, "layout": layout, "ratio": ratio, "zoom": 150, "pan_y": 40})[:4] == b"\x89PNG"


# ---------- gate helper ----------

def test_untouched_options_do_not_use_studio():
    assert ws.uses_studio_options(None) is False
    assert ws.uses_studio_options(json.dumps({"font": "tall", "ring": True})) is False
    assert ws.uses_studio_options(json.dumps({"zoom": 125})) is True
    assert ws.uses_studio_options(json.dumps({"text_size": "large"})) is True
    assert ws.uses_studio_options("not json") is False


def test_join_options_untouched_server_gets_the_stored_value_back_with_no_lookup(monkeypatch):
    lookup = AsyncMock(return_value=True)
    monkeypatch.setattr(ws, "studio_unlocked", lookup)
    raw = json.dumps({"font": "tall"})
    assert run(ws.options_for_render({"guild_id": 1, "clone_id": None, "ultra_card_json": raw})) == raw
    assert lookup.await_count == 0


def test_join_options_premium_keeps_studio_keys(monkeypatch):
    monkeypatch.setattr(ws, "studio_unlocked", AsyncMock(return_value=True))
    raw = json.dumps({"zoom": 150, "text_size": "large"})
    assert run(ws.options_for_render({"guild_id": 1, "clone_id": None, "ultra_card_json": raw})) == raw


def test_join_options_ultra_only_or_error_strips_studio_keys_but_keeps_the_rest(monkeypatch):
    raw = json.dumps({"zoom": 150, "pan_x": 30, "text_size": "xl", "font": "tall"})
    for result in (False, RuntimeError("down")):
        mock = AsyncMock(return_value=False) if result is False else AsyncMock(side_effect=result)
        monkeypatch.setattr(ws, "studio_unlocked", mock)
        out = run(ws.options_for_render({"guild_id": 1, "clone_id": None, "ultra_card_json": raw}))
        parsed = wc.parse_ultra_options(out)
        assert (parsed["zoom"], parsed["pan_x"], parsed["text_size"]) == (100, 0, "medium")
        assert parsed["font"] == "tall"


def test_studio_keys_match_the_defaults_and_the_parser():
    assert set(ws.STUDIO_DEFAULTS) == set(ws.STUDIO_KEYS)
    d = wc.parse_ultra_options(None)
    assert all(d[k] == v for k, v in ws.STUDIO_DEFAULTS.items())


def test_join_render_uses_the_gate():
    src = (ROOT / "discord_bot/cogs/welcome.py").read_text()
    i = src.index("ultra_opts = await options_for_render(config)")
    assert "ultra_options=ultra_opts" in src[i:i + 1500]


# ---------- wizard buttons ----------

def build(tab, more, cfg=None):
    cfg = cfg or {"ultra_pack_unlocked": True}
    return cc.build_customize_view(1, None, 42, cfg, tab=tab, more=more)


def test_look_step_more_has_arrows_and_zoom_but_easy_does_not():
    assert {"panleft", "panup", "pandown", "panright", "zoom"} <= kinds(build("layout", True))
    assert not {"panleft", "panup", "pandown", "panright", "zoom"} & kinds(build("layout", False))
    assert "textsize" in kinds(build("text", True)) and "textsize" not in kinds(build("text", False))


def test_new_buttons_registered_and_inside_discord_limits():
    for c in (cc.CardPanLeftButton, cc.CardPanUpButton, cc.CardPanDownButton, cc.CardPanRightButton,
              cc.CardZoomButton, cc.CardTextSizeButton):
        assert c in cc.DYNAMIC_ITEMS
    for tab in ("bg", "layout", "text", "send"):
        v = build(tab, True, {"ultra_pack_unlocked": True, "_studio": {"unlocked": True, "pictures": 3, "needs": [], "slots": [1, 2, 3], "mode": "random"}})
        assert len(list(v.walk_children())) <= 40
        for row in v.walk_children():
            if isinstance(row, discord.ui.ActionRow):
                assert len(row.children) <= 5


def _label_for(view, field):
    for c in view.walk_children():
        it = getattr(c, "item", c)
        cid = getattr(c, "custom_id", None) or getattr(it, "custom_id", None) or ""
        if cid.startswith(f"cardwz_{field}:"):
            return getattr(it, "label", None) or getattr(c, "label", None)
    return None


def test_zoom_and_text_size_labels_show_the_current_value():
    assert "150%" in cc.CardZoomButton(1, None, 42, 150).item.label
    assert "Extra large" in cc.CardTextSizeButton(1, None, 42, "xl").item.label
    v = build("layout", True, {"ultra_pack_unlocked": True, "ultra_card_json": json.dumps({"zoom": 150})})
    assert "150%" in (_label_for(v, "zoom") or "")
    v = build("text", True, {"ultra_pack_unlocked": True, "ultra_card_json": json.dumps({"text_size": "xl"})})
    assert "Extra large" in (_label_for(v, "textsize") or "")


def test_step_reset_covers_the_new_keys():
    assert {"zoom", "pan_x", "pan_y"} <= set(cc._STEP_RESET_FIELDS["layout"])
    assert "text_size" in set(cc._STEP_RESET_FIELDS["text"])


# ---------- saving: Premium writes to the database, Ultra-only stays in memory ----------

class FakeDB:
    def __init__(self, cfg):
        self.cfg = dict(cfg)
        self.writes = []

    async def get_welcome_config(self, g, clone_id=None):
        return dict(self.cfg)

    async def set_welcome_config(self, g, clone_id=None, **kw):
        self.writes.append(kw)
        self.cfg.update(kw)


def setup(monkeypatch, premium, cfg=None):
    fake = FakeDB(cfg or {"ultra_pack_unlocked": True, "ultra_card_json": json.dumps({"font": "tall"})})
    monkeypatch.setattr(cc, "db", fake)
    monkeypatch.setattr(ws, "studio_unlocked", AsyncMock(return_value=premium))
    cc._STUDIO_PREVIEW.clear()
    cc._UNDO.clear()
    return fake


def test_premium_saves_studio_options_to_the_database(monkeypatch):
    fake = setup(monkeypatch, True)
    run(cc._save_option(1, None, 42, zoom=150, pan_x=20))
    saved = json.loads(fake.writes[-1]["ultra_card_json"])
    assert saved["zoom"] == 150 and saved["pan_x"] == 20 and saved["font"] == "tall"
    assert not cc._STUDIO_PREVIEW.get(cc._draft_key(1, None, 42))


def test_ultra_only_server_keeps_studio_options_in_memory_and_previews_them(monkeypatch):
    fake = setup(monkeypatch, False)
    run(cc._save_option(1, None, 42, zoom=150, font="gothic"))
    saved = json.loads(fake.writes[-1]["ultra_card_json"])
    assert saved["font"] == "gothic" and saved["zoom"] == 100          # normal option saved, studio one not
    eff = cc._effective_config(fake.cfg, 1, None, 42)
    assert wc.parse_ultra_options(eff["ultra_card_json"])["zoom"] == 150   # but the editor preview shows it


def test_ultra_only_studio_only_change_writes_nothing(monkeypatch):
    fake = setup(monkeypatch, False)
    run(cc._save_option(1, None, 42, text_size="large"))
    assert fake.writes == []
    assert wc.parse_ultra_options(cc._effective_config(fake.cfg, 1, None, 42)["ultra_card_json"])["text_size"] == "large"


def test_step_reset_clears_the_preview_only_values(monkeypatch):
    fake = setup(monkeypatch, False)
    run(cc._save_option(1, None, 42, zoom=200, pan_x=50))
    defaults = wc.parse_ultra_options(None)
    run(cc._apply_style(1, None, 42, {k: defaults[k] for k in cc._STEP_RESET_FIELDS["layout"]}, "circle"))
    eff = wc.parse_ultra_options(cc._effective_config(fake.cfg, 1, None, 42)["ultra_card_json"])
    assert (eff["zoom"], eff["pan_x"]) == (100, 0)
    for w in fake.writes:
        stored = wc.parse_ultra_options(w.get("ultra_card_json"))
        assert stored["zoom"] == 100 and stored["pan_x"] == 0


def test_undo_never_writes_studio_values_for_ultra_only(monkeypatch):
    fake = setup(monkeypatch, False)
    run(cc._save_option(1, None, 42, zoom=150))       # preview only
    run(cc._save_option(1, None, 42, font="gothic"))  # snapshot taken before this carries the preview zoom
    assert run(cc._undo_last(1, None, 42)) is True
    stored = wc.parse_ultra_options(fake.writes[-1]["ultra_card_json"])
    assert stored["zoom"] == 100


def test_unpaid_draft_servers_are_untouched(monkeypatch):
    fake = setup(monkeypatch, False, {"ultra_pack_unlocked": False})
    run(cc._save_option(1, None, 42, zoom=150))
    assert fake.writes == []
    assert cc._get_draft(1, None, 42)["opts"]["zoom"] == 150


def test_pan_button_steps_and_stops_at_the_end(monkeypatch):
    fake = setup(monkeypatch, True, {"ultra_pack_unlocked": True, "ultra_card_json": json.dumps({"pan_x": 95})})
    monkeypatch.setattr(cc, "_check_access", AsyncMock(return_value=True))
    monkeypatch.setattr(cc, "_rerender", AsyncMock())
    inter = MagicMock()
    inter.user.id = 42
    inter.response.defer = AsyncMock()
    b = cc.CardPanRightButton(1, None, 42)
    run(b.callback(inter))
    assert json.loads(fake.writes[-1]["ultra_card_json"])["pan_x"] == 100
    n = len(fake.writes)
    run(b.callback(inter))                      # already at the end: nothing more to save
    assert len(fake.writes) == n


def test_zoom_and_text_size_buttons_cycle(monkeypatch):
    fake = setup(monkeypatch, True, {"ultra_pack_unlocked": True, "ultra_card_json": json.dumps({"zoom": 200, "text_size": "xl"})})
    monkeypatch.setattr(cc, "_check_access", AsyncMock(return_value=True))
    monkeypatch.setattr(cc, "_rerender", AsyncMock())
    inter = MagicMock()
    inter.user.id = 42
    inter.response.defer = AsyncMock()
    run(cc.CardZoomButton(1, None, 42).callback(inter))
    assert json.loads(fake.writes[-1]["ultra_card_json"])["zoom"] == 100       # wraps around
    run(cc.CardTextSizeButton(1, None, 42).callback(inter))
    assert json.loads(fake.writes[-1]["ultra_card_json"])["text_size"] == "small"


def test_renderer_does_not_import_the_studio_modules():
    src = (ROOT / "modules/welcome_card.py").read_text()
    assert "welcome_studio" not in src and "welcome_gallery" not in src
