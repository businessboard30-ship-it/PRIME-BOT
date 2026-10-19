"""General Marketplace data layer (R1a): fee, listing rules, one-transaction sale, rollback, expiry, and that a new item
type is only one adapter. Play coins only. Uses an in-memory fake database."""
import asyncio
import copy
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from modules import cat_adopt as ca
from modules import cat_market as cm
from modules import market as mk

ROOT = Path(__file__).resolve().parents[2]
PRICE = cm.price_range("common", 0)[0]


def run(c):
    return asyncio.run(c)


class MarketConn:
    """Cats 1 and 2 belong to player 10 (seller). Player 20 (buyer) owns cat 3."""

    def __init__(self, buyer_coins=100_000, seller_coins=1_000):
        self.cats = {i: dict(id=i, owner_id=o, clone_id=None, rarity="common", cat_rank=0, name=f"Cat{i}") for i, o in ((1, 10), (2, 10), (3, 20))}
        self.wallets = {10: seller_coins, 20: buyer_coins}
        self.listings, self.next_id = {}, 1
        self.old_listed, self.out, self.held = set(), set(), set()
        self.widgets = {}                       # ref -> owner (for the extra item type)
        self.fail_transfer = False

    @asynccontextmanager
    async def transaction(self):
        snap = copy.deepcopy((self.cats, self.wallets, self.listings, self.widgets))
        try:
            yield
        except BaseException:
            self.cats, self.wallets, self.listings, self.widgets = snap
            raise

    def open_rows(self):
        return [l for l in self.listings.values() if l["status"] == "open"]

    async def fetchrow(self, sql, *a):
        if "FROM cat_adoptions WHERE id" in sql:
            return self.cats.get(a[0])
        if "FROM market_listings WHERE id" in sql:
            l = self.listings.get(a[0])
            return dict(l, is_expired=l["expired"]) if l else None

    async def fetch(self, sql, *a):
        if "FROM cat_wallets" in sql:
            return [{"owner_id": u} for u in sorted(a[0])]
        if "UPDATE market_listings SET status = 'expired'" in sql:
            hit = [l for l in self.open_rows() if l["expired"]]
            for l in hit:
                l["status"] = "expired"
            return [{"id": l["id"]} for l in hit]
        if "FROM market_listings WHERE status = 'open' AND expires_at" in sql:
            rows = sorted((l for l in self.open_rows() if not l["expired"] and (a[0] is None or l["item_type"] == a[0])), key=lambda l: (l["price"], l["id"]))
            return rows[a[2]: a[2] + a[1]]
        if "FROM cat_adoptions WHERE id = ANY" in sql:
            return [self.cats[i] for i in a[0] if i in self.cats]
        return []

    async def fetchval(self, sql, *a):
        if "FROM cat_listings" in sql:
            return 1 if a[0] in self.old_listed else None
        if "FROM cat_outings" in sql:
            return 1 if a[0] in self.out else None
        if "FROM cat_fight_requests" in sql:
            return 1 if a[0] in self.held else None
        if "SELECT 1 FROM market_listings WHERE item_type" in sql:
            return 1 if any(l["item_type"] == a[0] and l["item_ref"] == a[1] for l in self.open_rows()) else None
        if "SELECT COUNT(*) FROM market_listings WHERE seller_id" in sql:
            return sum(1 for l in self.open_rows() if l["seller_id"] == a[0])
        if "SELECT COUNT(*) FROM market_listings WHERE status = 'open' AND expires_at" in sql:
            return sum(1 for l in self.open_rows() if not l["expired"] and (a[0] is None or l["item_type"] == a[0]))
        if "SELECT COUNT(*) FROM cat_adoptions WHERE owner_id" in sql:
            return sum(1 for c in self.cats.values() if c["owner_id"] == a[0])
        if sql.startswith("INSERT INTO market_listings"):
            i = self.next_id
            self.next_id += 1
            self.listings[i] = dict(id=i, item_type=a[0], item_ref=a[1], seller_id=a[2], seller_clone_id=a[3], price=a[4], fee=0,
                                    guild_id=a[5], channel_id=a[6], message_id=None, status="open", expired=False)
            return i
        if "UPDATE cat_wallets SET coins = coins - $3" in sql:
            if self.wallets.get(a[0], 0) >= a[2]:
                self.wallets[a[0]] -= a[2]
                return self.wallets[a[0]]
            return None
        if "UPDATE market_listings SET status = 'cancelled'" in sql:
            l = self.listings.get(a[0])
            if l and l["seller_id"] == a[1] and l["status"] == "open":
                l["status"] = "cancelled"
                return l["id"]
        return None

    async def execute(self, sql, *a):
        if sql.startswith("INSERT INTO cat_wallets"):
            self.wallets[a[0]] = self.wallets.get(a[0], 0) + a[2]
        elif sql.startswith("UPDATE cat_adoptions SET owner_id"):
            if self.fail_transfer:
                raise RuntimeError("boom")
            self.cats[a[0]]["owner_id"] = a[1]
        elif "SET status = 'sold'" in sql:
            l = self.listings[a[0]]
            l.update(status="sold", buyer_id=a[1], fee=a[3])
        elif "SET status = 'void'" in sql:
            self.listings[a[0]]["status"] = "void"
        elif "SET status = 'expired'" in sql and "WHERE id = $1" in sql:
            self.listings[a[0]]["status"] = "expired"


