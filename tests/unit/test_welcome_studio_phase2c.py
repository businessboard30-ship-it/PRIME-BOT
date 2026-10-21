"""Welcome Card Studio Phase 2c: Pictures step shows the gallery line + rotation picker (Premium/Plus only)."""
import asyncio
import re
from pathlib import Path

import discord

from discord_bot.cogs import _views_card_customize as cc
from modules import welcome_gallery as wg

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


UNLOCKED = {"unlocked": True, "pictures": 3, "needs": [], "mode": "random"}


def test_no_summary_means_no_gallery_line_but_the_picker_is_still_there():
    v = build(None, more=True)          # summary lookup failed: wizard must not break
    assert "rotation" in kinds(v) and "Gallery" not in texts(v) and "Premium" not in texts(v)


def test_free_server_can_open_the_picker_and_is_told_it_is_preview_only():
    v = build({"unlocked": False, "mode": "random"}, more=True)
    assert "rotation" in kinds(v)
    t = texts(v)
    assert "Preview only" in t and "Studio is part of Premium." in t and "saves with Premium" in t


def test_premium_sees_count_and_mode_in_plain_words():
    t = texts(build(UNLOCKED))
    assert "3 of 10" in t and wg.MODE_LABELS["random"] in t


def test_rotation_select_only_in_more_mode_but_for_everyone():
    for studio in (UNLOCKED, {"unlocked": False}):
        assert "rotation" not in kinds(build(studio))
        assert "rotation" in kinds(build(studio, more=True))


def test_easy_mode_still_three_controls_or_fewer():
    rows = [r for r in comps(build(UNLOCKED)) if isinstance(r, discord.ui.ActionRow)]
    ctl = [r for r in rows if not ({re.match(r"cardwz_([a-z]+):", c.custom_id).group(1) for c in r.children}
                                   & {"tabbg", "preset", "undo", "testsend"})]
    assert len(ctl) <= 3


def test_more_mode_stays_inside_discord_limits():
    v = build(UNLOCKED, more=True)
    assert len(comps(v)) < 40
    assert sum(isinstance(c, discord.ui.Select) for c in comps(v)) <= 5
    for row in (c for c in comps(v) if isinstance(c, discord.ui.ActionRow)):
        assert len(row.children) <= 5


def test_missing_pictures_are_named_for_the_admin():
    t = texts(build({**UNLOCKED, "needs": [2, 5]}))
    assert "Picture 2, 5 needs a new picture" in t


def test_other_steps_do_not_show_the_gallery_line():
    for tab in ("layout", "text", "send"):
        assert "Gallery" not in texts(build(UNLOCKED, tab=tab, more=True))


def test_select_marks_the_saved_mode_and_defaults_to_random():
    def defaults(cfg):
        s = cc.CardRotationSelect(1, None, 5, cfg)
        return [o.value for o in s.item.options if o.default]
    assert defaults({}) == ["random"]
    assert defaults({"rotation_mode": "order"}) == ["order"]
    assert defaults({"rotation_mode": "junk"}) == ["random"]


def test_select_id_round_trips_for_main_and_clone_and_is_registered():
    assert cc.CardRotationSelect in cc.DYNAMIC_ITEMS
    for clone, inv in ((None, 5), (7, None)):
        s = cc.CardRotationSelect(123, clone, inv)
        m = re.match(cc.CardRotationSelect.__discord_ui_compiled_template__.pattern, s.item.custom_id)
        again = run(cc.CardRotationSelect.from_custom_id(None, None, m))
        assert (again.guild_id, again.clone_id, again.invoker_id) == (123, clone, inv)


# ---------- server-side checks ----------

class FakeResp:
    async def defer(self, **k): pass


class FakeFollow:
    def __init__(self): self.sent = []
    async def send(self, msg, **k): self.sent.append(msg)


class FakeInter:
    def __init__(self):
        self.response, self.followup = FakeResp(), FakeFollow()
        self.user = type("U", (), {"id": 5})()


