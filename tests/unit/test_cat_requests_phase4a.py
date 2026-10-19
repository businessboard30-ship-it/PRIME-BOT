import asyncio
import re
from pathlib import Path

from modules import cat_arena, cat_battle, cat_requests as cr


def run(c):
    return asyncio.run(c)


# ── pure rules ──
def test_clamp_and_hours_error():
    assert cr.clamp_hours(0) == 1 and cr.clamp_hours(99) == 24 and cr.clamp_hours("x") == cr.DEFAULT_HOURS
    assert cr.hours_error(6) is None and cr.hours_error(0) == "hours" and cr.hours_error(25) == "hours"
    assert cr.hours_error(None) == "hours"


def test_power_range_matches_the_band_rule_exactly():
    for p in range(1, 1500):
        lo, hi = cr.power_range(p)
        for q in {max(1, lo - 1), lo, hi, hi + 1, p}:
            assert cr.in_band(q, p) == (lo <= q <= hi), (p, q, lo, hi)


def test_visible_to_rules():
    kw = dict(poster_id=1, viewer_id=2, poster_guild=10, viewer_guild=10, cross_server=False)
    assert cr.visible_to(**kw)
    assert not cr.visible_to(**{**kw, "viewer_id": 1})                       # never your own
    assert not cr.visible_to(**{**kw, "viewer_guild": 11})                   # other server, cross-server off
    assert cr.visible_to(**{**kw, "viewer_guild": 11, "cross_server": True})
    assert not cr.visible_to(**{**kw, "blocked_pairs": [1]})                 # blocked
    assert not cr.visible_to(**{**kw, "viewer_guild": None})


def test_server_label_and_paging():
    assert cr.board_server_label(False, "Cats") is None and cr.board_server_label(True, None) is None
    assert cr.board_server_label(True, "  Cat   Club ") == "Cat Club"
    assert len(cr.board_server_label(True, "x" * 90)) == 40
    assert cr.page_slice(0, 3) == (0, 1, 0) and cr.page_slice(11, 99) == (2, 3, 10) and cr.page_slice(5, 0) == (0, 1, 0)


# ── transactions on an in-memory fake ──
class Fake:
    def __init__(self):
        self.coins = {1: 10_000, 2: 10_000}
        self.requests = {}
        self.next_id = 1
        self.arena = {1: {"arena_on": True, "created_at": None}}
        self.cat_owner = {7: 1, 8: 1}
        self.snap = None

    # pool / connection surface
    def acquire(self): return self
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False

    def transaction(self):
        f = self

        class T:
            async def __aenter__(s):
                import copy
                f.snap = copy.deepcopy((f.coins, f.requests, f.next_id))
            async def __aexit__(s, et, ev, tb):
                if et is not None:
                    f.coins, f.requests, f.next_id = f.snap
                return False
        return T()

    async def fetchrow(self, sql, *a):
        if "FROM cat_arena WHERE" in sql:
            return self.arena.get(a[0])
        if "FROM cat_adoptions WHERE id" in sql:
            return {"id": a[0], "owner_id": self.cat_owner[a[0]], "clone_id": None} if a[0] in self.cat_owner else None
        if "FROM cat_cat_arena_daily" in sql or "cat_arena_daily" in sql:
            return None
        if sql.startswith("UPDATE cat_wallets SET coins = coins - "):
            o, _, w = a
            if self.coins.get(o, 0) >= w:
                self.coins[o] -= w
                return {"coins": self.coins[o]}
            return None
        if "FOR UPDATE SKIP LOCKED" in sql:
            r = self.requests.get(a[0])
            return r if r and r["status"] == "open" and r["due"] else None
        if "FROM cat_fight_requests WHERE id = $1 FOR UPDATE" in sql:
            return self.requests.get(a[0])
        raise AssertionError(sql)

    async def fetchval(self, sql, *a):
        if "FROM cat_listings" in sql or "FROM cat_outings" in sql or "FROM cat_effects" in sql:
            return None
        if "FROM cat_fight_requests WHERE cat_id" in sql:
            return 1 if any(r["cat_id"] == a[0] and r["status"] in ("open", "accepted", "locked") for r in self.requests.values()) else None
        if "COUNT(*) FROM cat_fight_requests" in sql:
            return sum(1 for r in self.requests.values() if r["poster_id"] == a[0] and r["status"] == "open")
        if sql.startswith("INSERT INTO cat_fight_requests"):
            rid = self.next_id
            self.next_id += 1
            self.requests[rid] = dict(id=rid, poster_id=a[3], cat_id=a[4], wager=a[5], status="open", due=False)
            return rid
        raise AssertionError(sql)

    async def execute(self, sql, *a):
        if sql.startswith("UPDATE cat_fight_requests SET status"):
            st = "cancelled" if "'cancelled'" in sql else "expired"
            self.requests[a[0]]["status"] = st
        elif sql.startswith("UPDATE cat_wallets SET coins = coins + "):
            self.coins[a[0]] += a[2]
        else:
            raise AssertionError(sql)

    async def fetch(self, sql, *a):
        if "status = 'open' AND expires_at <= NOW()" in sql:
            return [{"id": i} for i, r in self.requests.items() if r["status"] == "open" and r["due"]]
        raise AssertionError(sql)


