# path: tests/unit/test_card_economy_migrations_wired.py
"""Migrations 088-092 only run if database._create_tables executes them, AFTER the card_* base tables exist."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DB = (ROOT / "database.py").read_text()
FILES = ["088_card_economy_core.sql", "089_card_boosts.sql", "090_card_trading.sql",
         "091_card_coin_games.sql", "092_card_profiles.sql", "093_card_levelup_rewards.sql",
         "094_card_trade_drafts.sql"]


def test_every_card_migration_exists_and_is_wired_in_order():
    pos = [DB.index(f'"{f}"') for f in FILES]
    assert pos == sorted(pos)
    for f in FILES:
        assert (ROOT / "database" / "migrations" / f).exists()


def test_wired_after_base_card_tables():
    base = DB.index("card_watches_user_clone_card_key")
    assert DB.index('"088_card_economy_core.sql"') > base
    assert DB.index("CREATE TABLE IF NOT EXISTS card_coins") < DB.index('"088_card_economy_core.sql"')


def test_migrations_are_additive_only():
    for f in FILES:
        sql = (ROOT / "database" / "migrations" / f).read_text().upper()
        assert not re.search(r"\bDROP\s+(TABLE|COLUMN)\b|\bTRUNCATE\b|\bDELETE\s+FROM\b", sql), f
