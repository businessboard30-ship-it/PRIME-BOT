"""Clone bots get Premium checkout on the dashboard: Paystack / Gumroad buttons from the CLONE's payment mode."""
from datetime import datetime, timezone

import pytest

import config
from api import dash
from tests.unit.test_dash_api import _clone_env, call, env, GUILD  # noqa: F401

PRICES = {"premium": 4.0, "premium_yearly": 36.0, "premium_lifetime": 99.99, "welcome_card_pack": 2.0}


@pytest.fixture
def cl(env, monkeypatch):
    fake, members = env
    _clone_env(env, monkeypatch)
    state = {"made": [], "modes": {}, "asked": [], "expires": None}

    async def gp(gid, clone_id=None):
        return {"expires_at": state["expires"]} if state["expires"] else None

    async def active(gid, clone_id=None):
        return bool(state["expires"])

    async def gm(clone_id=None):
        state["asked"].append(clone_id)
        return state["modes"].get(clone_id, "split")

    async def made(intent, country):
        state["made"].append((intent, country))
        return "https://checkout.example.test/" + ("paystack" if country == "GH" else "gumroad")
    fake.get_guild_premium, fake.is_guild_premium_active, fake.get_payment_mode = gp, active, gm
    import payments_manual
    monkeypatch.setattr(payments_manual, "create_checkout_for_intent", made)
    monkeypatch.setattr(dash, "_price_usd", lambda plan: PRICES.get(plan))
    monkeypatch.setattr(dash, "CHECKOUT_MIN_INTERVAL", 0)
    monkeypatch.setattr(config, "DASH_OAUTH_REDIRECT_URI", "https://api.example.com/api/dash")
    dash._last_checkout.clear()
    fake.members = members
    return fake, state


def billing():
    return call("GET", {"action": "billing", "guild_id": str(GUILD), "clone_id": "7"})


def buy(plan, **extra):
    return call("POST", body=dict({"action": "checkout", "guild_id": str(GUILD), "clone_id": 7, "plan": plan}, **extra))


def test_clone_lists_the_three_premium_plans_and_no_card_pack(cl):
    st, p, _ = billing()
    assert st == 200 and p["clone"] is True
    assert {x["id"] for x in p["plans"]} == {"premium", "premium_yearly", "premium_lifetime"}
    assert [o["provider"] for o in p["pay"]] == ["paystack", "gumroad"]


def test_buttons_follow_the_clones_own_payment_mode(cl):
    fake, state = cl
    state["modes"][7] = "gumroad"                           # main is split, this clone is Gumroad only
    assert [o["provider"] for o in billing()[1]["pay"]] == ["gumroad"]
    assert 7 in state["asked"]
    assert buy("premium", provider="paystack")[0] == 422
    assert buy("premium")[1]["url"].endswith("/gumroad")
    state["modes"][7] = "auto"
    assert [o["provider"] for o in billing()[1]["pay"]] == ["paystack"]


def test_clone_checkout_is_bound_to_the_clone_server_user_and_server_price(cl):
    fake, state = cl
    st, p, _ = buy("premium_yearly", provider="gumroad", price_usd=0.01, clone_id_override=1, user_id=1)
    assert st == 200 and "/pay?" not in p["url"]
    intent, country = state["made"][0]
    assert intent["clone_id"] == 7 and intent["guild_id"] == GUILD and intent["user_id"] == 6
    assert intent["price_usd"] == 36.0 and intent["payment_type"] == "premium_yearly" and country == "XX"
    assert buy("premium", provider="paystack")[1]["url"].endswith("/paystack") and state["made"][1][1] == "GH"


def test_split_clone_makes_the_buyer_choose(cl):
    assert buy("premium")[0] == 422 and cl[1]["made"] == []


def test_clones_cannot_buy_the_card_pack_or_unknown_plans(cl):
    assert buy("welcome_card_pack", provider="gumroad")[0] == 422
    assert buy("nope", provider="gumroad")[0] == 422 and cl[1]["made"] == []


def test_lifetime_clone_server_cannot_buy_again(cl):
    fake, state = cl
    state["expires"] = datetime(2090, 1, 1, tzinfo=timezone.utc)
    p = billing()[1]
    assert p["premium"] is True and all(x["owned"] for x in p["plans"])
    assert buy("premium", provider="gumroad")[0] == 409 and state["made"] == []


def test_active_but_not_lifetime_can_renew(cl):
    fake, state = cl
    state["expires"] = datetime(2030, 1, 1, tzinfo=timezone.utc)
    assert not any(x["owned"] for x in billing()[1]["plans"])
    assert buy("premium", provider="gumroad")[0] == 200


def test_clone_checkout_still_needs_manage_server(cl):
    fake, state = cl
    fake.members["6"] = {"roles": ["501"]}                  # plain member
    assert buy("premium", provider="gumroad")[0] == 403 and state["made"] == []


def test_main_bot_checkout_is_unchanged(cl):
    st, p, _ = call("POST", body={"action": "checkout", "guild_id": str(GUILD), "plan": "premium", "provider": "paystack"})
    assert st == 200 and cl[1]["made"][0][0]["clone_id"] is None