def _select(monkeypatch, unlocked, value="order"):
    saved, rerendered = [], []

    async def fake_unlocked(g, c=None): return unlocked

    async def fake_save(g, c, mode): saved.append(mode)

    async def fake_access(i, inv, g=None): return True

    async def fake_rerender(*a, **k): rerendered.append(1)

    import modules.welcome_studio as ws
    monkeypatch.setattr(ws, "studio_unlocked", fake_unlocked)
    monkeypatch.setattr(cc.db, "gallery_set_rotation_mode", fake_save, raising=False)
    monkeypatch.setattr(cc, "_check_access", fake_access)
    monkeypatch.setattr(cc, "_rerender", fake_rerender)
    s = cc.CardRotationSelect(1, None, 5)
    monkeypatch.setattr(type(s.item), "values", [value], raising=False)
    return s, saved, rerendered


def test_free_server_can_try_it_but_nothing_is_saved(monkeypatch):
    cc._ROTATION_DRAFT.clear()
    s, saved, rer = _select(monkeypatch, unlocked=False, value="order")
    i = FakeInter()
    run(s.callback(i))
    assert saved == [] and rer == [1] and i.followup.sent == []
    assert cc._ROTATION_DRAFT[cc._draft_key(1, None, 5)] == "order"      # memory only


def test_preview_pick_shows_in_the_summary_and_the_select(monkeypatch):
    import modules.welcome_studio as ws

    async def no(g, c=None): return False
    monkeypatch.setattr(ws, "studio_unlocked", no)
    cc._ROTATION_DRAFT[cc._draft_key(1, None, 5)] = "member"
    cfg = run(cc._with_studio({}, 1, None, 5))
    assert cfg["_studio"] == {"unlocked": False, "mode": "member"}
    assert [o.value for o in cc.CardRotationSelect(1, None, 5, cfg).item.options if o.default] == ["member"]
    assert run(cc._with_studio({}, 1, None, 99))["_studio"]["mode"] == "random"   # other people are not affected
    cc._ROTATION_DRAFT.clear()


def test_unlocked_server_saves_the_mode_and_redraws(monkeypatch):
    s, saved, rer = _select(monkeypatch, unlocked=True, value="member")
    run(s.callback(FakeInter()))
    assert saved == ["member"] and rer == [1]


def test_studio_summary_unlocked_locked_and_error(monkeypatch):
    import modules.welcome_studio as ws

    async def yes(g, c=None): return True

    async def no(g, c=None): return False

    async def rows(g, c=None):
        return [{"slot": 1, "needs_new_picture": False}, {"slot": 2, "needs_new_picture": True}]

    async def boom(g, c=None): raise RuntimeError("db down")

    monkeypatch.setattr(cc.db, "gallery_list", rows, raising=False)
    monkeypatch.setattr(ws, "studio_unlocked", yes)
    s = run(cc._with_studio({"rotation_mode": "pin"}, 1, None))["_studio"]
    assert s == {"unlocked": True, "pictures": 2, "needs": [2], "slots": [1, 2], "mode": "pin"}
    monkeypatch.setattr(ws, "studio_unlocked", no)
    assert run(cc._with_studio({}, 1, None))["_studio"] == {"unlocked": False, "mode": "random"}
    monkeypatch.setattr(ws, "studio_unlocked", yes)
    monkeypatch.setattr(cc.db, "gallery_list", boom, raising=False)
    cfg = {"x": 1}
    assert run(cc._with_studio(cfg, 1, None)) == cfg          # error: wizard looks as before


def test_db_method_rejects_unknown_modes():
    src = (ROOT / "database.py").read_text()
    body = src.split("async def gallery_set_rotation_mode")[1].split("\n    async def ")[0]
    assert "not in MODES" in body and "return False" in body


def test_renderer_still_does_not_know_the_gallery():
    assert "welcome_gallery" not in (ROOT / "modules/welcome_card.py").read_text()
