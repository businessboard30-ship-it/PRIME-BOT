"""Welcome Card Studio Phase 5b: saved designs in the wizard (Send step): Save, Load, Rename, Delete."""
import asyncio
import json
import re
from pathlib import Path

import discord

from discord_bot.cogs import _views_card_customize as cc
from modules import welcome_card as wc
from modules import welcome_designs as wd
import modules.welcome_studio as ws

ROOT = Path(__file__).resolve().parents[2]


def run(c):
    return asyncio.run(c)


def kinds(v):
    return {re.match(r"cardwz_([a-z]+):", c.custom_id).group(1) for c in v.walk_children() if getattr(c, "custom_id", None)}


def build(tab, studio, more=False):
    cfg = {"ultra_pack_unlocked": True, "ultra_trial_active": True, "custom_background_url": "https://x/y.png"}
    if studio is not None:
        cfg["_studio"] = studio
    return cc.build_customize_view(1, None, 42, cfg, tab=tab, more=more, has_tv=True, note="x")


DESIGNS = [{"id": 11, "name": "Halloween"}, {"id": 12, "name": "Spring"}]


# ---------- the Send step ----------

def test_send_step_unlocked_without_designs_shows_only_save():
    k = kinds(build("send", {"unlocked": True, "designs": []}))
    assert "designsave" in k and not ({"designload", "designrename", "designdelete"} & k)


def test_send_step_with_designs_shows_select_rename_delete_and_stays_inside_limits():
    v = build("send", {"unlocked": True, "designs": DESIGNS})
    assert {"designsave", "designload", "designrename", "designdelete"} <= kinds(v)
    assert len(list(v.walk_children())) < 40
    assert sum(1 for c in v.walk_children() if isinstance(c, discord.ui.Select)) <= 5
    for row in v.walk_children():
        if isinstance(row, discord.ui.ActionRow):
            assert len(row.children) <= 5
    sel = next(c for c in v.walk_children() if "designload" in (getattr(c, "custom_id", "") or ""))
    sel = getattr(sel, "item", sel)
    assert [o.label for o in sel.options] == ["Halloween", "Spring"] and [o.value for o in sel.options] == ["11", "12"]


def test_locked_server_can_try_save_but_has_no_designs_to_load():
    k = kinds(build("send", {"unlocked": False}))
    assert "designsave" in k and not ({"designload", "designrename", "designdelete"} & k)
    assert "Preview only" in cc._designs_line({"unlocked": False})


def test_no_summary_means_the_send_step_looks_as_before():
    k = kinds(build("send", None))
    assert not any(x.startswith("design") for x in k)


def test_other_steps_never_show_design_controls():
    for tab, more in (("bg", True), ("layout", True), ("text", True)):
        assert not any(x.startswith("design") for x in kinds(build(tab, {"unlocked": True, "designs": DESIGNS}, more))), tab


def test_line_texts():
    assert "none yet" in cc._designs_line({"unlocked": True, "designs": []})
    line = cc._designs_line({"unlocked": True, "designs": DESIGNS})
    assert "2 of 5" in line and "1 Halloween" in line and "2 Spring" in line
    assert cc._designs_line(None) is None


def test_registered_and_ids_round_trip():
    for cls in (cc.CardDesignSelect, cc.CardDesignSaveButton, cc.CardDesignRenameButton, cc.CardDesignDeleteButton):
        assert cls in cc.DYNAMIC_ITEMS
    m = re.match(cc.CardDesignSaveButton.__discord_ui_compiled_template__.pattern, cc.CardDesignSaveButton(1, None, 5).item.custom_id)
    assert m and cc._decode(m) == (1, None, 5)


# ---------- fakes ----------

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


class FakeDB:
    def __init__(self, designs=None, save=("created", 1), cfg=None):
        self.designs, self.save_result, self.saves, self.deleted, self.renamed = list(designs or []), save, [], [], []
        self.cfg = cfg or {"ultra_pack_unlocked": True,
                           "ultra_card_json": json.dumps({"font": "marquee", "frame": "glow", "effect": "sparkle"}),
                           "avatar_shape": "hexagon"}

    async def get_welcome_config(self, g, clone_id=None): return dict(self.cfg)
    async def design_list(self, g, c): return list(self.designs)
    async def design_get(self, g, c, did): return next((d for d in self.designs if d["id"] == did), None)
    async def design_save(self, g, c, name, oj, sh, by, mx=5): self.saves.append((name, oj, sh, by, mx)); return self.save_result
    async def design_delete(self, g, c, did): self.deleted.append(did); return True
    async def design_rename(self, g, c, did, name): self.renamed.append((did, name)); return True


