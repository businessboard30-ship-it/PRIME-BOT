"""Cat market, leaderboard, one-cat-one-owner across bots, cross-bot trade (play coins only)."""
import asyncio
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from modules import cat_adopt as ca
from modules import cat_board as cb
from modules import cat_market as cm

ROOT = Path(__file__).resolve().parents[2]


def run(c):
    return asyncio.run(c)


class Tx:
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False


class Acq:
    def __init__(self, conn): self.conn = conn
    async def __aenter__(self): return self.conn
    async def __aexit__(self, *a): return False


class Pool:
    def __init__(self, conn): self.conn = conn
    def acquire(self): return Acq(self.conn)


def use(monkeypatch, mod, conn):
    async def _p(): return Pool(conn)
    monkeypatch.setattr(mod, "_pool", _p)


# -- pure rules ----------------------------------------------------------------------------------
@pytest.mark.parametrize("raw,price", [("1500", 1500), ("1,500", 1500), ("1.5k", 1500), (" 20 ", 20), ("5", None),
                                       ("2m", None), ("abc", None), ("-10", None), ("", None), ("1e9", None)])
def test_parse_price(raw, price):
    assert cm.parse_price(raw) == price


def test_board_text_names_people_without_pinging_and_shows_my_rank():
    rows = [{"owner_id": 1, "cats": 5, "score": 40, "legendary": 1}, {"owner_id": 2, "cats": 3, "score": 9, "legendary": 0}]
    text = cb.format_board(rows, "collectors", my_rank=42)
    assert "<@1>" in text and "1 legendary" in text and "**You:** #42" in text
    assert "coins" in cb.format_board([{"owner_id": 1, "score": 12345}], "richest")
    assert "Nobody" in cb.format_board([], "collectors")


def test_migration_resets_duplicates_keeps_earliest_and_wires_in():
    sql = (ROOT / "database/migrations/040_cat_market.sql").read_text()
    assert "DELETE FROM cat_adoptions" in sql and "(a.adopted_at, a.id) > (b.adopted_at, b.id)" in sql
    assert "UNIQUE INDEX IF NOT EXISTS ux_cat_adoptions_pool" in sql and "cat_listings" in sql
    db = (ROOT / "database.py").read_text()
    assert "040_cat_market.sql" in db and int(re.search(r'^SCHEMA_VERSION = "(\d+)"', db, re.M).group(1)) >= 85


# -- one cat, one owner across bots --------------------------------------------------------------
class AdoptConn:
    def __init__(self, already_owned: bool):
        self.already_owned, self.closed = already_owned, False
    def transaction(self): return Tx()
    async def fetchval(self, sql, *a):
        if "COUNT(*)" in sql: return 0
        if "FROM cat_adoptions WHERE pool_id" in sql: return 1 if self.already_owned else None
    async def fetchrow(self, sql, *a):
        if sql.startswith("UPDATE cat_spawns"): return {"pool_id": 7}
        if sql.startswith("INSERT INTO cat_adoptions"):
            return {"id": 1, "owner_id": a[0], "clone_id": a[1], "pool_id": a[2], "name": a[3], "rarity": a[4],
                    "trait": a[5], "adopted_at": None, "traded_count": 0}
    async def execute(self, sql, *a):
        if "adopted_by = 0" in sql: self.closed = True


def test_a_picture_already_owned_on_another_bot_cannot_be_adopted_again(monkeypatch):
    conn = AdoptConn(already_owned=True)
    use(monkeypatch, ca, conn)
    res = run(ca.adopt(1, 10, 99, 5))                      # tapped on a clone
    assert res.status == "taken" and conn.closed           # the spawn is closed for everybody


def test_a_free_picture_is_adopted_normally(monkeypatch):
    use(monkeypatch, ca, AdoptConn(already_owned=False))
    assert run(ca.adopt(1, 10, None, 5)).status == "ok"


# -- cross-bot trade: cats land in the new owner's bot -------------------------------------------
class TradeConn:
    def __init__(self, rows, listed=False, held=0, away=False):
        self.rows, self.listed, self.held, self.away, self.updates = rows, listed, held, away, []
    def transaction(self): return Tx()
    async def fetch(self, sql, ids): return [r for r in self.rows if r["id"] in ids]
    async def fetchval(self, sql, *a):
        if "FROM cat_listings" in sql: return 1 if self.listed else None
        if "FROM cat_outings" in sql: return 1 if self.away else None
        return self.held
    async def execute(self, sql, cat_id, owner, clone): self.updates.append((cat_id, owner, clone))


