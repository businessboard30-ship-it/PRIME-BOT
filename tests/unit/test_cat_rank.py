"""Cat ranks, the clearer sell screens, and the trade-ID uniqueness migration (light, no database needed)."""
import re
import asyncio
from pathlib import Path

import pytest

from modules import cat_adopt as ca
from modules import cat_board, cat_rank as cr

ROOT = Path(__file__).resolve().parents[2]


def cat(rank=0, rarity="common", cid=7, name="Whiskers"):
    return ca.Cat(id=cid, owner_id=1, clone_id=None, pool_id=9, name=name, rarity=rarity, trait="Brave", cat_rank=rank)


# ---------- costs and badges ----------
def test_costs_scale_with_rank_and_rarity():
    assert [cr.cost_to_reach(r, "common") for r in range(0, 6)] == [0, 300, 900, 2400, 6000, 15000]
    assert cr.cost_to_reach(1, "uncommon") == 450 and cr.cost_to_reach(1, "rare") == 750
    assert cr.cost_to_reach(5, "legendary") == 75000
    assert cr.cost_to_reach(1, "weird") == 300                       # unknown rarity behaves like common


def test_next_cost_stops_at_the_top():
    assert cr.next_cost(cat(0)) == 300 and cr.next_cost(cat(4, "rare")) == 15000 * 0 + int(round(15000 * 2.5))
    assert cr.next_cost(cat(5)) == 17700 and cr.next_cost(cat(25)) is None and cr.next_cost(cat(99)) is None


def test_old_cats_default_to_rank_zero_and_junk_is_clamped():
    assert cat().cat_rank == 0
    assert cr.clamp(None) == 0 and cr.clamp("x") == 0 and cr.clamp(-3) == 0 and cr.clamp(42) == cr.MAX_RANK
    assert cr.badge(0).endswith("Rank 0 \u00b7 Newcomer") and cr.badge(5).endswith("Legend")


def test_from_row_works_with_and_without_the_rank_column():
    row = {"id": 1, "owner_id": 2, "clone_id": None, "pool_id": 3, "name": "A", "rarity": "rare", "trait": "t",
           "adopted_at": None, "traded_count": 0}
    assert ca.Cat.from_row(row).cat_rank == 0                         # a row from before the column existed
    assert ca.Cat.from_row({**row, "cat_rank": 3}).cat_rank == 3
    assert ca.Cat.from_row({**row, "cat_rank": None}).cat_rank == 0


def test_every_rank_is_worth_points_on_the_leaderboard():
    assert f"cat_rank * {cr.POINTS_PER_RANK}" in cat_board._COLLECTORS


# ---------- migration + schema ----------
def test_migration_adds_rank_for_all_cats_and_global_trade_id_uniqueness():
    sql = (ROOT / "database/migrations/041_cat_ranks.sql").read_text()
    assert "ADD COLUMN IF NOT EXISTS cat_rank SMALLINT NOT NULL DEFAULT 0" in sql
    assert "CREATE UNIQUE INDEX IF NOT EXISTS ux_cat_owner_ids_trade_global ON cat_owner_ids (trade_id)" in sql
    assert sql.index("DELETE FROM cat_owner_ids") < sql.index("ux_cat_owner_ids_trade_global")   # clashes cleared first
    db = (ROOT / "database.py").read_text()
    assert "041_cat_ranks.sql" in db and int(re.search(r'^SCHEMA_VERSION = "(\d+)"', db, re.M).group(1)) >= 86


# ---------- the sell screens say which cat it is ----------
def test_sell_modal_names_the_cat():
    from discord_bot.cogs._views_cat_market import SellModal

    async def make():
        return SellModal(cat(name="Mr Fluffington The Third Of His Name", cid=1234))
    m = asyncio.run(make())
    assert len(m.title) <= 45 and "#1234" in m.title or "Mr Fluff" in m.title
    assert "Mr Fluff" in m.price.label and len(m.price.label) <= 45
    assert m.price in m.children


def test_cat_card_and_listing_show_rank_and_cat_id():
    from discord_bot.cogs import _views_cat_adopt as va, _views_cat_market as vm
    c = cat(3, "rare", cid=42)

    async def go():
        card = await va.cat_embed_full(None, c, "x")          # the classic card, still used by the public posts
        return card
    card = asyncio.run(go())
    assert "Rank 3" in card.description and "#42" in card.description
    assert "Rank 3" in va._cat_body(c) and "#42" in va._cat_body(c)    # the V2 screens read the same facts


def test_trade_screen_keeps_the_picture_visible():
    src = (ROOT / "discord_bot/cogs/_views_cat_adopt.py").read_text()
    i = src.index("async def _trade")
    block = src[i:i + 1200]
    # V2: the cat card (picture + facts) is passed into the trade screen instead of an embed
    assert "embed=" not in block and "_trade_card_text" in block and "image=" in block and "cat #" in block
