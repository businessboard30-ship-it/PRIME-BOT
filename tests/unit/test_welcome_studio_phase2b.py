"""Welcome Card Studio Phase 2b: the join render picks from the gallery (gated), else the old single picture."""
import asyncio
from pathlib import Path
from types import SimpleNamespace

import discord
import pytest

from discord_bot.cogs import welcome as w

ROOT = Path(__file__).resolve().parents[2]
CFG = {"guild_id": 1, "clone_id": None, "rotation_mode": "order", "last_slot_used": None}


def run(coro):
    return asyncio.run(coro)


class FakeDb:
    def __init__(self, rows):
        self.rows, self.flagged, self.last, self.counter = rows, [], [], 0

    async def gallery_list(self, g, c): return self.rows
    async def gallery_flag_missing(self, g, c, slot): self.flagged.append(slot)
    async def gallery_set_last_slot(self, g, c, slot): self.last.append(slot)
    async def gallery_next_counter(self, g, c):
        self.counter += 1
        return self.counter - 1


def pic(slot, **kw):
    return {"slot": slot, "channel_id": 10, "message_id": 100 + slot, "url_cache": f"u{slot}", "needs_new_picture": False, **kw}


@pytest.fixture
def setup(monkeypatch):
    state = SimpleNamespace(unlocked=True, gone=set(), bad=set(), old_calls=0, db=FakeDb([]))
    monkeypatch.setattr(w, "db", state.db, raising=False)

    async def unlocked(g, c=None):
        if state.unlocked == "boom":
            return False
        return state.unlocked
    monkeypatch.setattr("modules.welcome_studio.studio_unlocked", unlocked)

    async def slot_url(bot, row):
        return (None, True) if row["slot"] in state.gone else (f"url{row['slot']}", False)
    monkeypatch.setattr(w, "_gallery_slot_url", slot_url)

    async def fetch(session, url):
        slot = int(url[3:])
        return (None, "x") if slot in state.bad else (f"bytes{slot}".encode(), None)
    monkeypatch.setattr(w, "_fetch_custom_bg_bytes", fetch)

    async def old(session, cfg, bot=None):
        state.old_calls += 1
        return b"OLD"
    monkeypatch.setattr(w, "_custom_bg_bytes_for_render", old)
    state.go = lambda cfg=CFG, n=1: run(w._gallery_bg_bytes_for_render(None, cfg, object(), n))
    return state


def test_locked_server_gets_the_old_single_picture_and_never_reads_the_gallery(setup):
    setup.unlocked = False
    setup.db.rows = [pic(1), pic(2)]
    assert setup.go() == b"OLD" and setup.old_calls == 1 and setup.db.counter == 0


def test_unlocked_with_pictures_uses_the_gallery(setup):
    setup.db.rows = [pic(1), pic(2), pic(3)]
    assert setup.go(dict(CFG, rotation_mode="member"), 5) == b"bytes3"   # 5 mod 3 = 2 -> third picture
    assert setup.old_calls == 0


def test_in_order_mode_walks_the_pictures_with_the_atomic_counter(setup):
    setup.db.rows = [pic(1), pic(2)]
    assert [setup.go() for _ in range(3)] == [b"bytes1", b"bytes2", b"bytes1"]


def test_random_mode_remembers_the_last_picture_used(setup):
    setup.db.rows = [pic(1), pic(2)]
    setup.go(dict(CFG, rotation_mode="random"))
    assert setup.db.last and setup.db.last[0] in (1, 2)


def test_empty_gallery_falls_back_to_the_old_picture(setup):
    assert setup.go() == b"OLD"


def test_deleted_picture_is_flagged_and_the_next_one_is_used(setup):
    setup.db.rows = [pic(1), pic(2)]
    setup.gone = {1}
    assert setup.go(dict(CFG, rotation_mode="pin")) == b"bytes2"
    assert setup.db.flagged == [1]


def test_all_pictures_deleted_falls_back_without_crashing(setup):
    setup.db.rows = [pic(1), pic(2)]
    setup.gone = {1, 2}
    assert setup.go() == b"OLD" and sorted(setup.db.flagged) == [1, 2]


def test_a_picture_that_will_not_load_is_skipped_but_not_flagged(setup):
    setup.db.rows = [pic(1), pic(2)]
    setup.bad = {1}
    assert setup.go(dict(CFG, rotation_mode="pin")) == b"bytes2" and setup.db.flagged == []


def test_slots_already_flagged_are_ignored(setup):
    setup.db.rows = [pic(1, needs_new_picture=True), pic(2)]
    assert setup.go() == b"bytes2"


def test_gate_error_fails_closed_to_the_old_picture(setup):
    setup.unlocked = "boom"
    setup.db.rows = [pic(1)]
    assert setup.go() == b"OLD"


def test_any_unexpected_error_still_gives_the_old_picture(setup):
    class Broken(FakeDb):
        async def gallery_list(self, g, c): raise RuntimeError("db down")
    w.db = Broken([])
    assert setup.go() == b"OLD"


def test_no_bot_means_old_path(setup):
    assert run(w._gallery_bg_bytes_for_render(None, CFG, None, 1)) == b"OLD"


def test_only_the_real_join_uses_the_gallery():
    src = (ROOT / "discord_bot/cogs/welcome.py").read_text()
    assert src.count("_gallery_pick_for_render(") == 3      # the definition, the 2b wrapper, on_member_join (8c)
    join = src.split("async def on_member_join")[1].split("\n    async def ")[0]
    assert "_gallery_pick_for_render(" in join and "_custom_bg_bytes_for_render(" not in join
