"""Welcome Card Studio Phase 8b: preview as another picture / another member (editor only, nothing stored)."""
import asyncio
import re
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from discord_bot.cogs import _views_card_customize as cc


def run(c):
    return asyncio.run(c)


@pytest.fixture(autouse=True)
def _clean():
    cc._PREVIEW_AS.clear()
    cc._BG_CACHE.clear()
    yield
    cc._PREVIEW_AS.clear()
    cc._BG_CACHE.clear()


def comps(v):
    return list(v.walk_children())


def kinds(v):
    return {re.match(r"cardwz_([a-z]+):", c.custom_id).group(1) for c in comps(v) if getattr(c, "custom_id", None)}


def build(studio, more=True, tab="bg"):
    cfg = {"ultra_pack_unlocked": True, "custom_background_url": "https://x/y.png"}
    if studio is not None:
        cfg["_studio"] = studio
    return cc.build_customize_view(1, None, 42, cfg, tab=tab, more=more)


UNLOCKED = {"unlocked": True, "pictures": 3, "needs": [], "slots": [1, 2, 3], "mode": "random"}


# ---------- wiring ----------

def test_button_only_on_pictures_step_in_more_mode_and_for_every_server():
    assert "previewas" in kinds(build(UNLOCKED))
    assert "previewas" in kinds(build({"unlocked": False, "mode": "random"}))
    assert "previewas" not in kinds(build(UNLOCKED, more=False))
    for tab in ("layout", "text", "send"):
        assert "previewas" not in kinds(build(UNLOCKED, tab=tab))


def test_pictures_more_stays_inside_discord_limits():
    for studio in (UNLOCKED, {"unlocked": False, "mode": "random"}):
        v = build(studio)
        assert len(comps(v)) <= 40
        assert sum(isinstance(c, discord.ui.Select) for c in comps(v)) <= 5
        for row in (c for c in comps(v) if isinstance(c, discord.ui.ActionRow)):
            assert len(row.children) <= 5


def test_button_is_registered_and_id_round_trips():
    cls = cc.CardPreviewAsButton
    assert cls in cc.DYNAMIC_ITEMS
    for clone, inv in ((None, 5), (7, None)):
        b = cls(123, clone, inv)
        m = re.match(cls.__discord_ui_compiled_template__.pattern, b.item.custom_id)
        again = run(cls.from_custom_id(None, None, m))
        assert (again.guild_id, again.clone_id, again.invoker_id) == (123, clone, inv)


# ---------- the words ----------

def test_plain_words_line():
    assert cc._preview_as_line({}) == ""
    assert cc._preview_as_line({"slot": 3}) == "Previewing picture 3"
    assert cc._preview_as_line({"name": "Sam", "number": 1200}) == "Previewing as Sam #1200"
    assert cc._preview_as_line({"slot": 2, "number": 50}) == "Previewing picture 2, as you #50"


# ---------- modal ----------

class FakeResp:
    async def defer(self, **k): pass


class FakeFollow:
    def __init__(self): self.sent = []
    async def send(self, msg, **k): self.sent.append(msg)


class FakeInter:
    def __init__(self):
        self.response, self.followup = FakeResp(), FakeFollow()
        self.user = type("U", (), {"id": 5})()


def set_values(m, slot, name, number):
    for field, val in ((m.slot, slot), (m.name, name), (m.number, number)):
        field.__class__ = type("T", (type(field),), {"value": val})


def submit(monkeypatch, slot="", name="", number="", have=(1, 2, 3)):
    rer = []

    async def fake_rerender(*a, **k): rer.append(1)

    monkeypatch.setattr(cc, "_rerender", fake_rerender)
    fake = MagicMock()
    fake.gallery_list = AsyncMock(return_value=[{"slot": s} for s in have])
    monkeypatch.setattr(cc, "db", fake)
    m = cc.CardPreviewAsModal(1, None, 5)
    set_values(m, slot, name, number)
    i = FakeInter()
    run(m.on_submit(i))
    return i, rer


def test_modal_stores_picture_name_and_number_and_redraws(monkeypatch):
    i, rer = submit(monkeypatch, "2", "  Sam   Long  Name ", "1200")
    assert cc._PREVIEW_AS[(1, None, 5)] == {"slot": 2, "name": "Sam Long Name", "number": 1200}
    assert rer == [1] and i.followup.sent == []


def test_all_blank_goes_back_to_the_normal_preview(monkeypatch):
    cc._PREVIEW_AS[(1, None, 5)] = {"slot": 2, "name": None, "number": None}
    i, rer = submit(monkeypatch)
    assert (1, None, 5) not in cc._PREVIEW_AS and rer == [1]