@pytest.fixture
def db(monkeypatch):
    def make(**kw):
        conn = MarketConn(**kw)

        class Pool:
            @asynccontextmanager
            async def acquire(self):
                yield conn

        async def get_pool():
            return Pool()
        monkeypatch.setattr(mk, "_pool", get_pool)
        return conn
    return make


def listed(conn, cat=1, price=PRICE):
    st, l = run(mk.create_listing("cat", cat, 10, None, price))
    assert st == "ok", st
    return l


# ---------- fee ----------
def test_fee_is_a_share_and_adds_up():
    assert mk.fee_for(1000) == 50 and mk.seller_gets(1000) == 950
    assert all(mk.fee_for(p) + mk.seller_gets(p) == p for p in (10, 99, 1234, 50_000_000))


# ---------- listing rules ----------
def test_list_a_cat_and_the_rules(db):
    conn = db()
    l = listed(conn)
    assert l.item_type == "cat" and l.item_ref == 1 and l.status == "open"
    assert run(mk.create_listing("cat", 1, 10, None, PRICE))[0] == "listed"
    assert run(mk.create_listing("cat", 3, 10, None, PRICE))[0] == "not_yours"
    assert run(mk.create_listing("cat", 2, 10, None, 1))[0] == "bad_price"
    assert run(mk.create_listing("cat", 2, 10, None, mk.MAX_PRICE + 1))[0] == "bad_price"
    assert run(mk.create_listing("cat", 2, 10, None, cm.price_range("common", 0)[1] + 1))[0] == "out_of_range"
    assert run(mk.create_listing("sofa", 2, 10, None, PRICE))[0] == "unknown_type"


def test_holds_keep_a_cat_off_the_market(db):
    conn = db()
    conn.out.add(1)
    conn.held.add(2)
    assert run(mk.create_listing("cat", 1, 10, None, PRICE))[0] == "away"        # on a walk or heist
    assert run(mk.create_listing("cat", 2, 10, None, PRICE))[0] == "away"        # held by a fight request
    conn.out.clear(), conn.held.clear()
    conn.old_listed.add(1)
    assert run(mk.create_listing("cat", 1, 10, None, PRICE))[0] == "listed"      # still on the old cat market


def test_a_seller_has_a_limit_of_open_listings(db):
    conn = db()
    for i in range(10, 10 + mk.MAX_OPEN_PER_SELLER):
        conn.cats[i] = dict(id=i, owner_id=10, clone_id=None, rarity="common", cat_rank=0, name="x")
        assert run(mk.create_listing("cat", i, 10, None, PRICE))[0] == "ok"
    assert run(mk.create_listing("cat", 1, 10, None, PRICE))[0] == "too_many"


# ---------- the sale ----------
def test_buy_moves_the_cat_pays_the_seller_and_the_fee_leaves_the_game(db):
    conn = db()
    l = listed(conn)
    before = sum(conn.wallets.values())
    res = run(mk.buy(l.id, 20, None))
    fee = mk.fee_for(PRICE)
    assert res.status == "ok" and res.paid == PRICE and res.fee == fee
    assert conn.cats[1]["owner_id"] == 20
    assert conn.wallets[20] == 100_000 - PRICE and conn.wallets[10] == 1_000 + PRICE - fee
    assert sum(conn.wallets.values()) == before - fee
    assert conn.listings[l.id]["status"] == "sold" and conn.listings[l.id]["fee"] == fee
    assert run(mk.buy(l.id, 20, None)).status == "gone"                           # a second tap


