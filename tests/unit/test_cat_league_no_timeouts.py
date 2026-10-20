"""League screens answer Discord first (3 s limit) and season close is one batched statement. Play coins only."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from discord_bot.cogs import _views_cat_league as vl
from modules import cat_league as cl


def run(c):
    return asyncio.run(c)


def _inter(order):
    resp = SimpleNamespace(is_done=lambda: False)

    async def defer():
        order.append("defer")
        resp.is_done = lambda: True
    resp.defer = defer
    return SimpleNamespace(user=SimpleNamespace(id=7), response=resp, client=SimpleNamespace(get_user=lambda i: None),
                           edit_original_response=AsyncMock(side_effect=lambda **k: order.append("edit")), message=None)


def test_opening_and_refreshing_acknowledge_before_any_database_work(monkeypatch):
    order = []

    async def my_entry(*a):
        order.append("db")
        return None

    async def season_info(*a):
        order.append("db")
        return None
    monkeypatch.setattr(cl, "my_entry", my_entry)
    monkeypatch.setattr(cl, "season_info", season_info)
    monkeypatch.setattr(vl, "_clone_id", lambda c: None)
    view = vl.LeagueView(SimpleNamespace(interaction_check=AsyncMock()))
    run(view._refresh(_inter(order)))
    assert order[0] == "defer" and order[-1] == "edit" and "db" in order


def test_joining_acknowledges_before_the_join_transaction(monkeypatch):
    order = []

    async def join(*a):
        order.append("join")
        return cl.JoinResult("me_off")

    async def none(*a):
        return None
    monkeypatch.setattr(cl, "join", join)
    monkeypatch.setattr(cl, "my_entry", none)
    monkeypatch.setattr(cl, "season_info", none)
    monkeypatch.setattr(vl, "_clone_id", lambda c: None)
    view = vl.LeagueView(SimpleNamespace(interaction_check=AsyncMock()))
    run(view._join(_inter(order)))
    assert order.index("defer") < order.index("join")


class Conn:
    def __init__(self, n):
        self.rows = [{"owner_id": i, "division": 0, "points": 100 - i} for i in range(1, n + 1)]
        self.statements = []

    async def execute(self, sql, *a):
        self.statements.append((sql, a))

    async def fetch(self, sql, *a):
        return self.rows


def test_closing_a_big_season_writes_everyone_in_one_statement():
    conn = Conn(500)
    run(cl._close_season(conn, 1, -1))
    inserts = [s for s in conn.statements if s[0].startswith("INSERT INTO cat_league_next")]
    assert len(inserts) == 1 and len(conn.statements) == 2                       # the close UPDATE + one batched write
    _, args = inserts[0]
    owners, divs = args[1], args[2]
    assert len(owners) == 500 and len(divs) == 500
    assert divs[owners.index(1)] == 1 and divs[owners.index(500)] == 0 and divs[owners.index(250)] == 0   # top fifth up, Bronze can't drop
