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


@pytest.fixture
def made(monkeypatch):
    seen = []

    async def fake(intent, country):
        seen.append((intent, country))
        return "https://checkout.example.test/" + ("paystack" if country == "GH" else "gumroad")
    import payments_manual
    monkeypatch.setattr(payments_manual, "create_checkout_for_intent", fake)
    return seen


@pytest.fixture(autouse=True)
def _split(env, monkeypatch):
    async def gm(clone_id=None):
        return "split"
    monkeypatch.setattr(env[0], "get_payment_mode", gm, raising=False)


def test_checkout_binds_server_user_and_server_price(pay, made):
    st, p, _ = buy("premium_yearly", provider="gumroad", price_usd=0.01, guild_id_override="999", user_id=1)   # client extras ignored
    assert st == 200 and p["url"] == "https://checkout.example.test/gumroad" and "/pay?" not in p["url"]
    intent, country = made[0]
    assert intent["payment_type"] == "premium_yearly" and intent["price_usd"] == 36.0 and country == "XX"
    assert intent["guild_id"] == GUILD and intent["user_id"] == 6 and intent["clone_id"] is None
    assert buy("premium", provider="paystack")[1]["url"].endswith("/paystack") and made[1][1] == "GH"


def test_checkout_follows_payment_mode(pay, made, monkeypatch):
    assert [o["provider"] for o in call("GET", {"action": "billing", "guild_id": str(GUILD)})[1]["pay"]] == ["paystack", "gumroad"]
    assert buy("premium")[0] == 422                           # split: the buyer must choose
    async def gm(clone_id=None):
        return "auto"
    monkeypatch.setattr(pay, "get_payment_mode", gm, raising=False)
    assert buy("premium", provider="gumroad")[0] == 422 and buy("premium")[1]["url"].endswith("/paystack")


def test_checkout_rejects_bad_requests(pay, made):
    assert buy("free_lunch")[0] == 422
    assert buy(None)[0] == 422
    assert call("POST", body={"action": "checkout", "guild_id": "999", "plan": "premium"})[0] == 403
    assert call("POST", token=None, body={"action": "checkout", "guild_id": str(GUILD), "plan": "premium"})[0] == 401
    assert not pay.settings


def test_checkout_blocks_duplicates_and_missing_prices(pay, made, monkeypatch):
    pay.pack = True
    assert buy("welcome_card_pack")[0] == 409
    pay.pack, pay.premium = False, True
    assert buy("welcome_card_pack")[0] == 409                 # included with Premium
    assert buy("premium_lifetime", provider="gumroad")[0] == 200                  # upgrading while active is allowed
    monkeypatch.setitem(PRICES, "premium", None)
    assert buy("premium", provider="gumroad")[0] == 503


def test_checkout_gateway_failure_and_rate_limit(pay, made, monkeypatch):
    import payments_manual

    async def none(intent, country):
        return None
    monkeypatch.setattr(payments_manual, "create_checkout_for_intent", none)
    assert buy("premium", provider="paystack")[0] == 502
    async def ok(intent, country):
        return "https://checkout.example.test/x"
    monkeypatch.setattr(payments_manual, "create_checkout_for_intent", ok)
    monkeypatch.setattr(dash, "CHECKOUT_MIN_INTERVAL", 60)
    dash._last_checkout.clear()
    assert buy("premium", provider="paystack")[0] == 200
    assert buy("premium", provider="paystack")[0] == 429
