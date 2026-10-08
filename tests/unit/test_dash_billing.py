import json
from datetime import datetime, timezone

import pytest

import config
from api import dash
from tests.unit.test_dash_api import env, call, GUILD  # noqa: F401

PRICES = {"premium": 4.0, "premium_yearly": 36.0, "premium_lifetime": 99.99, "welcome_card_pack": 2.0}


@pytest.fixture
def pay(env, monkeypatch):
    fake, members = env
    fake.settings, fake.pack = {}, False
    async def welcome(gid, clone_id=None):
        return {"card_pack_unlocked": fake.pack}
    async def gp(gid, clone_id=None):
        return {"expires_at": datetime(2030, 1, 1, tzinfo=timezone.utc)} if fake.premium else None
    async def setg(k, v):
        fake.settings[k] = v
    fake.get_welcome_config, fake.get_guild_premium, fake.set_global_setting = welcome, gp, setg
    monkeypatch.setattr(dash, "_price_usd", lambda plan: PRICES.get(plan))
    monkeypatch.setattr(dash, "CHECKOUT_MIN_INTERVAL", 0)
    monkeypatch.setattr(config, "DASH_OAUTH_REDIRECT_URI", "https://api.example.com/api/dash")
    dash._last_checkout.clear()
    return fake


def buy(plan, **extra):
    return call("POST", body=dict({"action": "checkout", "guild_id": str(GUILD), "plan": plan}, **extra))


def test_billing_lists_plans_and_status(pay):
    st, p, _ = call("GET", {"action": "billing", "guild_id": str(GUILD)})
    assert st == 200 and p["premium"] is False and p["expires_at"] is None
    assert {x["id"]: x["price_usd"] for x in p["plans"]} == PRICES
    pay.premium = True
    p = call("GET", {"action": "billing", "guild_id": str(GUILD)})[1]
    assert p["premium"] is True and p["expires_at"].startswith("2030")
    assert [x for x in p["plans"] if x["id"] == "welcome_card_pack"][0]["included"] is True


def test_checkout_binds_server_user_and_server_price(pay):
    st, p, _ = buy("premium_yearly", price_usd=0.01, guild_id_override="999", user_id=1)   # client extras ignored
    assert st == 200 and p["url"].startswith("https://api.example.com/pay?t=")
    token = p["url"].split("t=")[1]
    intent = json.loads(pay.settings[f"payintent:{token}"])
    assert intent["payment_type"] == "premium_yearly" and intent["price_usd"] == 36.0
    assert intent["guild_id"] == GUILD and intent["user_id"] == 6 and intent["clone_id"] is None
    assert len(token) <= 64


def test_checkout_rejects_bad_requests(pay):
    assert buy("free_lunch")[0] == 422
    assert buy(None)[0] == 422
    assert call("POST", body={"action": "checkout", "guild_id": "999", "plan": "premium"})[0] == 403
    assert call("POST", token=None, body={"action": "checkout", "guild_id": str(GUILD), "plan": "premium"})[0] == 401
    assert not pay.settings


def test_checkout_blocks_duplicates_and_missing_prices(pay, monkeypatch):
    pay.pack = True
    assert buy("welcome_card_pack")[0] == 409
    pay.pack, pay.premium = False, True
    assert buy("welcome_card_pack")[0] == 409                 # included with Premium
    assert buy("premium_lifetime")[0] == 200                  # upgrading while active is allowed
    monkeypatch.setitem(PRICES, "premium", None)
    assert buy("premium")[0] == 503


def test_checkout_needs_config_and_is_rate_limited(pay, monkeypatch):
    monkeypatch.setattr(config, "DASH_OAUTH_REDIRECT_URI", "")
    assert buy("premium")[0] == 503
    monkeypatch.setattr(config, "DASH_OAUTH_REDIRECT_URI", "https://api.example.com/api/dash")
    monkeypatch.setattr(dash, "CHECKOUT_MIN_INTERVAL", 60)
    dash._last_checkout.clear()
    assert buy("premium")[0] == 200
    assert buy("premium")[0] == 429
