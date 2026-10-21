"""Welcome Card Studio follow-up: every place that draws a welcome card from the stored options goes through
options_for_render, so a server without Premium/Plus never sees (or posts) its studio look. Before this, only the real
join did; /welcome preview, the setup wizard TV and Preview button, the test post and the nudge/heads-up cards did not."""
import asyncio
import io
import json
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from PIL import Image

from discord_bot.cogs import _views_welcome as vw
from discord_bot.cogs import welcome, welcome_extras as we
from modules import welcome_studio as ws

ROOT = Path(__file__).resolve().parents[2]
SITE_FILES = ("discord_bot/cogs/welcome.py", "discord_bot/cogs/_views_welcome.py", "discord_bot/cogs/welcome_extras.py")

# only keys that exist on main (3d's text_pos is a separate PR); the gate is generic over STUDIO_KEYS
STUDIO = json.dumps({"text_dx": 40, "text_dy": -20, "frame": "thick", "zoom": 150, "pan_x": 30, "font": "tall"})


def run(c):
    return asyncio.run(c)


def png():
    b = io.BytesIO()
    Image.new("RGB", (1400, 500), (10, 20, 30)).save(b, format="PNG")
    return b.getvalue()


# ---------- nobody passes the raw stored options to the renderer any more ----------

def test_no_render_site_hands_the_raw_stored_options_to_the_renderer():
    for rel in SITE_FILES:
        src = (ROOT / rel).read_text(encoding="utf-8")
        assert not re.search(r"ultra_options\s*=\s*[\w\.]*config[\w\.]*\.get\(\s*[\"']ultra_card_json[\"']\s*\)", src), rel
        assert "options_for_render" in src, rel


def test_every_render_call_in_the_cogs_uses_the_gate():
    """Each ultra_options= argument in these files is the gated value (or the join's own already-gated variable)."""
    for rel in SITE_FILES:
        for line in (ROOT / rel).read_text(encoding="utf-8").splitlines():
            if "ultra_options=" in line and "def " not in line:
                assert "options_for_render" in line or "ultra_opts" in line, (rel, line.strip())


# ---------- behaviour ----------

class _Resp:
    async def read(self):
        return b"avatar-bytes"

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        pass


class _Sess:
    def get(self, *a, **k):
        return _Resp()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        pass


async def _no_sticker(session, url):
    return None


async def _no_bg(session, cfg, client):
    return b"custom-bg"


def _asset():
    return SimpleNamespace(key="k1", replace=lambda size: SimpleNamespace(url="https://x/a.png"))


def tv(monkeypatch, premium, ultra_json):
    seen = {}

    def fake_render(avatar, name, line, **kw):
        seen.update(kw)
        return png(), "PNG"

    monkeypatch.setattr(vw, "render_welcome_card", fake_render)
    monkeypatch.setattr(vw.aiohttp, "ClientSession", lambda *a, **k: _Sess())
    monkeypatch.setattr(vw, "_fetch_sticker_bytes", _no_sticker)
    monkeypatch.setattr(welcome, "_custom_bg_bytes_for_render", _no_bg)
    monkeypatch.setattr(ws, "studio_unlocked", AsyncMock(return_value=premium))
    vw._TV_AVATARS.clear()
    user = SimpleNamespace(id=9, display_name="Ann", display_avatar=_asset())
    cfg = {"guild_id": 5, "clone_id": None, "use_template": True, "background_color": "#111111", "accent_color": "#222222",
           "ultra_card_json": ultra_json}
    run(vw._render_setup_tv(None, SimpleNamespace(name="G", member_count=3), user, cfg))
    return seen["ultra_options"]


def test_setup_wizard_tv_strips_studio_options_without_premium(monkeypatch):
    from modules import welcome_card as wc
    p = wc.parse_ultra_options(tv(monkeypatch, False, STUDIO))
    assert p["text_dx"] == 0 and p["text_dy"] == 0 and p["frame"] == "none" and p["zoom"] == 100 and p["pan_x"] == 0
    assert p["font"] == "tall"                                        # non-studio options are kept


def test_setup_wizard_tv_keeps_studio_options_with_premium(monkeypatch):
    assert tv(monkeypatch, True, STUDIO) == STUDIO


def test_untouched_options_are_passed_through_unchanged_without_any_premium_lookup(monkeypatch):
    got = tv(monkeypatch, False, json.dumps({"font": "tall"}))
    assert got == json.dumps({"font": "tall"})
    ws.studio_unlocked.assert_not_awaited()


def test_test_post_strips_studio_options_without_premium(monkeypatch):
    from modules import welcome_card as wc
    seen = {}

    def fake_render(avatar, name, line, **kw):
        seen.update(kw)
        return png(), "PNG"

    monkeypatch.setattr(we, "render_welcome_card", fake_render)
    monkeypatch.setattr(we.aiohttp, "ClientSession", lambda *a, **k: _Sess())
    monkeypatch.setattr(welcome, "_fetch_sticker_bytes", _no_sticker)
    monkeypatch.setattr(welcome, "_custom_bg_bytes_for_render", _no_bg)
    monkeypatch.setattr(welcome, "_apply_template", lambda template, member: "Hi")
    monkeypatch.setattr(we, "cooldown_left", lambda g, c: 0)
    monkeypatch.setattr(we, "_mark_test", lambda g, c: None)
    monkeypatch.setattr(ws, "studio_unlocked", AsyncMock(return_value=False))
    cfg = {"guild_id": 5, "clone_id": None, "delivery_mode": "dm", "background_color": "#111111", "accent_color": "#222222",
           "message_template": "Hi {member}", "ultra_card_json": STUDIO}
    monkeypatch.setattr(we.db, "get_welcome_config", AsyncMock(return_value=cfg))
    guild = SimpleNamespace(id=5, name="G", member_count=3)
    member = SimpleNamespace(display_name="Ann", display_avatar=_asset(), mention="<@1>", id=1, send=AsyncMock())
    ok, msg = run(we.send_test_welcome(SimpleNamespace(clone_id=None), guild, member))
    assert ok, msg
    p = wc.parse_ultra_options(seen["ultra_options"])
    assert p["text_dx"] == 0 and p["frame"] == "none" and p["zoom"] == 100 and p["font"] == "tall"
