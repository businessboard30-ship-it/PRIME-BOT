"""General Marketplace R1b: the cat market now runs on market_listings. Migration keeps ids and closes old rows, every cat
lock check also looks at the new table, the screens show the fee, expiry sweep is wired. Play coins only."""
import asyncio
import re
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from modules import cat_arena as ar
from modules import cat_market as cm
from modules import market

ROOT = Path(__file__).resolve().parents[2]


def run(c):
    return asyncio.run(c)


def test_migration_keeps_ids_closes_old_rows_and_is_loaded():
    sql = (ROOT / "database/migrations/066_market_cat_migrate.sql").read_text()
    assert "INSERT INTO market_listings (id," in sql and "ON CONFLICT (id) DO NOTHING" in sql          # ids kept, rerun safe
    assert "UPDATE cat_listings SET status = 'void'" in sql and "setval(" in sql
    assert "DROP" not in sql.upper() and "DELETE" not in sql.upper()
    assert "066_market_cat_migrate.sql" in (ROOT / "database.py").read_text()


@pytest.mark.parametrize("f", ["modules/cat_rank.py", "modules/cat_requests.py", "modules/cat_arena.py", "modules/cat_life.py",
                               "modules/cat_stats.py", "modules/cat_adopt.py", "modules/cat_care.py"])
def test_every_cat_lock_also_looks_at_the_new_market(f):
    src = (ROOT / f).read_text()
    assert src.count("market_listings") >= src.count("FROM cat_listings"), f
    assert "market_listings" in src


def test_no_cat_lock_is_left_looking_only_at_the_old_table():
    for f in (ROOT / "modules").glob("*.py"):
        if f.name in ("cat_market.py", "market.py"):
            continue
        src = f.read_text()
        for m in re.finditer(r"FROM cat_listings[^\"]*", src):
            window = src[m.start(): m.start() + 400]
            assert "market_listings" in window, f"{f.name}: {m.group(0)[:60]}"


def test_a_cat_on_the_new_market_is_busy(monkeypatch):
    class Conn:
        async def fetch(self, sql, *a):
            if "FROM market_listings" in sql:
                return [{"item_ref": 5}]
            return []

    class Pool:
        @asynccontextmanager
        async def acquire(self):
            yield Conn()

    async def get_pool():
        return Pool()
    monkeypatch.setattr(ar, "_pool", get_pool)
    assert run(ar.busy_ids([5, 6])) == {5}


def test_listing_post_shows_the_fee_and_the_expiry():
    from discord_bot.cogs import _views_cat_market as vm
    l = cm.Listing(1, 4, 10, None, 1000, 1, 2, 3, "open")
    e = run(vm.listing_embed(None, l))
    assert f"{cm.FEE_PERCENT}% market fee" in e.description and f"{cm.EXPIRY_DAYS} days" in e.description


def test_listing_from_row_reads_the_fee_when_there_is_one():
    row = {"id": 1, "adoption_id": 4, "seller_id": 10, "seller_clone_id": None, "price": 1000, "fee": 50, "guild_id": 1,
           "channel_id": 2, "message_id": 3, "status": "sold", "name": None}
    assert cm.Listing.from_row(row).fee == 50
    row.pop("fee")
    assert cm.Listing.from_row(row).fee == 0


def test_cat_market_numbers_come_from_the_one_market():
    assert cm.FEE_PERCENT == market.FEE_PERCENT and cm.EXPIRY_DAYS == market.EXPIRY_DAYS


def test_expiry_sweep_runs_on_the_main_bot_only():
    src = (ROOT / "discord_bot/cogs/cat.py").read_text()
    start = src.index("self._market_sweep.start()")
    assert src.rfind("clone_id\", None) is None", 0, start) > src.rfind("def ", 0, start) - 4000
    assert "_market_sweep.cancel()" in src and "market.expire_due_ids()" in src
