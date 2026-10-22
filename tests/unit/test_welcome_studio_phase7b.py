"""Welcome Card Studio Phase 7b: schedule a saved design from the Send step (button + modal), 'live today' line,
active_design_id remembered on Load."""
import asyncio
import datetime as dt
import json
import re
from pathlib import Path

import discord

from discord_bot.cogs import _views_card_customize as cc
from modules import welcome_designs as wd
import modules.welcome_studio as ws

ROOT = Path(__file__).resolve().parents[2]
D = dt.date


def run(c):
    return asyncio.run(c)


def kinds(v):
    return {re.match(r"cardwz_([a-z]+):", c.custom_id).group(1) for c in v.walk_children() if getattr(c, "custom_id", None)}


def build(tab, studio, more=False):
    cfg = {"ultra_pack_unlocked": True, "ultra_trial_active": True, "custom_background_url": "https://x/y.png"}
    if studio is not None:
        cfg["_studio"] = studio
    return cc.build_customize_view(1, None, 42, cfg, tab=tab, more=more, has_tv=True, note="x")


DESIGNS = [{"id": 11, "name": "Halloween", "when": "25 Oct to 1 Nov every year"}, {"id": 12, "name": "Spring", "when": None}]
ROWS = [{"id": 11, "name": "Halloween", "options_json": "{}", "avatar_shape": "circle",
         "sched_from": D(2000, 10, 25), "sched_to": D(2000, 11, 1), "sched_yearly": True},
        {"id": 12, "name": "Spring", "options_json": "{}", "avatar_shape": "circle", "sched_from": None, "sched_to": None, "sched_yearly": False}]


# ---------- the Send step ----------

def test_schedule_button_shows_only_when_designs_exist_and_stays_inside_limits():
    assert "designsched" not in kinds(build("send", {"unlocked": True, "designs": []}))
    assert "designsched" not in kinds(build("send", {"unlocked": False}))
    v = build("send", {"unlocked": True, "designs": DESIGNS})
    assert {"designsave", "designrename", "designsched", "designdelete"} <= kinds(v)
    assert len(list(v.walk_children())) < 40
    for row in v.walk_children():
        if isinstance(row, discord.ui.ActionRow):
            assert len(row.children) <= 5


def test_other_steps_never_show_it():
    for tab, more in (("bg", True), ("layout", True), ("text", True)):
        assert "designsched" not in kinds(build(tab, {"unlocked": True, "designs": DESIGNS}, more)), tab


def test_line_shows_schedules_and_what_is_live_today():
    line = cc._designs_line({"unlocked": True, "designs": DESIGNS})
    assert "Scheduled (UTC)" in line and "1 Halloween: 25 Oct to 1 Nov every year" in line and "Spring:" not in line
    assert "Today new members get" not in line
    live = cc._designs_line({"unlocked": True, "designs": DESIGNS, "live_now": "Halloween"})
    assert "Today new members get **Halloween**" in live and "regular look" in live
    plain = cc._designs_line({"unlocked": True, "designs": [{"id": 1, "name": "A", "when": None}]})
    assert "Scheduled" not in plain


def test_registered_and_id_round_trips():
    assert cc.CardDesignScheduleButton in cc.DYNAMIC_ITEMS
    m = re.match(cc.CardDesignScheduleButton.__discord_ui_compiled_template__.pattern, cc.CardDesignScheduleButton(1, None, 5).item.custom_id)
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
    def __init__(self, designs=None, ok=True):
        self.designs, self.ok, self.set_calls, self.clear_calls, self.active = list(designs or []), ok, [], [], None
        self.cfg = {"ultra_pack_unlocked": True}

    async def design_list(self, g, c): return list(self.designs)
    async def design_get(self, g, c, did): return next((d for d in self.designs if d["id"] == did), None)
    async def design_set_schedule(self, g, c, did, a, b, y): self.set_calls.append((did, a, b, y)); return self.ok
    async def design_clear_schedule(self, g, c, did): self.clear_calls.append(did); return self.ok
    async def set_active_design(self, g, c, did): self.active = did; return True


def setup(monkeypatch, db, unlocked=True):
    rer, forgot = [], []

    async def fake_unlocked(g, c=None): return unlocked

    async def fake_rerender(*a, **k): rer.append(1)

    async def ok(i, inv, g=None): return True
    monkeypatch.setattr(ws, "studio_unlocked", fake_unlocked)
    monkeypatch.setattr(ws, "forget_schedule_cache", lambda g=None, c=None: forgot.append((g, c)))
    monkeypatch.setattr(cc, "db", db)
    monkeypatch.setattr(cc, "_rerender", fake_rerender)
    monkeypatch.setattr(cc, "_check_access", ok)
    return rer, forgot


def sched(monkeypatch, db, number, a, b, unlocked=True):
    rer, forgot = setup(monkeypatch, db, unlocked)
    m = cc.CardDesignScheduleModal(1, None, 5)
    m.number._value, m.date_from._value, m.date_to._value = number, a, b
    i = FakeInter()
    run(m.on_submit(i))
    return rer, forgot, i


# ---------- the modal ----------