def test_swap_between_main_bot_and_a_clone_moves_each_cat_into_the_others_bot(monkeypatch):
    conn = TradeConn([{"id": 1, "owner_id": 10, "clone_id": None}, {"id": 2, "owner_id": 20, "clone_id": 7}])
    use(monkeypatch, ca, conn)
    assert run(ca.trade(1, 10, 2, 20, None, taker_clone_id=7)) == "ok"
    assert (1, 20, 7) in conn.updates and (2, 10, None) in conn.updates


def test_trade_refuses_wrong_bot_or_a_cat_that_is_for_sale(monkeypatch):
    rows = [{"id": 1, "owner_id": 10, "clone_id": None}]
    use(monkeypatch, ca, TradeConn(rows))
    assert run(ca.trade(1, 10, None, 20, 5)) == "gone"            # giver says bot 5, the cat lives on the main bot
    use(monkeypatch, ca, TradeConn(rows, listed=True))
    assert run(ca.trade(1, 10, None, 20, None)) == "gone"
    use(monkeypatch, ca, TradeConn(rows, held=ca.MAX_CATS))
    assert run(ca.trade(1, 10, None, 20, None)) == "limit"


def test_a_cat_out_on_a_walk_or_heist_cannot_be_traded():
    conn = TradeConn([{"id": 1, "owner_id": 10, "clone_id": None}], away=True)
    import modules.cat_adopt as mod
    class M:  # route the pool to the fake connection
        pass
    from unittest.mock import patch
    with patch.object(mod, "_pool", new=lambda: _pool_for(conn)):
        assert run(ca.trade(1, 10, None, 20, None)) == "gone"
    assert conn.updates == []


async def _pool_for(conn):
    class Acq:
        async def __aenter__(self): return conn
        async def __aexit__(self, *a): return False
    class P:
        def acquire(self): return Acq()
    return P()


def test_forty_cats_fit_in_a_collection():
    assert ca.MAX_CATS >= 40


# -- selling and buying --------------------------------------------------------------------------
class MarketConn:
    def __init__(self, status="open", seller=10, seller_clone=None, owner=10, owner_clone=None, held=0, coins=500, price=300):
        self.listing = {"id": 1, "adoption_id": 4, "seller_id": seller, "seller_clone_id": seller_clone, "price": price,
                        "status": status, "guild_id": 1, "channel_id": 2, "message_id": 3}
        self.cat = {"owner_id": owner, "clone_id": owner_clone}
        self.held, self.coins, self.log = held, coins, []
    def transaction(self): return Tx()
    async def fetchrow(self, sql, *a):
        if "FROM cat_listings WHERE id" in sql and "FOR UPDATE" in sql: return self.listing
        if "FROM cat_adoptions WHERE id" in sql: return self.cat
        if sql.lstrip().startswith("SELECT l.*"): return None
    async def fetchval(self, sql, *a):
        if "COUNT(*)" in sql: return self.held
        if sql.startswith("UPDATE cat_wallets"):
            if self.coins >= a[2]:
                self.coins -= a[2]; self.log.append(("debit", a[0], a[2])); return self.coins
            return None
    async def execute(self, sql, *a): self.log.append((sql.split()[0] + " " + sql.split()[1], a))


def test_buy_pays_the_seller_and_moves_the_cat(monkeypatch):
    conn = MarketConn()
    use(monkeypatch, cm, conn)
    async def fake_get(i): return "L"
    monkeypatch.setattr(cm, "get", fake_get)
    res = run(cm.buy(1, 20, 7))                            # buyer on clone 7, seller on the main bot
    assert res.status == "ok" and res.listing == "L"
    assert ("debit", 20, 300) in conn.log
    credit = [x for x in conn.log if x[0] == "INSERT INTO" and x[1][0] == 10 and x[1][1] is None and x[1][2] == 300]
    assert credit                                          # the seller's wallet on the seller's bot gets exactly the price
    moved = [x for x in conn.log if x[0] == "UPDATE cat_adoptions"][0]
    assert moved[1] == (4, 20, 7)                          # cat lands in the buyer's bot


@pytest.mark.parametrize("kw,status", [(dict(status="sold"), "gone"), (dict(seller=20), "own"), (dict(coins=10), "poor"),
                                       (dict(held=ca.MAX_CATS), "limit"), (dict(owner=99), "gone")])
def test_buy_refusals_move_nothing(monkeypatch, kw, status):
    conn = MarketConn(**kw)
    use(monkeypatch, cm, conn)
    res = run(cm.buy(1, 20, None))
    assert res.status == status
    assert not [x for x in conn.log if x[0] in ("UPDATE cat_adoptions",)] and not any(x[0] == "debit" for x in conn.log)
