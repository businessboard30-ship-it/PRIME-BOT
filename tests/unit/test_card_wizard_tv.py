"""Customize Card wizard: TV preview on top, three tabs, redraw after every change, NO preview limit."""
import asyncio
import io
import re
from types import SimpleNamespace

import discord
import pytest
from PIL import Image

from discord_bot.cogs import _views_card_customize as cc


def ids(view):
    return [c.custom_id for c in view.walk_children() if getattr(c, "custom_id", None)]


def kinds(view):
    out = set()
    for cid in ids(view):
        m = re.match(r"cardwz_([a-z]+):", cid)
        out.add(m.group(1))
    return out


def build(tab="bg", tv=False, unlocked=False):
    return cc.build_customize_view(1, None, 5, {"ultra_pack_unlocked": unlocked}, tab=tab, has_tv=tv, note="Sample backdrop")


def test_each_tab_shows_only_its_own_controls():
    common = {"tabbg", "tablayout", "tabtext", "reset", "unlock"}
    assert kinds(build("bg")) == common | {"setbg", "dim"}
    assert kinds(build("layout")) == common | {"banner", "side", "shape"}
    assert kinds(build("text")) == common | {"color", "heading", "number"}


def test_unlocked_server_gets_done_not_unlock_and_clear_only_with_a_background():
    v = cc.build_customize_view(1, None, 5, {"ultra_pack_unlocked": True, "custom_background_url": "https://x/y.png"}, tab="bg")
    k = kinds(v)
    assert "done" in k and "unlock" not in k and "clearbg" in k
    assert "clearbg" not in kinds(cc.build_customize_view(1, None, 5, {"ultra_pack_unlocked": True}, tab="bg"))


def test_there_is_no_preview_button_in_the_new_wizard():
    for t in cc._TABS:
        assert "preview" not in kinds(build(t))


def test_the_tv_is_a_media_gallery_pointing_at_the_attachment():
    with_tv = [c for c in build("bg", tv=True).walk_children() if isinstance(c, discord.ui.MediaGallery)]
    assert len(with_tv) == 1 and with_tv[0].items[0].media.url == "attachment://card.png"
    assert not [c for c in build("bg", tv=False).walk_children() if isinstance(c, discord.ui.MediaGallery)]


def test_the_active_tab_is_highlighted_and_the_wizard_stays_short():
    for t in cc._TABS:
        v = build(t, tv=True)
        tabs = {c.custom_id.split(":")[0]: c.item.style for c in v.walk_children() if getattr(c, "custom_id", "").startswith("cardwz_tab")}
        assert tabs["cardwz_tab" + t] == discord.ButtonStyle.primary
        assert sum(1 for s in tabs.values() if s == discord.ButtonStyle.primary) == 1
        assert sum(isinstance(c, discord.ui.Select) for c in v.walk_children()) <= 3
        assert len(list(v.walk_children())) < 25


def test_tab_button_custom_id_round_trips_for_main_and_clone():
    for clone, inv in ((None, 5), (7, None)):
        b = cc.CardTabButton("layout", 123, clone, inv)
        m = re.match(cc.CardTabButton.__discord_ui_compiled_template__.pattern, b.item.custom_id)
        assert m
        again = asyncio.run(cc.CardTabButton.from_custom_id(None, None, m))
        assert (again.tab, again.guild_id, again.clone_id, again.invoker_id) == ("layout", 123, clone, inv)


def test_tab_state_defaults_to_background_and_expires():
    key = (1, None, 42)
    assert cc._get_tab(key) == "bg"
    cc._set_tab(key, "text")
    assert cc._get_tab(key) == "text"
    cc._set_tab(key, "bogus")
    assert cc._get_tab(key) == "bg"
    cc._TAB_STATE[key] = ("text", __import__("time").monotonic() - 10_000)
    assert cc._get_tab(key) == "bg"


def test_no_preview_cooldown_exists_anymore():
    assert not hasattr(cc, "_PREVIEW_COOLDOWN") and not hasattr(cc, "_LAST_PREVIEW")


def test_all_dynamic_items_are_registered_including_tabs():
    assert cc.CardTabButton in cc.DYNAMIC_ITEMS and cc.CardPreviewButton in cc.DYNAMIC_ITEMS


# ---------- redraw behaviour (fake Discord interaction) ----------

class FakeInteraction:
    def __init__(self, uid=42):
        self.user = SimpleNamespace(id=uid)
        self.guild = SimpleNamespace(name="G", member_count=3)
        self.client = SimpleNamespace(get_guild=lambda gid: self.guild)
        self.edits = []

    async def edit_original_response(self, **kw):
        self.edits.append(kw)