def test_a_yearly_schedule_is_saved_and_the_render_cache_is_dropped(monkeypatch):
    db = FakeDB(ROWS)
    rer, forgot, i = sched(monkeypatch, db, " 1 ", "25 Oct", "1 Nov")
    assert db.set_calls == [(11, D(2000, 10, 25), D(2000, 11, 1), True)] and rer == [1] and forgot == [(1, None)] and not i.followup.sent


def test_a_one_off_schedule_and_a_single_day(monkeypatch):
    db = FakeDB(ROWS)
    sched(monkeypatch, db, "2", "2026-12-24", "2026-12-26")
    assert db.set_calls == [(12, D(2026, 12, 24), D(2026, 12, 26), False)]
    db = FakeDB(ROWS)
    sched(monkeypatch, db, "2", "14 Feb", "")
    assert db.set_calls == [(12, D(2000, 2, 14), D(2000, 2, 14), True)]


def test_empty_from_removes_the_schedule_only(monkeypatch):
    db = FakeDB(ROWS)
    rer, forgot, i = sched(monkeypatch, db, "1", "  ", "1 Nov")
    assert db.clear_calls == [11] and db.set_calls == [] and rer == [1] and forgot == [(1, None)]


def test_bad_dates_say_what_to_type_and_change_nothing(monkeypatch):
    cases = {("nope", "1 Nov"): "From", ("25 Oct", "nope"): "Until", ("2026-10-25", "1 Nov"): "same way", ("2026-11-02", "2026-10-25"): "before"}
    for (a, b), word in cases.items():
        db = FakeDB(ROWS)
        rer, forgot, i = sched(monkeypatch, db, "1", a, b)
        assert db.set_calls == [] and rer == [] and forgot == [] and word in i.followup.sent[0], (a, b)


def test_bad_numbers_and_free_servers_change_nothing(monkeypatch):
    for bad in ("", "abc", "0", "3", "-1"):
        db = FakeDB(ROWS)
        rer, forgot, i = sched(monkeypatch, db, bad, "25 Oct", "1 Nov")
        assert db.set_calls == [] and rer == [] and "from 1 to 2" in i.followup.sent[0], bad
    db = FakeDB(ROWS)
    rer, forgot, i = sched(monkeypatch, db, "1", "25 Oct", "1 Nov", unlocked=False)
    assert db.set_calls == [] and db.clear_calls == [] and "Preview only" in i.followup.sent[0]


def test_a_failed_write_says_so_and_does_not_redraw(monkeypatch):
    db = FakeDB(ROWS, ok=False)
    rer, forgot, i = sched(monkeypatch, db, "1", "25 Oct", "1 Nov")
    assert rer == [] and forgot == [] and "Couldn't save" in i.followup.sent[0]
    rer, forgot, i = sched(monkeypatch, FakeDB(ROWS, ok=False), "1", "", "")
    assert "Couldn't remove" in i.followup.sent[0]


def test_button_opens_the_schedule_modal(monkeypatch):
    setup(monkeypatch, FakeDB(ROWS))
    i = FakeInter()
    run(cc.CardDesignScheduleButton(1, None, 5).callback(i))
    assert isinstance(i.response.modal, cc.CardDesignScheduleModal)


# ---------- summary + load ----------

async def _async(v):
    return v


def test_summary_reports_schedules_and_what_is_live_today(monkeypatch):
    db = FakeDB(ROWS)
    db.gallery_list = lambda g, c: _async([])
    setup(monkeypatch, db, unlocked=True)
    monkeypatch.setattr(ws, "_today_utc", lambda: D(2026, 10, 28))
    out = run(cc._with_studio({"ultra_pack_unlocked": True}, 1, None, 5))["_studio"]
    assert out["designs"][0]["when"] == "25 Oct to 1 Nov every year" and out["designs"][1]["when"] is None
    assert out["live_now"] == "Halloween"
    monkeypatch.setattr(ws, "_today_utc", lambda: D(2026, 12, 1))
    assert run(cc._with_studio({"ultra_pack_unlocked": True}, 1, None, 5))["_studio"]["live_now"] is None


def test_load_remembers_which_design_is_live(monkeypatch):
    oj, shape = wd.snapshot_look({"font": "poster"}, "circle")
    db = FakeDB([{"id": 11, "name": "H", "options_json": oj, "avatar_shape": shape}])
    rer, forgot = setup(monkeypatch, db)

    async def fake_apply(g, c, u, opts, sh): pass
    monkeypatch.setattr(cc, "_apply_style", fake_apply)
    sel = cc.CardDesignSelect(1, None, 5, [])
    sel.item._values = ["11"]
    run(sel.callback(FakeInter()))
    assert db.active == 11 and rer == [1]


def test_source_guards():
    src = (ROOT / "discord_bot/cogs/_views_card_customize.py").read_text()
    body = src[src.index("class CardDesignScheduleModal"):src.index("class _DesignButton")]
    assert "set_welcome_config" not in body and "ultra_card_json" not in body        # schedule never edits the live look
    assert body.count("studio_unlocked") >= 2                                          # gated on the server