def test_bad_numbers_are_refused_and_nothing_changes(monkeypatch):
    for slot, number in (("0", ""), ("11", ""), ("x", ""), ("", "0"), ("", "abc"), ("", "-4")):
        i, rer = submit(monkeypatch, slot, "", number)
        assert rer == [] and cc._PREVIEW_AS == {} and i.followup.sent, (slot, number)


def test_unknown_picture_is_said_plainly_but_name_and_number_still_apply(monkeypatch):
    i, rer = submit(monkeypatch, "9", "Sam", "5", have=(1, 2))
    assert "no picture 9" in i.followup.sent[0]
    assert cc._PREVIEW_AS[(1, None, 5)] == {"slot": None, "name": "Sam", "number": 5} and rer == [1]


def test_unknown_picture_alone_leaves_no_preview(monkeypatch):
    i, rer = submit(monkeypatch, "9", have=(1,))
    assert cc._PREVIEW_AS == {} and rer == [1]


# ---------- the preview itself ----------

def test_gallery_preview_can_pick_one_slot(monkeypatch):
    import discord_bot.cogs.welcome as wl
    import modules.welcome_studio as ws
    rows = [{"slot": 1, "message_id": 7}, {"slot": 2, "message_id": 8}]
    monkeypatch.setattr(ws, "studio_unlocked", AsyncMock(return_value=True))
    fake = MagicMock()
    fake.gallery_list = AsyncMock(return_value=rows)
    monkeypatch.setattr(cc, "db", fake)
    seen = []

    async def slot_url(client, row):
        seen.append(row["slot"])
        return f"https://cdn/{row['slot']}.png", False

    async def fetch(session, url):
        return url.encode(), None

    monkeypatch.setattr(wl, "_gallery_slot_url", slot_url)
    monkeypatch.setattr(wl, "_fetch_custom_bg_bytes", fetch)
    assert run(cc._gallery_preview_bytes(MagicMock(), MagicMock(), 1, None, slot=2)) == b"https://cdn/2.png"
    assert seen == [2]
    assert run(cc._gallery_preview_bytes(MagicMock(), MagicMock(), 1, None, slot=3)) is None   # not there: nothing else is tried
    cc._BG_CACHE.clear()
    assert run(cc._gallery_preview_bytes(MagicMock(), MagicMock(), 1, None)) == b"https://cdn/1.png"   # no slot: as before


def test_render_tv_uses_the_pretend_name_number_and_picture(monkeypatch):
    got = {}

    def fake_render(avatar, name, subtitle, **k):
        got.update(name=name, subtitle=subtitle, bg=k.get("custom_background_bytes"))
        return b"CARD", "PNG"

    fake_db = MagicMock()
    fake_db.get_welcome_config = AsyncMock(return_value={"ultra_pack_unlocked": True})
    monkeypatch.setattr(cc, "db", fake_db)
    monkeypatch.setattr(cc, "render_welcome_card", fake_render)
    monkeypatch.setattr(cc, "_avatar_bytes", AsyncMock(return_value=b"AV"))
    monkeypatch.setattr(cc, "_gallery_preview_pick", AsyncMock(return_value=(b"SLOT2", {})))
    monkeypatch.setattr(cc, "_tv_finish", lambda b, wm, full=False: b)
    monkeypatch.setattr(cc, "_effective_config", lambda real, g, c, u: dict(real))
    guild = MagicMock(member_count=99)
    guild.name = "G"
    user = MagicMock(id=5, display_name="Admin")
    png, note = run(cc._render_tv(MagicMock(), guild, 1, None, user))
    assert got["name"] == "Admin" and got["subtitle"] == "Member #99" and "Previewing" not in note
    cc._PREVIEW_AS[(1, None, 5)] = {"slot": 2, "name": "Sam", "number": 1200}
    png, note = run(cc._render_tv(MagicMock(), guild, 1, None, user))
    assert got["name"] == "Sam" and got["subtitle"] == "Member #1200" and got["bg"] == b"SLOT2"
    assert "Previewing picture 2, as Sam #1200" in note
    assert cc._gallery_preview_pick.await_args.kwargs["slot"] == 2


def test_nothing_is_written_to_the_database_by_the_modal(monkeypatch):
    i, rer = submit(monkeypatch, "1", "Sam", "7")
    writes = [n for n in dir(cc.db) if n.startswith(("set_", "gallery_add", "gallery_remove", "gallery_replace", "gallery_make"))]
    for n in writes:
        m = getattr(cc.db, n)
        if isinstance(m, (MagicMock, AsyncMock)):
            assert not m.called, n
