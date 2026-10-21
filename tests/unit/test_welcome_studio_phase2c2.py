"""Welcome Card Studio Phase 2c-2: numbered strip, Remove / Make first, Unlock with Premium button."""
import asyncio
import re
from pathlib import Path

import discord

from discord_bot.cogs import _views_card_customize as cc

ROOT = Path(__file__).resolve().parents[2]


def comps(v):
    return list(v.walk_children())


def kinds(v):
    return {re.match(r"cardwz_([a-z]+):", c.custom_id).group(1) for c in comps(v) if getattr(c, "custom_id", None)}


def texts(v):
    return " ".join(c.content for c in comps(v) if isinstance(c, discord.ui.TextDisplay))


def build(studio, more=False, tab="bg"):
    cfg = {"ultra_pack_unlocked": True, "custom_background_url": "https://x/y.png"}
    if studio is not None:
        cfg["_studio"] = studio
    return cc.build_customize_view(1, None, 42, cfg, tab=tab, more=more)


def run(c):
    return asyncio.run(c)


UNLOCKED = {"unlocked": True, "pictures": 3, "needs": [3], "slots": [1, 2, 3], "mode": "random"}


# ---------- numbered strip ----------

def test_strip_shows_filled_empty_and_needs_new_picture():
    t = texts(build(UNLOCKED))
    assert "1\u25CF 2\u25CF 3\u26A0\uFE0F 4\u25CB" in t and "10\u25CB" in t


def test_no_strip_without_slots_and_never_on_other_steps():
    assert "\u25CF" not in texts(build({"unlocked": True, "pictures": 2, "needs": [], "mode": "random"}))
    for tab in ("layout", "text", "send"):
        assert "\u25CB" not in texts(build(UNLOCKED, tab=tab, more=True))


# ---------- buttons ----------

def test_manage_buttons_only_in_more_mode():
    assert not {"picremove", "picfirst"} & kinds(build(UNLOCKED))
    assert {"picremove", "picfirst"} <= kinds(build(UNLOCKED, more=True))


def test_premium_with_no_pictures_has_nothing_to_manage():
    v = build({"unlocked": True, "pictures": 0, "needs": [], "slots": [], "mode": "random"}, more=True)
    assert not {"picremove", "picfirst", "studiogoprem"} & kinds(v)


def test_free_server_sees_unlock_button_in_easy_mode_and_can_still_try_manage_buttons():
    free = {"unlocked": False, "mode": "random"}
    assert "studiogoprem" in kinds(build(free))
    assert {"picremove", "picfirst"} <= kinds(build(free, more=True))
    assert "studiogoprem" not in kinds(build(UNLOCKED, more=True))
    assert "studiogoprem" not in kinds(build(None))


def test_easy_mode_still_three_controls_or_fewer():
    for studio in (UNLOCKED, {"unlocked": False, "mode": "random"}):
        rows = [r for r in comps(build(studio)) if isinstance(r, discord.ui.ActionRow)]
        ctl = [r for r in rows if not ({re.match(r"cardwz_([a-z]+):", c.custom_id).group(1) for c in r.children}
                                       & {"tabbg", "preset", "undo", "testsend"})]
        assert len(ctl) <= 3


def test_more_mode_stays_inside_discord_limits():
    for studio in (UNLOCKED, {"unlocked": False, "mode": "random"}):
        v = build(studio, more=True)
        assert len(comps(v)) < 40
        assert sum(isinstance(c, discord.ui.Select) for c in comps(v)) <= 5
        for row in (c for c in comps(v) if isinstance(c, discord.ui.ActionRow)):
            assert len(row.children) <= 5


def test_ids_round_trip_and_are_registered():
    for cls in (cc.CardPictureRemoveButton, cc.CardPictureFirstButton, cc.CardStudioPremiumButton):
        assert cls in cc.DYNAMIC_ITEMS
        for clone, inv in ((None, 5), (7, None)):
            b = cls(123, clone, inv)
            m = re.match(cls.__discord_ui_compiled_template__.pattern, b.item.custom_id)
            again = run(cls.from_custom_id(None, None, m))
            assert (again.guild_id, again.clone_id, again.invoker_id) == (123, clone, inv)


# ---------- server-side checks ----------

class FakeResp:
    def __init__(self): self.modal = None
    async def defer(self, **k): pass
    async def send_modal(self, m): self.modal = m


