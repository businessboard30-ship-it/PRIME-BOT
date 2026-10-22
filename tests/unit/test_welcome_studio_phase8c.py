"""Welcome Card Studio Phase 8c: per-picture tweaks (own crop and text color)."""
import asyncio
import json
import re
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from discord_bot.cogs import _views_card_customize as cc
from modules import welcome_gallery as wg


def run(c):
    return asyncio.run(c)


def comps(v):
    return list(v.walk_children())


def kinds(v):
    return {re.match(r"cardwz_([a-z]+):", c.custom_id).group(1) for c in comps(v) if getattr(c, "custom_id", None)}


def test_clean_override_keeps_only_known_values():
    assert wg.clean_override({"focus": "top", "text_color": "gold", "zoom": 200}) == {"focus": "top", "text_color": "gold"}
    assert wg.clean_override('{"focus": "left"}') == {"focus": "left"}
    assert wg.clean_override({"focus": "nope", "text_color": "pink!"}) == {}
    assert wg.clean_override({"text_color": "avatar"}) == {"text_color": "avatar"}
    for junk in (None, "", "not json", 5, [1], b"\xff"):
        assert wg.clean_override(junk) == {}


def test_override_json_none_when_empty():
    assert wg.override_json({}) is None and wg.override_json({"focus": "x"}) is None
    assert json.loads(wg.override_json({"focus": "top"})) == {"focus": "top"}


def test_join_pick_returns_the_picture_tweak(monkeypatch):
    import discord_bot.cogs.welcome as wl
    import modules.welcome_studio as ws
    monkeypatch.setattr(ws, "studio_unlocked", AsyncMock(return_value=True))
    fake = MagicMock()
    fake.gallery_list = AsyncMock(return_value=[{"slot": 1, "message_id": 5, "options_override_json": '{"text_color": "gold", "x": 1}'}])
    fake.gallery_next_counter = AsyncMock(return_value=0)
    fake.gallery_set_last_slot = AsyncMock()
    monkeypatch.setattr(wl, "db", fake)
    monkeypatch.setattr(wl, "_gallery_slot_url", AsyncMock(return_value=("https://c/a.png", False)))
    monkeypatch.setattr(wl, "_fetch_custom_bg_bytes", AsyncMock(return_value=(b"PIC", None)))
    cfg = {"guild_id": 1, "clone_id": None, "rotation_mode": "pin"}
    assert run(wl._gallery_pick_for_render(MagicMock(), cfg, MagicMock(), 3)) == (b"PIC", {"text_color": "gold"})
    assert run(wl._gallery_bg_bytes_for_render(MagicMock(), cfg, MagicMock(), 3)) == b"PIC"


def test_locked_server_gets_no_tweak(monkeypatch):
    import discord_bot.cogs.welcome as wl
    import modules.welcome_studio as ws
    monkeypatch.setattr(ws, "studio_unlocked", AsyncMock(return_value=False))
    monkeypatch.setattr(wl, "_custom_bg_bytes_for_render", AsyncMock(return_value=b"OLD"))
    assert run(wl._gallery_pick_for_render(MagicMock(), {"guild_id": 1}, MagicMock(), 1)) == (b"OLD", {})


def test_error_gives_no_tweak(monkeypatch):
    import discord_bot.cogs.welcome as wl
    import modules.welcome_studio as ws
    monkeypatch.setattr(ws, "studio_unlocked", AsyncMock(side_effect=RuntimeError("x")))
    monkeypatch.setattr(wl, "_custom_bg_bytes_for_render", AsyncMock(return_value=b"OLD"))
    assert run(wl._gallery_pick_for_render(MagicMock(), {"guild_id": 1}, MagicMock(), 1)) == (b"OLD", {})


def test_preview_pick_returns_tweak(monkeypatch):
    import discord_bot.cogs.welcome as wl
    import modules.welcome_studio as ws
    cc._BG_CACHE.clear()
    monkeypatch.setattr(ws, "studio_unlocked", AsyncMock(return_value=True))
    fake = MagicMock()
    fake.gallery_list = AsyncMock(return_value=[{"slot": 2, "message_id": 9, "options_override_json": '{"focus": "top"}'}])
    monkeypatch.setattr(cc, "db", fake)
    monkeypatch.setattr(wl, "_gallery_slot_url", AsyncMock(return_value=("https://c/a.png", False)))
    monkeypatch.setattr(wl, "_fetch_custom_bg_bytes", AsyncMock(return_value=(b"P", None)))
    assert run(cc._gallery_preview_pick(MagicMock(), MagicMock(), 1, None, slot=2)) == (b"P", {"focus": "top"})
    assert run(cc._gallery_preview_pick(MagicMock(), MagicMock(), 1, None, slot=2)) == (b"P", {"focus": "top"})   # cached picture keeps its tweak
    cc._BG_CACHE.clear()