def setup(monkeypatch, db, unlocked=True):
    rer, applied = [], []

    async def fake_unlocked(g, c=None): return unlocked

    async def fake_rerender(*a, **k): rer.append(1)

    async def fake_apply(g, c, u, opts, shape): applied.append((opts, shape))

    async def ok(i, inv, g=None): return True
    monkeypatch.setattr(ws, "studio_unlocked", fake_unlocked)
    monkeypatch.setattr(cc, "db", db)
    monkeypatch.setattr(cc, "_rerender", fake_rerender)
    monkeypatch.setattr(cc, "_apply_style", fake_apply)
    monkeypatch.setattr(cc, "_check_access", ok)
    return rer, applied


def save_modal(name):
    m = cc.CardDesignSaveModal(1, None, 5)
    m.name._value = name
    return m


# ---------- Save ----------

def test_save_stores_the_look_the_editor_shows_and_redraws(monkeypatch):
    db = FakeDB()
    rer, _ = setup(monkeypatch, db)
    i = FakeInter()
    run(save_modal("  Halloween  ").on_submit(i))
    name, oj, shape, by, mx = db.saves[0]
    d = json.loads(oj)
    assert name == "Halloween" and shape == "hexagon" and by == 5 and mx == wd.MAX_DESIGNS
    assert d["font"] == "marquee" and d["frame"] == "glow" and d["effect"] == "sparkle"
    assert not any(k.startswith(("custom_bg", "custom_background")) for k in d)
    assert rer == [1] and i.followup.sent == []


def test_save_on_a_free_server_stores_nothing_and_says_why(monkeypatch):
    db = FakeDB()
    rer, _ = setup(monkeypatch, db, unlocked=False)
    i = FakeInter()
    run(save_modal("X").on_submit(i))
    assert db.saves == [] and rer == [] and "Preview only" in i.followup.sent[0]


def test_save_refuses_empty_name_and_reports_full_and_error(monkeypatch):
    db = FakeDB()
    rer, _ = setup(monkeypatch, db)
    i = FakeInter()
    run(save_modal("   ").on_submit(i))
    assert db.saves == [] and "name" in i.followup.sent[0]
    for status, word in (("full", "5 designs"), ("error", "Couldn't")):
        db = FakeDB(save=(status, None))
        rer, _ = setup(monkeypatch, db)
        i = FakeInter()
        run(save_modal("Y").on_submit(i))
        assert rer == [] and word in i.followup.sent[0]


# ---------- Load ----------

def load(monkeypatch, db, value, unlocked=True):
    rer, applied = setup(monkeypatch, db, unlocked)
    sel = cc.CardDesignSelect(1, None, 5, DESIGNS)
    monkeypatch.setattr(type(sel.item), "values", property(lambda self: [value]), raising=False)
    i = FakeInter()
    run(sel.callback(i))
    return rer, applied, i


def test_load_applies_the_whole_look_through_the_undoable_gated_path(monkeypatch):
    oj, shape = wd.snapshot_look({"font": "poster", "text_outline": "glow"}, "diamond")
    db = FakeDB(designs=[{"id": 11, "name": "Halloween", "options_json": oj, "avatar_shape": shape}])
    rer, applied, i = load(monkeypatch, db, "11")
    assert len(applied) == 1 and applied[0][1] == "diamond"
    assert applied[0][0]["font"] == "poster" and applied[0][0]["text_outline"] == "glow"
    assert set(applied[0][0]) == set(wc.parse_ultra_options(None))          # every key, so the old look is fully replaced
    assert rer == [1]