def _patch(monkeypatch, fake):
    async def gp(): return fake
    monkeypatch.setattr(cr, "_pool", gp)


def test_post_escrows_the_wager_and_rejects_bad_requests(monkeypatch):
    f = Fake(); _patch(monkeypatch, f)
    r = run(cr.post(1, None, 7, 500, 100))
    assert r.status == "ok" and r.coins == 9_500 and f.coins[1] == 9_500 and f.requests[r.request_id]["wager"] == 500
    assert run(cr.post(1, None, 7, 500, 100)).status == "cat"                # the cat is held by its open request
    assert run(cr.post(1, None, 8, 700, 100)).status == "ladder"
    assert run(cr.post(1, None, 8, 500, 100, hours=0)).status == "hours"
    assert run(cr.post(2, None, 8, 500, 100)).status == "me_off"
    assert run(cr.post(1, None, 99, 500, 100)).status == "cat"
    assert f.coins[1] == 9_500


def test_post_limits_open_requests_and_checks_coins(monkeypatch):
    f = Fake(); _patch(monkeypatch, f)
    f.cat_owner.update({9: 1})
    assert run(cr.post(1, None, 7, 500, 100)).status == "ok"
    assert run(cr.post(1, None, 8, 500, 100)).status == "ok"
    assert run(cr.post(1, None, 9, 500, 100)).status == "max_open"
    f2 = Fake(); _patch(monkeypatch, f2); f2.coins[1] = 100
    assert run(cr.post(1, None, 7, 500, 100)).status == "poor" and f2.coins[1] == 100 and not f2.requests


def test_cancel_refunds_once_and_only_for_the_poster(monkeypatch):
    f = Fake(); _patch(monkeypatch, f)
    rid = run(cr.post(1, None, 7, 2_500, 100)).request_id
    assert f.coins[1] == 7_500
    assert run(cr.cancel(rid, 2)).status == "not_yours" and f.coins[1] == 7_500
    assert run(cr.cancel(rid, 1)).status == "ok" and f.coins[1] == 10_000
    assert run(cr.cancel(rid, 1)).status == "gone" and f.coins[1] == 10_000     # a second tap can't refund again


def test_expiry_refunds_once_even_if_swept_twice(monkeypatch):
    f = Fake(); _patch(monkeypatch, f)
    rid = run(cr.post(1, None, 7, 500, 100)).request_id
    assert run(cr.expire_due()) == 0 and f.coins[1] == 9_500                    # not due yet
    f.requests[rid]["due"] = True
    assert run(cr.expire_due()) == 1 and f.coins[1] == 10_000
    assert run(cr.expire_due()) == 0 and f.coins[1] == 10_000


def test_board_query_enforces_band_blocks_and_cross_server():
    src = Path(cr.__file__).read_text()
    board = src[src.index("async def board("):src.index("# ── block list")]
    assert "r.power BETWEEN" in board and "cat_blocks" in board and "r.cross_server" in board
    assert "r.poster_id <> $2" in board and "r.expires_at > NOW()" in board
    assert "ORDER BY r.id DESC LIMIT" in board


def test_migration_is_additive_only():
    sql = (Path(cr.__file__).parent.parent / "database" / "migrations" / "060_cat_requests.sql").read_text().upper()
    assert "DROP " not in sql and "ALTER TABLE" not in sql and "DELETE " not in sql
    assert len(re.findall(r"CREATE TABLE IF NOT EXISTS", sql)) == 2


def test_board_never_selects_dm_or_secret_columns():
    src = Path(cr.__file__).read_text()
    assert "_SELECT" in src and "accepter_id" not in src[src.index("_SELECT = ("):src.index("async def board(")]
