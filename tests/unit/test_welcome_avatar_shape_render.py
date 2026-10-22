"""Fix: the avatar shape picked in the wizard is the shape that is drawn at every welcome render site.

Before this, `welcome.py` (real join, /welcome preview, nudge/heads-up post, refresh) and the test post never passed
`avatar_shape` to the renderer, so a server that chose e.g. a rounded square still got a circle (only the setup-wizard
TV honoured it, so the preview and the real card disagreed). Untouched servers (circle) are unchanged."""
import asyncio
import io
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from PIL import Image

from discord_bot.cogs import _views_welcome as vw
from discord_bot.cogs import welcome, welcome_extras as we
from modules import welcome_studio as ws
from modules.welcome_card import AVATAR_SHAPES, DEFAULT_AVATAR_SHAPE

ROOT = Path(__file__).resolve().parents[2]
SITE_FILES = ("discord_bot/cogs/welcome.py", "discord_bot/cogs/_views_welcome.py", "discord_bot/cogs/welcome_extras.py")


def run(c):
    return asyncio.run(c)


def png():
    b = io.BytesIO()
    Image.new("RGB", (1400, 500), (10, 20, 30)).save(b, format="PNG")
    return b.getvalue()


# ---------- the helper ----------

def test_helper_returns_the_stored_shape_and_falls_back_to_circle():
    other = next(k for k in AVATAR_SHAPES if k != DEFAULT_AVATAR_SHAPE)
    assert run(ws.avatar_shape_for_render({"avatar_shape": other})) == other
    for junk in ({}, None, {"avatar_shape": None}, {"avatar_shape": "nope"}, {"avatar_shape": 5}):
        assert run(ws.avatar_shape_for_render(junk)) == DEFAULT_AVATAR_SHAPE


# ---------- source guard: a render call that takes the studio options also takes the shape ----------

def test_every_card_render_call_that_takes_ultra_options_also_takes_the_shape():
    calls = 0
    for rel in SITE_FILES:
        lines = (ROOT / rel).read_text(encoding="utf-8").splitlines()
        for i, line in enumerate(lines):
            if re.search(r"\brender_welcome_card,?\s*$", line.strip()) or line.strip() == "render_welcome_card,":
                block = "\n".join(lines[i:i + 16])
                block = block.split("\n)")[0] if "\n)" in block else block
                if "ultra_options=" in block:
                    calls += 1
                    assert "avatar_shape=" in block, (rel, i + 1)
    assert calls >= 7          # join, preview, refresh, nudge, test post, setup TV, setup Preview button


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


def _other_shape():
    return next(k for k in AVATAR_SHAPES if k != DEFAULT_AVATAR_SHAPE)


def test_setup_tv_uses_the_stored_shape(monkeypatch):
    seen = {}

    def fake_render(avatar, name, line, **kw):
        seen.update(kw)
        return png(), "PNG"

    monkeypatch.setattr(vw, "render_welcome_card", fake_render)
    monkeypatch.setattr(vw.aiohttp, "ClientSession", lambda *a, **k: _Sess())
    monkeypatch.setattr(vw, "_fetch_sticker_bytes", _no_sticker)
    monkeypatch.setattr(welcome, "_custom_bg_bytes_for_render", _no_bg)
    vw._TV_AVATARS.clear()
    user = SimpleNamespace(id=9, display_name="Ann", display_avatar=_asset())
    shape = _other_shape()
    cfg = {"guild_id": 5, "clone_id": None, "use_template": True, "background_color": "#111111", "accent_color": "#222222",
           "avatar_shape": shape}
    run(vw._render_setup_tv(None, SimpleNamespace(name="G", member_count=3), user, cfg))
    assert seen["avatar_shape"] == shape


def test_test_post_now_draws_the_stored_shape(monkeypatch):
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
    shape = _other_shape()
    cfg = {"guild_id": 5, "clone_id": None, "delivery_mode": "dm", "background_color": "#111111", "accent_color": "#222222",
           "message_template": "Hi {member}", "avatar_shape": shape}
    monkeypatch.setattr(we.db, "get_welcome_config", AsyncMock(return_value=cfg))
    guild = SimpleNamespace(id=5, name="G", member_count=3)
    member = SimpleNamespace(display_name="Ann", display_avatar=_asset(), mention="<@1>", id=1, send=AsyncMock())
    ok, msg = run(we.send_test_welcome(SimpleNamespace(clone_id=None), guild, member))
    assert ok, msg
    assert seen["avatar_shape"] == shape


def test_untouched_server_still_draws_a_circle(monkeypatch):
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
    cfg = {"guild_id": 5, "clone_id": None, "delivery_mode": "dm", "background_color": "#111111", "accent_color": "#222222",
           "message_template": "Hi {member}"}
    monkeypatch.setattr(we.db, "get_welcome_config", AsyncMock(return_value=cfg))
    guild = SimpleNamespace(id=5, name="G", member_count=3)
    member = SimpleNamespace(display_name="Ann", display_avatar=_asset(), mention="<@1>", id=1, send=AsyncMock())
    ok, _ = run(we.send_test_welcome(SimpleNamespace(clone_id=None), guild, member))
    assert ok and seen["avatar_shape"] == DEFAULT_AVATAR_SHAPE
