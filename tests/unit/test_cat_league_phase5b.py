"""Cat League slice B: promotions, season close, points hook, League screen budget, wiring. Play coins only."""
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from discord_bot.cogs import _cat_visibility as vis
from discord_bot.cogs import _views_cat_league as vl
from modules import cat_league as cl
from tests.unit.test_cat_board_phase4b import ready  # noqa: F401

ROOT = Path(__file__).resolve().parents[2]


def run(c):
    return asyncio.run(c)


def test_top_fifth_up_bottom_fifth_down_with_limits():
    ranked = [(i, 100 - i) for i in range(1, 11)]               # 10 players, best first
    out = cl.moves(ranked, 1)
    assert out[1] == 2 and out[2] == 2 and out[9] == 0 and out[10] == 0 and out[5] == 1
    assert cl.moves(ranked[:4], 1) == {1: 1, 2: 1, 3: 1, 4: 1}  # under 5 entrants: nobody moves
    assert cl.moves(ranked, 4)[1] == 4 and cl.moves(ranked, 0)[10] == 0     # Diamond stays, Bronze stays
    assert cl.moves([(1, 0), (2, 0), (3, 0), (4, 0), (5, 0)], 1)[1] == 1    # no promotion without a point


class Conn:
    def __init__(self, entered=(1, 2), season=7):
        self.entered, self.season, self.pts = set(entered), season, {}

    async def fetchval(self, sql, *a):
        if "FROM cat_league_seasons" in sql:
            return self.season
        if "COUNT(*)" in sql:
            return len(self.entered & set(a[1]))
        if sql.startswith("UPDATE cat_league_entries"):
            self.pts[a[1]] = self.pts.get(a[1], 0) + a[2]
            return 1


def test_points_go_to_both_players_only_when_both_are_entered():
    c = Conn()
    run(cl.play_points(c, -1, 1, 2))
    assert c.pts == {1: 3, 2: 1}
    c = Conn(entered=(1,))
    run(cl.play_points(c, -1, 1, 2))
    assert c.pts == {}
    c = Conn(season=None)
    run(cl.play_points(c, -1, 1, 2))
    assert c.pts == {}


def test_a_league_error_never_breaks_a_fight():
    class Bad:
        async def fetchval(self, *a): raise RuntimeError("boom")
    run(cl.play_points(Bad(), -1, 1, 2))


def test_league_screen_fits_and_shows_names_not_ids():
    view = vl.LeagueView(SimpleNamespace(interaction_check=AsyncMock()))
    view.client = SimpleNamespace(get_user=lambda i: None)
    view.entry = cl.Entry(1, 10**17, 2, 40, 9, 3)
    view.top = [cl.Entry(1, 10**17 + i, 2, 40 - i) for i in range(10)]
    view.names = ["A" * 32] * 10
    view.note, view.ends_ts = "x" * 100, 1_900_000_000
    view._build()
    assert sum(1 for _ in view.walk_children()) <= 40
    vis.attach_back(view)
    assert sum(1 for _ in view.walk_children()) <= 40
    text = vl.league_text(view.entry, view.names, 1_900_000_000, None, view.top)
    assert str(10**17) not in text and len(text) < 4000 and "Gold" in text
    assert "not in this season" in vl.league_text(None, [], None)


def test_wiring_and_migration():
    assert "play_points" in (ROOT / "modules/cat_arena.py").read_text()
    assert "_league_sweep" in (ROOT / "discord_bot/cogs/cat.py").read_text()
    assert "_views_cat_league" in (ROOT / "discord_bot/cogs/_views_cat_arena.py").read_text()
    sql = (ROOT / "database/migrations/063_cat_league_next.sql").read_text()
    assert "CREATE TABLE IF NOT EXISTS cat_league_next" in sql and "DROP" not in sql.upper()
    assert "063_cat_league_next.sql" in (ROOT / "database.py").read_text()