@pytest.fixture
def redraw(monkeypatch):
    state = {"renders": 0, "fail": False}

    async def cfg(gid, clone_id=None):
        return {"ultra_pack_unlocked": False}

    async def render(client, guild, gid, clone_id, user):
        state["renders"] += 1
        await asyncio.sleep(0.01)
        if state["fail"]:
            raise RuntimeError("boom")
        return b"PNG", "Sample backdrop"
    monkeypatch.setattr(cc.db, "get_welcome_config", cfg)
    monkeypatch.setattr(cc, "_render_tv", render)
    monkeypatch.setattr(cc, "_REDRAW_DELAY", 0.05)
    monkeypatch.setattr(cc, "_refresh_public_wizard", lambda *a, **k: None)
    cc._GEN.clear(); cc._LOCKS.clear(); cc._TAB_STATE.clear()
    return state


def test_quick_taps_collapse_into_one_redraw(redraw):
    async def go():
        it = FakeInteraction()
        await asyncio.gather(*[cc._rerender(it, 1, None, 5) for _ in range(5)])
        return it
    it = asyncio.run(go())
    assert redraw["renders"] == 1 and len(it.edits) == 1
    assert it.edits[0]["attachments"][0].filename == "card.png"
    assert isinstance(it.edits[0]["view"], discord.ui.LayoutView)


def test_separate_taps_each_redraw_with_no_cooldown(redraw):
    async def go():
        it = FakeInteraction()
        for _ in range(6):                       # six edits back to back, far faster than the old 8 s cooldown
            await cc._rerender(it, 1, None, 5)
        return it
    it = asyncio.run(go())
    assert redraw["renders"] == 6 and len(it.edits) == 6


def test_different_editors_do_not_batch_each_other(redraw):
    async def go():
        a, b = FakeInteraction(1), FakeInteraction(2)
        await asyncio.gather(cc._rerender(a, 1, None, 5), cc._rerender(b, 1, None, 5))
        return a, b
    a, b = asyncio.run(go())
    assert redraw["renders"] == 2 and len(a.edits) == 1 and len(b.edits) == 1


def test_render_failure_still_updates_the_controls_without_a_tv(redraw):
    redraw["fail"] = True
    it = FakeInteraction()
    asyncio.run(cc._rerender(it, 1, None, 5))
    assert len(it.edits) == 1 and it.edits[0]["attachments"] == []
    assert not [c for c in it.edits[0]["view"].walk_children() if isinstance(c, discord.ui.MediaGallery)]


def test_redraw_keeps_the_current_tab(redraw):
    cc._set_tab((1, None, 42), "layout")
    it = FakeInteraction()
    asyncio.run(cc._rerender(it, 1, None, 5))
    assert "banner" in kinds(it.edits[0]["view"]) and "dim" not in kinds(it.edits[0]["view"])


# ---------- the real renderer and caches ----------

def png(w=1400, h=500, color=(30, 60, 120)):
    b = io.BytesIO()
    Image.new("RGB", (w, h), color).save(b, format="PNG")
    return b.getvalue()


def test_tv_finish_downsizes_and_only_watermarks_unpaid():
    big = png(1400, 500)
    unpaid = cc._tv_finish(big, True)
    paid = cc._tv_finish(big, False)
    assert Image.open(io.BytesIO(paid)).width == cc._TV_WIDTH and Image.open(io.BytesIO(unpaid)).width == cc._TV_WIDTH
    assert paid != unpaid
    small = cc._tv_finish(png(400, 150), False)
    assert Image.open(io.BytesIO(small)).width == 400          # never upscaled


def test_real_renderer_draws_the_tv_for_an_unpaid_server(monkeypatch):
    async def cfg(gid, clone_id=None):
        return {"ultra_pack_unlocked": False, "avatar_shape": "circle"}

    async def avatar(session, user):
        return png(256, 256, (200, 50, 50))
    monkeypatch.setattr(cc.db, "get_welcome_config", cfg)
    monkeypatch.setattr(cc, "_avatar_bytes", avatar)
    user = SimpleNamespace(id=9, display_name="Tester")
    guild = SimpleNamespace(name="Test Server", member_count=12)
    data, note = asyncio.run(cc._render_tv(None, guild, 1, None, user))
    img = Image.open(io.BytesIO(data))
    assert img.format == "PNG" and 100 < img.width <= cc._TV_WIDTH and "Sample backdrop" in note


def test_render_without_a_guild_raises_cleanly():
    with pytest.raises(RuntimeError):
        asyncio.run(cc._render_tv(None, None, 1, None, SimpleNamespace(id=1, display_name="x")))


def test_avatar_is_downloaded_once_per_avatar():
    cc._AVATAR_CACHE.clear()
    calls = []

    class Resp:
        async def read(self):
            return b"AV"
        async def __aenter__(self):
            calls.append(1); return self
        async def __aexit__(self, *a):
            return False

    class Session:
        def get(self, url, timeout=None):
            return Resp()
    asset = SimpleNamespace(key="abc", replace=lambda size: SimpleNamespace(url="https://cdn/x.png"))
    user = SimpleNamespace(id=3, display_avatar=asset)
    assert asyncio.run(cc._avatar_bytes(Session(), user)) == b"AV" and asyncio.run(cc._avatar_bytes(Session(), user)) == b"AV"
    assert len(calls) == 1
