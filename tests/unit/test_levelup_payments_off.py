# path: tests/unit/test_levelup_payments_off.py
import asyncio

import config
from discord_bot.cogs import _views_leveling_boost as b


def test_paid_levelups_off_by_default(monkeypatch):
    assert config.LEVELUP_PAYMENTS_ENABLED is False
    assert b._available_bundles() == {}
    assert asyncio.run(b.pass_offered(None)) is False


def test_flag_restores_old_shop(monkeypatch):
    monkeypatch.setattr(config, "LEVELUP_PAYMENTS_ENABLED", True)
    monkeypatch.setitem(config.GUMROAD_PRODUCT_LINKS, next(iter(config.XP_BOOST_BUNDLES)), "https://x")
    assert b._available_bundles()