def build(studio, more=True):
    cfg = {"ultra_pack_unlocked": True, "custom_background_url": "https://x/y.png", "_studio": studio}
    return cc.build_customize_view(1, None, 42, cfg, tab="bg", more=more)


def test_button_visibility_limits_and_registration():
    full = {"unlocked": True, "pictures": 3, "needs": [], "slots": [1, 2, 3], "mode": "random"}
    free = {"unlocked": False, "mode": "random"}
    empty = {"unlocked": True, "pictures": 0, "needs": [], "slots": [], "mode": "random"}
    assert "pictweak" in kinds(build(full)) and "pictweak" in kinds(build(free))
    assert "pictweak" not in kinds(build(empty)) and "pictweak" not in kinds(build(full, more=False))
    for st in (full, free):
        v = build(st)
        assert len(comps(v)) <= 40 and sum(isinstance(c, discord.ui.Select) for c in comps(v)) <= 5
        assert all(len(r.children) <= 5 for r in comps(v) if isinstance(r, discord.ui.ActionRow))
    assert cc.CardPictureTweakButton in cc.DYNAMIC_ITEMS
    b = cc.CardPictureTweakButton(123, None, 5)
    m = re.match(cc.CardPictureTweakButton.__discord_ui_compiled_template__.pattern, b.item.custom_id)
    assert run(cc.CardPictureTweakButton.from_custom_id(None, None, m)).guild_id == 123


class FakeResp:
    async def defer(self, **k): pass


class FakeFollow:
    def __init__(self): self.sent = []
    async def send(self, msg, **k): self.sent.append(msg)


class FakeInter:
    def __init__(self): self.response, self.followup = FakeResp(), FakeFollow()


def submit(monkeypatch, unlocked, number, focus, color, done=True):
    import modules.welcome_studio as ws
    monkeypatch.setattr(ws, "studio_unlocked", AsyncMock(return_value=unlocked))
    fake = MagicMock()
    fake.gallery_set_override = AsyncMock(return_value=done)
    monkeypatch.setattr(cc, "db", fake)
    rer = []

    async def rr(*a, **k): rer.append(1)

    monkeypatch.setattr(cc, "_rerender", rr)
    m = cc.CardPictureTweakModal(1, None, 5)
    for obj, attr, val in ((m.number, "value", number), (m.focus, "values", [focus]), (m.color, "values", [color])):
        obj.__class__ = type("T", (type(obj),), {attr: val})
    i = FakeInter()
    run(m.on_submit(i))
    return i, fake, rer


def test_premium_saves_the_tweak_and_shows_that_picture(monkeypatch):
    cc._PREVIEW_AS.clear()
    i, fake, rer = submit(monkeypatch, True, "2", "top", "gold")
    fake.gallery_set_override.assert_awaited_once_with(1, None, 2, json.dumps({"focus": "top", "text_color": "gold"}, sort_keys=True))
    assert rer == [1] and cc._PREVIEW_AS[(1, None, 5)]["slot"] == 2 and "Picture 2 now has" in i.followup.sent[0]
    cc._PREVIEW_AS.clear()


def test_both_keep_removes_the_tweak(monkeypatch):
    i, fake, rer = submit(monkeypatch, True, "1", "keep", "keep")
    assert fake.gallery_set_override.await_args.args[3] is None and "follows the card" in i.followup.sent[0]
    cc._PREVIEW_AS.clear()


def test_free_server_stores_nothing(monkeypatch):
    i, fake, rer = submit(monkeypatch, False, "1", "top", "gold")
    assert not fake.gallery_set_override.called and rer == [] and "Preview only" in i.followup.sent[0]


def test_bad_number_and_missing_picture(monkeypatch):
    for bad in ("", "0", "11", "x"):
        i, fake, rer = submit(monkeypatch, True, bad, "top", "keep")
        assert not fake.gallery_set_override.called and "1 to 10" in i.followup.sent[0]
    i, fake, rer = submit(monkeypatch, True, "4", "top", "keep", done=False)
    assert "no picture 4" in i.followup.sent[0] and rer == []
