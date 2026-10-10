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
    common = {"tabbg", "tablayout", "tabtext", "reset", "unlock", "preset"}
    assert kinds(build("bg")) == common | {"setbg", "dim", "focus"}
    assert kinds(build("layout")) == common | {"layout", "banner", "side", "shape"}
    assert kinds(build("text")) == common | {"color", "font", "style", "heading", "number"}


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
        assert sum(isinstance(getattr(c, 'item', c), discord.ui.Select) for c in v.walk_children()) <= 5
        assert len(list(v.walk_children())) < 25
        assert sum(isinstance(c, discord.ui.Select) for c in v.walk_children()) <= 5


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
    cc._GEN.clear(); cc._LOCKS.clear(); cc._TAB_STATE.clear(); cc._TV_STATE.clear(); cc._TV_NOTES.clear()
    return state


def test_quick_taps_collapse_into_one_redraw(redraw):
    async def go():
        it = FakeInteraction()
        await asyncio.gather(*[cc._rerender(it, 1, None, 5) for _ in range(5)])
        return it
    it = asyncio.run(go())
    assert redraw["renders"] == 1 and len(it.edits) == 1
    name = it.edits[0]["attachments"][0].filename
    assert name.startswith("card") and name.endswith(".png")
    gallery = [c for c in it.edits[0]["view"].walk_children() if isinstance(c, discord.ui.MediaGallery)]
    assert gallery[0].items[0].media.url == f"attachment://{name}"            # the view points at the file sent in the same edit
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


def _count(n):
    return 1 + sum(_count(c) for c in (n.get("components") or [])) if isinstance(n, dict) else 0


def test_every_tab_fits_discords_component_limit_with_all_the_new_controls():
    for tab in cc._TABS:
        for cfg in ({"ultra_pack_unlocked": False}, {"ultra_pack_unlocked": True, "custom_background_url": "https://x/y.png",
                                                      "ultra_trial_active": True}):
            v = cc.build_customize_view(1, 7, 5, cfg, tab=tab, has_tv=True, note="x")
            assert sum(_count(c) for c in v.to_components()) < 40, tab


def test_trial_shows_the_banner_and_keeps_unlock_next_to_done():
    from datetime import datetime, timezone
    cfg = {"ultra_pack_unlocked": True, "ultra_trial_active": True, "ultra_trial_ends_at": datetime(2030, 1, 1, tzinfo=timezone.utc)}
    v = cc.build_customize_view(1, None, 5, cfg, tab="bg")
    text = " ".join(getattr(c, "content", "") for c in v.walk_children())
    assert "free 5-day trial" in text.lower() and "<t:" in text
    k = kinds(v)
    assert "done" in k and "unlock" in k
    plain = cc.build_customize_view(1, None, 5, {"ultra_pack_unlocked": True}, tab="bg")
    assert "unlock" not in kinds(plain) and "trial" not in " ".join(getattr(c, "content", "") for c in plain.walk_children()).lower()


def test_opening_the_wizard_starts_the_trial_once_and_shows_the_tv(monkeypatch):
    sent = []
    state = {"cfg": {"ultra_pack_unlocked": False}, "started": []}

    async def get(gid, clone_id=None):
        return dict(state["cfg"])

    async def start(gid, uid, clone_id=None):
        state["started"].append(gid)
        state["cfg"] = {"ultra_pack_unlocked": True, "ultra_trial_active": True}
        return True

    async def render(*a, **k):
        return b"PNG", "Sample backdrop"

    class FU:
        async def send(self, *a, **kw):
            sent.append((a, kw))
    monkeypatch.setattr(cc.db, "get_welcome_config", get)
    monkeypatch.setattr(cc.db, "start_ultra_trial", start)
    monkeypatch.setattr(cc, "_render_tv", render)
    it = SimpleNamespace(user=SimpleNamespace(id=42), guild=SimpleNamespace(), client=SimpleNamespace(get_guild=lambda g: None), followup=FU())
    asyncio.run(cc.open_customize_wizard(it, 1, None))
    assert state["started"] == [1]
    assert "free 5-day trial" in sent[0][0][0].lower() and sent[0][1]["ephemeral"] is True       # trial note first
    assert isinstance(sent[1][1]["view"], discord.ui.LayoutView) and sent[1][1]["file"].filename.startswith("card") and sent[1][1]["file"].filename.endswith(".png")


# ---------- the picture must not vanish ----------

def _gallery(view):
    return [c for c in view.walk_children() if isinstance(c, discord.ui.MediaGallery)]


