"""Premium Plus: caps, product wiring and the migration."""
import asyncio
from pathlib import Path

import config
import gumroad_autocreate as ga
import gumroad_payments as gp
from modules import ai_features as ai
from payments_manual import UNLOCK_HANDLERS


def test_reply_caps_by_tier():
    assert ai.reply_cap_for(1, False) == ai.REPLY_CAP_NORMAL
    assert ai.reply_cap_for(1, True) == ai.REPLY_CAP_PREMIUM
    assert ai.reply_cap_for(1, True, True) == config.PREMIUM_PLUS_REPLY_CAP
    assert config.PREMIUM_PLUS_REPLY_CAP > ai.REPLY_CAP_PREMIUM
    assert ai.reply_cap_for(None, True, True) == ai.DM_CAP          # DMs never get the server tier


def test_plus_products_wired_everywhere():
    for t in ("premium_plus", "premium_plus_yearly"):
        assert t in UNLOCK_HANDLERS
        assert t in gp._GUILD_SCOPED_TYPES
        assert t in ga.PRODUCTS
        assert gp.expected_price_usd(t) > 0
    assert ga.PRODUCTS["premium_plus"]["kind"] == "membership"       # monthly: adopt by name or DM the owner
    assert ga.PRODUCTS["premium_plus_yearly"]["kind"] == "one_time"  # yearly: the bot can create it itself
    assert gp.expected_price_usd("premium_plus") == config.PREMIUM_PLUS_FEE_USD
    assert config.PREMIUM_PLUS_FEE_USD > config.PREMIUM_FEE_USD
    assert config.PREMIUM_PLUS_YEARLY_FEE_USD < config.PREMIUM_PLUS_FEE_USD * 12


def test_owner_dm_text_for_plus_membership(monkeypatch):
    sent = []

    async def fake_alert(msg):
        sent.append(msg)

    store = {}

    async def get(k):
        return store.get(k)

    async def put(k, v):
        store[k] = v

    monkeypatch.setattr("gumroad_payments._alert_owner", fake_alert)
    monkeypatch.setattr(ga.db, "get_global_setting", get)
    monkeypatch.setattr(ga.db, "set_global_setting", put)
    asyncio.run(ga._notify_owner_once("premium_plus", "test"))
    asyncio.run(ga._notify_owner_once("premium_plus", "test"))      # only once
    assert len(sent) == 1
    assert "Server Premium Plus - Monthly" in sent[0] and "6.99" in sent[0] and "Membership" in sent[0]


def test_migration_is_additive_and_wired():
    sql = (Path(__file__).resolve().parents[2] / "database/migrations/053_guild_plus.sql").read_text()
    assert "ADD COLUMN IF NOT EXISTS plus_expires_at" in sql and "plus_subscription_id" in sql
    assert "DROP" not in sql.upper()
    assert "053_guild_plus.sql" in (Path(__file__).resolve().parents[2] / "database.py").read_text()
