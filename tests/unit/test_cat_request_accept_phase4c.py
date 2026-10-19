"""Request Board accept (fights plan, phase 4C): one transaction, double-accept race, refunds, locks, wiring.
Play coins only. Reuses the Phase 3 in-memory fake database and adds the request table."""
import asyncio
import copy
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from modules import cat_arena as ar
from modules import cat_requests as cr
from tests.unit.test_cat_arena_phase3 import FakeConn, NOW

from datetime import timedelta

ROOT = Path(__file__).resolve().parents[2]


def run(c):
    return asyncio.run(c)


class ReqConn(FakeConn):
    """Player 2 posted request 1 with cat 10 (wager 500 already held); player 1 answers with cat 11."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.fights = {}
        self.wallets[2] -= 500
        self.requests = {1: dict(id=1, clone_key=-1, guild_id=5, poster_id=2, cat_id=10, wager=500, cross_server=False,
                                 status="open", live=True, accepter_id=None, fight_id=None)}
        self.blocked = False
        self.held_by_other = set()

    @asynccontextmanager
    async def transaction(self):
        snap = copy.deepcopy((self.wallets, self.arena, self.daily, self.effects, self.fights, self.cats, self.requests, self.next_fight))
        try:
            yield
        except BaseException:
            (self.wallets, self.arena, self.daily, self.effects, self.fights, self.cats, self.requests, self.next_fight) = snap
            raise

    async def fetchrow(self, sql, *a):
        if "FROM cat_fight_requests WHERE id" in sql:
            r = self.requests.get(a[0])
            return dict(r) if r else None
        if sql.startswith("INSERT INTO cat_fights"):
            fid = self.next_fight
            self.next_fight += 1
            row = dict(id=fid, mode="request", clone_key=a[0], guild_id=a[1], challenger_id=a[2], challenged_id=a[3],
                       challenger_cat=a[4], challenged_cat=None, wager=a[5], fee=0, status="pending", seed=None, winner_id=None,
                       ended_by=None, snap=None, day=None, expires_at=NOW, settled_at=None)
            self.fights[fid] = row
            return dict(row)
        return await super().fetchrow(sql, *a)

    async def fetchval(self, sql, *a):
        if "FROM cat_blocks" in sql:
            return 1 if self.blocked else None
        if "FROM cat_fight_requests WHERE cat_id" in sql:
            return 1 if a[0] in self.held_by_other else None
        return await super().fetchval(sql, *a)

    async def execute(self, sql, *a):
        if sql.startswith("UPDATE cat_fight_requests SET status = $2"):
            if self.requests[a[0]]["status"] == "open":
                self.requests[a[0]]["status"] = a[1]
        elif sql.startswith("UPDATE cat_fight_requests SET status = 'settled'"):
            self.requests[a[0]].update(status="settled", accepter_id=a[1], fight_id=a[2])
        else:
            await super().execute(sql, *a)


@pytest.fixture
def db(monkeypatch):
    def make(**kw):
        conn = ReqConn(**kw)

        class Pool:
            @asynccontextmanager
            async def acquire(self):
                yield conn

        async def get_pool():
            return Pool()
        monkeypatch.setattr(cr, "_pool", get_pool)
        return conn
    return make


def test_accept_takes_only_the_accepters_wager_pays_the_winner_and_settles_the_request(db):
    conn = db()
    res = run(cr.accept(1, 1, None, 11, guild_id=5, seed=7))
    assert res.status == "ok" and res.wager == 500 and res.fee == 80 and res.payout == 920
    assert conn.wallets[res.winner_id] == 10_000 + 420 and conn.wallets[res.loser_id] == 10_000 - 500
    assert sum(conn.wallets.values()) == 20_000 - 80                        # both stakes in play, the 8% fee leaves the game
    r = conn.requests[1]
    assert r["status"] == "settled" and r["accepter_id"] == 1 and r["fight_id"] in conn.fights
    f = conn.fights[r["fight_id"]]
    assert f["status"] == "settled" and f["mode"] == "request" and f["challenger_id"] == 2 and f["challenged_id"] == 1
    assert f["seed"] == 7 and f["challenged_cat"] == 11 and f["winner_id"] == res.winner_id
    assert conn.cats[10]["owner_id"] == 2 and conn.cats[11]["owner_id"] == 1      # nobody loses a cat


def test_a_second_accept_finds_the_request_gone_and_moves_no_coins(db):
    conn = db()
    first = run(cr.accept(1, 1, None, 11, guild_id=5, seed=1))
    wallets = dict(conn.wallets)
    again = run(cr.accept(1, 1, None, 11, guild_id=5, seed=2))
    assert first.status == "ok" and again.status == "gone" and conn.wallets == wallets and len(conn.fights) == 1


def test_you_cannot_answer_your_own_request_or_an_expired_one(db):
    conn = db()
    assert run(cr.accept(1, 2, None, 10, guild_id=5)).status == "self"
    conn.requests[1]["live"] = False
    assert run(cr.accept(1, 1, None, 11, guild_id=5)).status == "gone"
    assert conn.wallets == {1: 10_000, 2: 9_500}


def test_blocked_other_server_and_out_of_band_accepts_change_nothing(db):
    conn = db()
    conn.blocked = True
    assert run(cr.accept(1, 1, None, 11, guild_id=5)).status == "seen"
    conn.blocked = False
    assert run(cr.accept(1, 1, None, 11, guild_id=99)).status == "seen"       # other server, cross-server off
    conn.cat_stats[11] = dict(strength=30, guard=30, speed=30, spirit=30)
    assert run(cr.accept(1, 1, None, 11, guild_id=5)).status == "band"
    assert conn.wallets == {1: 10_000, 2: 9_500} and conn.requests[1]["status"] == "open" and not conn.fights


def test_accepter_problems_leave_the_request_open_and_everything_untouched(db):
    conn = db(coins=(100, 10_000))
    assert run(cr.accept(1, 1, None, 11, guild_id=5)).status == "poor"
    conn.wallets[1] = 10_000
    conn.arena[1]["arena_on"] = False
    assert run(cr.accept(1, 1, None, 11, guild_id=5)).status == "me_off"
    conn.arena[1]["arena_on"] = True
    conn.held_by_other.add(11)
    assert run(cr.accept(1, 1, None, 11, guild_id=5)).status == "cat_you"
    assert conn.requests[1]["status"] == "open" and not conn.fights and conn.wallets[1] == 10_000


def test_when_the_poster_can_no_longer_fight_the_request_closes_and_their_wager_comes_back(db):
    conn = db()
    conn.listed.add(10)                                                       # poster put the cat up for sale meanwhile
    res = run(cr.accept(1, 1, None, 11, guild_id=5))
    assert res.status == "cat_them" and conn.requests[1]["status"] == "cancelled" and conn.wallets[2] == 10_000 and conn.wallets[1] == 10_000
    conn2 = db()
    conn2.arena[2]["arena_on"] = False
    assert run(cr.accept(1, 1, None, 11, guild_id=5)).status == "them_off"
    assert conn2.requests[1]["status"] == "cancelled" and conn2.wallets[2] == 10_000


def test_a_failure_mid_accept_rolls_everything_back(db):
    conn = db()
    real = conn.execute

    async def boom(sql, *a):
        if sql.startswith("UPDATE cat_fight_requests SET status = 'settled'"):
            raise RuntimeError("boom")                                          # the very last step of the transaction
        await real(sql, *a)
    conn.execute = boom
    with pytest.raises(RuntimeError):
        run(cr.accept(1, 1, None, 11, guild_id=5))
    assert conn.requests[1]["status"] == "open" and conn.wallets == {1: 10_000, 2: 9_500} and not conn.fights
    assert not conn.effects and conn.arena[1]["wins"] == 0 and conn.arena[2]["losses"] == 0


def test_cat_locks_are_wired_into_every_place_a_cat_can_move():
    for f in ("modules/cat_arena.py", "modules/cat_adopt.py", "modules/market.py", "modules/cat_life.py", "modules/cat_care.py"):
        assert "cat_fight_requests" in (ROOT / f).read_text(), f


def test_the_board_has_an_accept_button_and_fits_the_component_budget():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from discord_bot.cogs import _cat_visibility as vis
    from discord_bot.cogs import _views_cat_board as vb
    from tests.unit.test_cat_board_phase4b import ready, req, _count
    view = vb.BoardView(SimpleNamespace(interaction_check=AsyncMock()), [ready(f"Cat{i}", i) for i in range(1, 26)])
    view.items, view.pages, view.page, view.client = [req(i) for i in range(1, 6)], 9, 4, SimpleNamespace(get_user=lambda i: None)
    view.req_id, view.note = 1, "x" * 100
    view._build()
    labels = [getattr(c, "label", None) for c in view.walk_children()]
    assert "Accept" in labels
    accept = next(c for c in view.walk_children() if getattr(c, "label", None) == "Accept")
    assert not accept.disabled
    assert _count(view) <= 40
    vis.attach_back(view)
    assert _count(view) <= 40
    view.req_id = None
    view._build()
    assert next(c for c in view.walk_children() if getattr(c, "label", None) == "Accept").disabled