def test_every_redraw_uses_a_new_file_name_and_the_view_points_at_it(redraw):
    it = FakeInteraction()
    for _ in range(3):
        asyncio.run(cc._rerender(it, 1, None, 5))
    names = [e["attachments"][0].filename for e in it.edits]
    assert len(set(names)) == 3                                         # never two uploads called card.png
    for e, n in zip(it.edits, names):
        assert _gallery(e["view"])[0].items[0].media.url == f"attachment://{n}"
    assert cc._TV_STATE[(1, None, 42)] == names[-1]


def test_a_failed_redraw_keeps_the_picture_that_is_already_there(redraw):
    asyncio.run(cc._rerender(FakeInteraction(), 1, None, 5))              # a good draw first
    shown = cc._TV_STATE[(1, None, 42)]
    redraw["fail"] = True
    it = FakeInteraction()
    asyncio.run(cc._rerender(it, 1, None, 5))
    e = it.edits[0]
    assert "attachments" not in e                                       # Discord keeps the old file
    assert _gallery(e["view"])[0].items[0].media.url == f"attachment://{shown}"
    assert cc._TV_STATE[(1, None, 42)] == shown and "Couldn't refresh" in (cc._TV_NOTES[(1, None, 42)] or "")


def test_open_remembers_the_picture_name_so_a_tab_switch_keeps_it(monkeypatch):
    sent = []

    async def get(gid, clone_id=None):
        return {"ultra_pack_unlocked": True}

    async def render(*a, **k):
        return b"PNG", "Sample backdrop"

    class FU:
        async def send(self, *a, **kw):
            sent.append(kw)
    monkeypatch.setattr(cc.db, "get_welcome_config", get)
    monkeypatch.setattr(cc, "_render_tv", render)
    cc._TV_STATE.clear()
    it = SimpleNamespace(user=SimpleNamespace(id=9), guild=SimpleNamespace(), client=SimpleNamespace(get_guild=lambda g: None), followup=FU())
    asyncio.run(cc.open_customize_wizard(it, 1, None))
    name = sent[0]["file"].filename
    assert cc._TV_STATE[(1, None, 9)] == name and _gallery(sent[0]["view"])[0].items[0].media.url == f"attachment://{name}"


def test_tab_switch_uses_remembered_state_not_the_message_attachments(monkeypatch):
    async def ok(*a, **k):
        return True

    async def get(gid, clone_id=None):
        return {"ultra_pack_unlocked": True}
    monkeypatch.setattr(cc, "_check_access", ok)
    monkeypatch.setattr(cc.db, "get_welcome_config", get)
    cc._TV_STATE.clear()
    cc._TV_STATE[(1, None, 42)] = "card-abc123.png"
    edits = []

    class Resp:
        async def defer(self, *a, **k):
            pass
    it = SimpleNamespace(user=SimpleNamespace(id=42), response=Resp(), message=SimpleNamespace(attachments=[]))

    async def edit(**kw):
        edits.append(kw)
    it.edit_original_response = edit
    asyncio.run(cc.CardTabButton("layout", 1, None, 5).callback(it))
    assert _gallery(edits[0]["view"])[0].items[0].media.url == "attachment://card-abc123.png"
    assert "attachments" not in edits[0]


def test_tab_switch_falls_back_to_the_message_after_a_restart(monkeypatch):
    async def ok(*a, **k):
        return True

    async def get(gid, clone_id=None):
        return {"ultra_pack_unlocked": True}
    monkeypatch.setattr(cc, "_check_access", ok)
    monkeypatch.setattr(cc.db, "get_welcome_config", get)
    cc._TV_STATE.clear()
    edits = []

    class Resp:
        async def defer(self, *a, **k):
            pass
    it = SimpleNamespace(user=SimpleNamespace(id=42), response=Resp(),
                         message=SimpleNamespace(attachments=[SimpleNamespace(filename="card-zzz999.png")]))

    async def edit(**kw):
        edits.append(kw)
    it.edit_original_response = edit
    asyncio.run(cc.CardTabButton("text", 1, None, 5).callback(it))
    assert _gallery(edits[0]["view"])[0].items[0].media.url == "attachment://card-zzz999.png"


def test_the_background_button_is_called_upload_image():
    assert cc.CardBackgroundButton.LABEL.endswith("Upload image") and "Set background" not in cc.CardBackgroundButton.LABEL
    labels = [getattr(getattr(c, "item", c), "label", "") for c in build("bg").walk_children()]
    assert any(l and l.endswith("Upload image") for l in labels)
    assert cc.CardBackgroundButton.FIELD == "setbg"                       # id unchanged so open wizard messages keep working
