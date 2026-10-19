"""Cat fights phase 4B: Request Board screens (budgets, text, flows on fakes, wiring). Play coins only."""
import asyncio
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord

from discord_bot.cogs import _cat_visibility as vis
from discord_bot.cogs import _views_cat_arena as va
from discord_bot.cogs import _views_cat_board as vb
from modules import cat_adopt as ca
from modules import cat_arena as ar
from modules import cat_battle as cb
from modules import cat_requests as cr
from modules import cat_stats as cs

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime.now(timezone.utc)


def run(c):
    return asyncio.run(c)


def _count(view):
    return sum(1 for _ in view.walk_children())


def ready(name="Mochi", cid=11, rank=3):
    cat = ca.Cat(id=cid, owner_id=1, clone_id=None, pool_id=1, name=name, rarity="legendary", trait="Brave", cat_rank=rank)
    st = cs.Stats(strength=20, guard=15, speed=15, spirit=14, skills=("pounce", "hiss", "fluff_guard"))
    return ar.Ready(cat, st, ar.Effects(), cb.from_cat(cat, st))


def req(i=1, wager=50_000, name="Biscuit"):
    return cr.Request(id=i, poster_id=999_999_999_999_999_999, cat_id=i, wager=wager, power=123, status="open",
                      guild_id=5, show_server=True, server_name="The Very Long Cat Lovers Club of Everywhere",
                      expires_at=NOW + timedelta(hours=5), cat_name=name, rarity="legendary", cat_rank=25)


def test_pr_band_is_rounded_not_exact():
    assert vb.pr_band(123) == "~120" and vb.pr_band(125) == "~120" or vb.pr_band(125) == "~130"
    assert vb.pr_band(1) == "~10"


def test_request_line_shows_no_ids_and_trims_the_server():
    line = vb.request_line(req(), "Mia")
    assert "999999999999999999" not in line and "<@" not in line and "Mia" in line and "50,000" in line
    assert "~120" in line and "123" not in line
    assert vb.request_line(cr.Request(1, 2, 3, 500, 50, "open", cat_name="x"), "Mia").endswith("by Mia")   # no server shown


def test_board_screen_fits_with_back():
    items = [req(i) for i in range(5)]
    view = vb.BoardView(SimpleNamespace(interaction_check=AsyncMock()), [ready(f"Cat{i}", i) for i in range(25)])
    view.items, view.pages, view.page, view.client = items, 9, 4, SimpleNamespace(get_user=lambda i: None)
    view.note = "x" * 100
    view._build()
    assert _count(view) <= 40
    vis.attach_back(view)
    assert _count(view) <= 40
    names = [vb.request_line(r, "A" * 32) for r in items]
    assert len(vb.board_text(names, 4, 9, 123, "x" * 100)) < 4000
    assert "no cat that is ready" in vb.board_text([], 0, 1, None)
    assert "Nothing on the board" in vb.board_text([], 0, 1, 100)


def test_post_and_mine_screens_fit_with_back():
    cats = [ready(f"Cat{i}", i) for i in range(25)]
    pv = vb.PostView(object(), object(), cats, 2, "note")
    assert _count(pv) <= 40 and len(vb.post_text(cats, 1, 50_000, 24, 2, "x" * 100)) < 4000
    vis.attach_back(pv)
    assert _count(pv) <= 40
    mv = vb.MineView(object(), object(), [req(i) for i in range(10)], "note")
    assert _count(mv) <= 40 and len(vb.mine_text([req(i) for i in range(10)], "x" * 100)) < 4000
    vis.attach_back(mv)
    assert _count(mv) <= 40
    assert "no open requests" in vb.mine_text([])


def _inter():
    i = MagicMock()
    i.user.id, i.guild_id = 1, 5
    i.guild.name = "Cats"
    i.message = None
    i.response.edit_message = AsyncMock()
    i.response.is_done = MagicMock(return_value=False)
    i.client.clone_id = None
    return i


def test_posting_uses_the_cats_power_and_removes_the_cat_from_the_list(monkeypatch):
    seen = {}

    async def fake_post(poster, clone, cat, wager, power, hours, gid, gname, *a, **k):
        seen.update(poster=poster, cat=cat, wager=wager, power=power, hours=hours, gid=gid, gname=gname)
        return cr.PostResult("ok", 9, 9_500)
    monkeypatch.setattr(cr, "post", fake_post)
    cats = [ready("A", 11), ready("B", 12)]
    pv = vb.PostView(object(), object(), cats, 0)
    run(pv._send(_inter()))
    assert seen["cat"] == 11 and seen["power"] == cats[0].fighter.power and seen["hours"] == cr.DEFAULT_HOURS
    assert seen["gname"] == "Cats" and [r.cat.id for r in pv.ready] == [12] and pv.open_n == 1
    assert "on the board" in pv.note


def test_post_errors_show_the_reason_and_keep_the_cat(monkeypatch):
    monkeypatch.setattr(cr, "post", AsyncMock(return_value=cr.PostResult("max_open")))
    pv = vb.PostView(object(), object(), [ready()], 2)
    run(pv._send(_inter()))
    assert "2 open requests" in pv.note and len(pv.ready) == 1


def test_taking_a_request_down_refunds_and_updates_the_list(monkeypatch):
    items = [req(1), req(2)]
    monkeypatch.setattr(cr, "cancel", AsyncMock(return_value=cr.EndResult("ok", 50_000)))
    monkeypatch.setattr(cr, "mine", AsyncMock(return_value=items[1:]))
    mv = vb.MineView(object(), object(), items)
    run(mv._cancel(_inter()))
    assert "50,000" in mv.note and [r.id for r in mv.items] == [2] and mv.pick == 2


def test_arena_screen_has_the_board_button_and_still_fits():
    view = va.ArenaView()
    view.st = ar.ArenaState(True, 1234, 20, 11, 9, -45_000, NOW - timedelta(days=30))
    view._build()
    labels = [c.label for c in view.walk_children() if isinstance(c, discord.ui.Button)]
    assert "Request board" in labels and _count(view) <= 40
    vis.attach_back(view)
    assert _count(view) <= 40


def test_the_expiry_sweep_runs_on_the_main_bot_only_and_there_is_no_new_slash_command():
    cog = (ROOT / "discord_bot/cogs/cat.py").read_text()
    assert "_requests_sweep.start()" in cog and "cat_requests.expire_due" in cog
    start = cog.index("_requests_sweep.start()")
    assert 'getattr(self.bot, "clone_id", None) is None' in cog[start - 700:start]
    assert "app_commands" not in (ROOT / "discord_bot/cogs/_views_cat_board.py").read_text()


def test_the_board_module_never_shows_ids():
    src = (ROOT / "discord_bot/cogs/_views_cat_board.py").read_text()
    assert "<@" not in src and "poster_id}" not in src
