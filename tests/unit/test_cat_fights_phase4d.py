"""Cat fights phase 4D: server toggles, reports, block list, repeated-pair flag. Play coins only."""
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from discord_bot.cogs import _cat_visibility as vis
from discord_bot.cogs import _views_cat_board as vb
from modules import cat_requests as cr
from modules import cat_setup as cs

ROOT = Path(__file__).resolve().parents[2]


def run(c):
    return asyncio.run(c)


def _count(view):
    return sum(1 for _ in view.walk_children())


def test_fight_toggles_default_off_and_survive_a_row_round_trip():
    cfg = cs.CatConfig(guild_id=1)
    assert cfg.fights_cross_server is False and cfg.fights_show_server is False
    row = {"guild_id": 1, "clone_id": None, "enabled": True, "channel_ids": [5], "interval_minutes": 720, "next_at": None,
           "last_post_at": None}
    assert cs.CatConfig.from_row(_Row(row)).fights_cross_server is False            # old rows without the columns
    row.update(fights_cross_server=True, fights_show_server=True)
    got = cs.CatConfig.from_row(_Row(row))
    assert got.fights_cross_server and got.fights_show_server


class _Row(dict):
    pass


def test_migration_is_additive_and_wired():
    sql = (ROOT / "database/migrations/061_cat_fight_setup_reports.sql").read_text()
    assert "ADD COLUMN IF NOT EXISTS fights_cross_server BOOLEAN NOT NULL DEFAULT FALSE" in sql
    assert "CREATE TABLE IF NOT EXISTS cat_fight_reports" in sql
    assert "DROP" not in sql.upper() and "061_cat_fight_setup_reports.sql" in (ROOT / "database.py").read_text()
    assert "fights_cross_server" in (ROOT / "modules/cat_setup.py").read_text()


def test_pair_flag_only_fires_at_the_line_and_names_no_one_twice():
    assert cr.pair_flag_text(1, 2, cr.REPEAT_FLAG_AT - 1) is None
    t = cr.pair_flag_text(9, 3, cr.REPEAT_FLAG_AT)
    assert t and "3 and 9" in t


class ReportConn:
    def __init__(self):
        self.fights = {1: {"challenger_id": 2, "challenged_id": 1}}
        self.reports = set()

    def acquire(self): return self
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    def transaction(self): return self

    async def fetchrow(self, sql, fid): return self.fights.get(fid)

    async def fetchval(self, sql, fid, rep, other):
        if (fid, rep) in self.reports:
            return None
        self.reports.add((fid, rep))
        return len(self.reports)


def test_a_report_is_about_the_other_player_once_per_person_and_only_for_players_in_the_fight(monkeypatch):
    conn = ReportConn()

    async def pool():
        return conn
    monkeypatch.setattr(cr, "_pool", pool)
    assert run(cr.report(1, 1)) == "ok" and run(cr.report(1, 1)) == "again"
    assert run(cr.report(1, 2)) == "ok"
    assert run(cr.report(1, 77)) == "gone" and run(cr.report(99, 1)) == "gone"


def test_blocked_screen_fits_and_shows_names_not_ids():
    ids = list(range(10**17, 10**17 + 30))
    board = SimpleNamespace(client=SimpleNamespace(get_user=lambda i: None), _show=AsyncMock())
    view = vb.BlockedView(SimpleNamespace(interaction_check=AsyncMock()), board, ids)
    assert _count(view) <= 40
    vis.attach_back(view)
    assert _count(view) <= 40
    assert str(10**17) not in vb.blocked_text(["Mia"] * 25)
    assert "haven't blocked anyone" in vb.blocked_text([])


def test_result_screen_buttons_fit_and_board_has_a_blocked_button():
    board = SimpleNamespace(_show=AsyncMock())
    rows = vb.report_rows(5, 2, board)
    labels = [b.label for b in rows[0].children]
    assert labels == ["Report this fight", "Block player", "Back to board"]
    from tests.unit.test_cat_board_phase4b import ready, req
    view = vb.BoardView(SimpleNamespace(interaction_check=AsyncMock()), [ready(f"Cat{i}", i) for i in range(1, 26)])
    view.items, view.pages, view.client = [req(i) for i in range(1, 6)], 3, SimpleNamespace(get_user=lambda i: None)
    view.req_id = 1
    view._build()
    assert "Blocked" in [getattr(c, "label", None) for c in view.walk_children()]
    assert _count(view) <= 40
    vis.attach_back(view)
    assert _count(view) <= 40


def test_the_setup_screen_has_both_fight_toggles_and_still_fits():
    import discord
    from discord_bot.cogs import _views_cat_setup as vs
    src = (ROOT / "discord_bot/cogs/_views_cat_setup.py").read_text()
    assert "_on_cross" in src and "_on_show_name" in src
    text = vs.render_text(cs.CatConfig(guild_id=1, fights_cross_server=True), False, "", "")
    assert "cross-server ON" in text and "board OFF" in text and len(text) < 4000