def test_load_never_applies_on_a_free_server_or_a_missing_design_or_junk(monkeypatch):
    db = FakeDB(designs=[{"id": 11, "name": "A", "options_json": "{}", "avatar_shape": "circle"}])
    rer, applied, i = load(monkeypatch, db, "11", unlocked=False)
    assert applied == [] and "Preview only" in i.followup.sent[0]
    rer, applied, i = load(monkeypatch, FakeDB(), "99")
    assert applied == [] and "gone" in i.followup.sent[0]
    rer, applied, i = load(monkeypatch, FakeDB(), "not-a-number")
    assert applied == [] and "gone" in i.followup.sent[0]


def test_load_survives_a_corrupt_stored_design(monkeypatch):
    db = FakeDB(designs=[{"id": 11, "name": "Bad", "options_json": "{{{", "avatar_shape": "nope"}])
    rer, applied, i = load(monkeypatch, db, "11")
    assert len(applied) == 1 and applied[0][1] == wc.DEFAULT_AVATAR_SHAPE and applied[0][0] == wc.parse_ultra_options(None)


# ---------- Rename / Delete ----------

def number_modal(monkeypatch, db, action, number, new_name="New", unlocked=True):
    rer, _ = setup(monkeypatch, db, unlocked)
    m = cc.CardDesignNumberModal(1, None, 5, action)
    m.number._value = number
    if m.new_name is not None:
        m.new_name._value = new_name
    i = FakeInter()
    run(m.on_submit(i))
    return rer, i


def test_delete_and_rename_use_the_number_in_the_list(monkeypatch):
    db = FakeDB(designs=DESIGNS)
    rer, i = number_modal(monkeypatch, db, "delete", "2")
    assert db.deleted == [12] and rer == [1]
    db = FakeDB(designs=DESIGNS)
    rer, i = number_modal(monkeypatch, db, "rename", " 1 ", new_name="  Fall ")
    assert db.renamed == [(11, "Fall")] and rer == [1]


def test_bad_numbers_names_and_free_servers_change_nothing(monkeypatch):
    for bad in ("", "abc", "0", "3", "-1"):
        db = FakeDB(designs=DESIGNS)
        rer, i = number_modal(monkeypatch, db, "delete", bad)
        assert db.deleted == [] and rer == [] and "from 1 to 2" in i.followup.sent[0], bad
    db = FakeDB(designs=DESIGNS)
    rer, i = number_modal(monkeypatch, db, "rename", "1", new_name="   ")
    assert db.renamed == [] and "new name" in i.followup.sent[0]
    for action in ("delete", "rename"):
        db = FakeDB(designs=DESIGNS)
        rer, i = number_modal(monkeypatch, db, action, "1", unlocked=False)
        assert db.deleted == [] and db.renamed == [] and "Preview only" in i.followup.sent[0]


def test_buttons_open_the_right_modal(monkeypatch):
    setup(monkeypatch, FakeDB())
    for cls, kind in ((cc.CardDesignSaveButton, cc.CardDesignSaveModal), (cc.CardDesignRenameButton, cc.CardDesignNumberModal),
                      (cc.CardDesignDeleteButton, cc.CardDesignNumberModal)):
        i = FakeInter()
        run(cls(1, None, 5).callback(i))
        assert isinstance(i.response.modal, kind)
    assert i.response.modal.action == "delete"


# ---------- the studio summary ----------

def test_studio_summary_lists_designs_only_for_premium(monkeypatch):
    db = FakeDB(designs=[{"id": 11, "name": "A", "options_json": "{}", "avatar_shape": "circle"}])
    db.gallery_list = lambda g, c: _async([])
    setup(monkeypatch, db, unlocked=True)
    out = run(cc._with_studio({"ultra_pack_unlocked": True}, 1, None, 5))
    assert out["_studio"]["designs"] == [{"id": 11, "name": "A"}]
    setup(monkeypatch, db, unlocked=False)
    out = run(cc._with_studio({"ultra_pack_unlocked": True}, 1, None, 5))
    assert "designs" not in out["_studio"]


async def _async(v):
    return v


def test_load_goes_through_apply_style_which_is_gated_and_undoable():
    src = (ROOT / "discord_bot/cogs/_views_card_customize.py").read_text()
    body = src[src.index("class CardDesignSelect"):src.index("class CardDesignSaveModal")]
    assert "_apply_style(" in body and "set_welcome_config" not in body