def test_buy_refusals_change_nothing(db):
    conn = db(buyer_coins=PRICE - 1)
    l = listed(conn)
    assert run(mk.buy(l.id, 10, None)).status == "own"
    assert run(mk.buy(l.id, 20, None)).status == "poor"
    assert conn.cats[1]["owner_id"] == 10 and conn.wallets == {10: 1_000, 20: PRICE - 1} and conn.listings[l.id]["status"] == "open"
    for i in range(100, 100 + ca.MAX_CATS):
        conn.cats[i] = dict(id=i, owner_id=20, clone_id=None, rarity="common", cat_rank=0, name="x")
    conn.wallets[20] = 100_000
    assert run(mk.buy(l.id, 20, None)).status == "limit"
    assert conn.cats[1]["owner_id"] == 10 and conn.wallets[20] == 100_000


def test_a_cat_that_left_the_seller_voids_the_listing(db):
    conn = db()
    l = listed(conn)
    conn.cats[1]["owner_id"] = 99
    assert run(mk.buy(l.id, 20, None)).status == "gone"
    assert conn.listings[l.id]["status"] == "void" and conn.wallets == {10: 1_000, 20: 100_000}


def test_a_cat_now_held_cannot_be_bought(db):
    conn = db()
    l = listed(conn)
    conn.held.add(1)
    assert run(mk.buy(l.id, 20, None)).status == "gone"
    assert conn.cats[1]["owner_id"] == 10 and conn.wallets == {10: 1_000, 20: 100_000}


def test_a_failure_part_way_rolls_back_the_coins(db):
    conn = db()
    l = listed(conn)
    conn.fail_transfer = True
    with pytest.raises(RuntimeError):
        run(mk.buy(l.id, 20, None))
    assert conn.wallets == {10: 1_000, 20: 100_000} and conn.cats[1]["owner_id"] == 10 and conn.listings[l.id]["status"] == "open"


# ---------- cancel, list, expiry ----------
def test_cancel_is_for_the_seller_only(db):
    conn = db()
    l = listed(conn)
    assert run(mk.cancel(l.id, 20)) is False and conn.listings[l.id]["status"] == "open"
    assert run(mk.cancel(l.id, 10)) is True and conn.listings[l.id]["status"] == "cancelled"


def test_expired_listings_are_hidden_swept_and_cannot_be_bought(db):
    conn = db()
    a, b = listed(conn, 1), listed(conn, 2)
    conn.listings[a.id]["expired"] = True
    rows, total = run(mk.list_open("cat"))
    assert [r.id for r in rows] == [b.id] and total == 1
    assert run(mk.buy(a.id, 20, None)).status == "gone" and conn.listings[a.id]["status"] == "expired"
    conn.listings[b.id]["expired"] = True
    assert run(mk.expire_due()) == 1 and conn.listings[b.id]["status"] == "expired"


def test_list_open_is_cheapest_first_and_can_filter_by_type(db):
    conn = db()
    hi, lo = listed(conn, 1, PRICE + 5), listed(conn, 2, PRICE)
    rows, total = run(mk.list_open())
    assert [r.id for r in rows] == [lo.id, hi.id] and total == 2
    assert run(mk.list_open("house"))[1] == 0


# ---------- a new item type is one adapter ----------
def test_a_new_item_type_is_only_an_adapter(db, monkeypatch):
    conn = db()
    conn.widgets[7] = 10

    class Widget(mk.ItemAdapter):
        item_type = "widget"

        async def check_listable(self, c, ref, seller, clone, price):
            return None if c.widgets.get(ref) == seller else "not_yours"

        async def check_owned(self, c, ref, seller, clone):
            return c.widgets.get(ref) == seller

        async def transfer(self, c, ref, buyer, clone):
            c.widgets[ref] = buyer

    monkeypatch.setitem(mk.ADAPTERS, "widget", Widget())
    st, l = run(mk.create_listing("widget", 7, 10, None, 500))
    assert st == "ok"
    assert run(mk.create_listing("widget", 8, 10, None, 500))[0] == "not_yours"
    assert run(mk.buy(l.id, 20, None)).status == "ok"
    assert conn.widgets[7] == 20 and conn.wallets[10] == 1_000 + mk.seller_gets(500)


# ---------- wiring ----------
def test_migration_is_additive_and_loaded():
    sql = (ROOT / "database/migrations/065_market_listings.sql").read_text()
    assert "CREATE TABLE IF NOT EXISTS market_listings" in sql and "DROP" not in sql.upper()
    assert "ux_market_listings_open" in sql
    assert "065_market_listings.sql" in (ROOT / "database.py").read_text()