class FakeFollow:
    def __init__(self): self.sent = []
    async def send(self, msg, **k): self.sent.append(msg)


class FakeInter:
    def __init__(self):
        self.response, self.followup = FakeResp(), FakeFollow()
        self.user = type("U", (), {"id": 5})()


def _modal(monkeypatch, action, unlocked, value, result=True):
    calls, rer = [], []

    async def fake_unlocked(g, c=None): return unlocked

    async def fake_remove(g, c, n): calls.append(("remove", n)); return result

    async def fake_first(g, c, n): calls.append(("first", n)); return result

    async def fake_rerender(*a, **k): rer.append(1)

    import modules.welcome_studio as ws
    monkeypatch.setattr(ws, "studio_unlocked", fake_unlocked)
    monkeypatch.setattr(cc.db, "gallery_remove", fake_remove, raising=False)
    monkeypatch.setattr(cc.db, "gallery_make_first", fake_first, raising=False)
    monkeypatch.setattr(cc, "_rerender", fake_rerender)
    m = cc.CardPictureNumberModal(1, None, 5, action)
    monkeypatch.setattr(type(m.number), "value", value, raising=False)
    return m, calls, rer


def test_premium_server_removes_and_redraws(monkeypatch):
    m, calls, rer = _modal(monkeypatch, "remove", True, "2")
    i = FakeInter()
    run(m.on_submit(i))
    assert calls == [("remove", 2)] and rer == [1] and i.followup.sent == []


def test_premium_server_makes_first_and_redraws(monkeypatch):
    m, calls, rer = _modal(monkeypatch, "first", True, " 4 ")
    run(m.on_submit(FakeInter()))
    assert calls == [("first", 4)] and rer == [1]


def test_free_server_changes_nothing_and_is_told_why(monkeypatch):
    for action in ("remove", "first"):
        m, calls, rer = _modal(monkeypatch, action, False, "2")
        i = FakeInter()
        run(m.on_submit(i))
        assert calls == [] and rer == [] and "Preview only" in i.followup.sent[0]


def test_bad_numbers_are_refused_before_touching_anything(monkeypatch):
    for bad in ("", "abc", "0", "11", "-1", "2.5"):
        m, calls, rer = _modal(monkeypatch, "remove", True, bad)
        i = FakeInter()
        run(m.on_submit(i))
        assert calls == [] and rer == [] and "1 to 10" in i.followup.sent[0]


def test_nothing_to_change_is_said_plainly(monkeypatch):
    m, calls, rer = _modal(monkeypatch, "remove", True, "6", result=False)
    i = FakeInter()
    run(m.on_submit(i))
    assert rer == [] and "no picture 6" in i.followup.sent[0]
    m, calls, rer = _modal(monkeypatch, "first", True, "1", result=False)
    i = FakeInter()
    run(m.on_submit(i))
    assert "already first" in i.followup.sent[0]


def test_database_error_does_not_crash(monkeypatch):
    m, calls, rer = _modal(monkeypatch, "remove", True, "2")

    async def boom(g, c, n): raise RuntimeError("db down")
    monkeypatch.setattr(cc.db, "gallery_remove", boom, raising=False)
    i = FakeInter()
    run(m.on_submit(i))
    assert rer == [] and "Couldn't" in i.followup.sent[0]


def test_buttons_open_the_right_modal(monkeypatch):
    async def ok(i, inv, g=None): return True
    monkeypatch.setattr(cc, "_check_access", ok)
    for cls, action in ((cc.CardPictureRemoveButton, "remove"), (cc.CardPictureFirstButton, "first")):
        i = FakeInter()
        run(cls(1, None, 5).callback(i))
        assert isinstance(i.response.modal, cc.CardPictureNumberModal) and i.response.modal.action == action


def test_database_make_first_never_violates_the_unique_slot_key():
    src = (ROOT / "database.py").read_text()
    body = src.split("async def gallery_make_first")[1].split("\n    async def ")[0]
    assert "conn.transaction()" in body and "pg_advisory_xact_lock" in body
    assert "last_slot_used = NULL" in body
    assert "DELETE" not in body                       # pictures are never deleted by Make first
    swap = body.split("else:")[1]
    assert "SET slot" not in swap                     # a swap moves content, never slot numbers


def test_renderer_still_does_not_know_the_gallery():
    assert "welcome_gallery" not in (ROOT / "modules/welcome_card.py").read_text()
