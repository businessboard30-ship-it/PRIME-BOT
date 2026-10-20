"""Auto-creation of payment products: neutral names, Paystack plans created/adopted, Gumroad adopt-or-notify."""
import asyncio
import json

import pytest

import config
import gumroad_autocreate as ga
import paystack_autocreate as pa


class FakeDB:
    def __init__(self):
        self.s = {}

    async def get_global_setting(self, k):
        return self.s.get(k)

    async def set_global_setting(self, k, v):
        self.s[k] = v


@pytest.fixture
def pdb(monkeypatch):
    fake = FakeDB()
    monkeypatch.setattr(pa, "db", fake)
    monkeypatch.setattr(pa, "RUNTIME", {})
    monkeypatch.setattr(pa, "_last_load", 0.0)
    monkeypatch.setattr(config, "PAYSTACK_SECRET_KEY", "sk_test_x", raising=False)
    monkeypatch.setitem(config.USER_PLAN_PAYSTACK_CODES, "card_plan", "")
    monkeypatch.setitem(config.USER_PLAN_PAYSTACK_CODES, "dev_monthly", "")
    monkeypatch.setitem(config.USER_PLAN_PAYSTACK_CODES, "dev_yearly", "")
    monkeypatch.setattr(pa, "_amount_minor", lambda product: 1234)
    return fake


def test_no_product_or_plan_name_mentions_the_bot():
    names = [s["name"] for s in ga.PRODUCTS.values()] + [s["name"] for s in pa.PLAN_SPECS.values()]
    text = " ".join(names + [s["description"] for s in ga.PRODUCTS.values()] + [s["description"] for s in pa.PLAN_SPECS.values()])
    assert "prime" not in text.lower() and "bot" not in text.lower().replace("robot", "")
    assert all(n.strip() for n in names)


def test_gumroad_adopts_old_names_and_never_creates_memberships():
    assert "PRIME-BOT Premium - Yearly" in ga.PRODUCTS["premium_yearly"]["legacy"]
    assert {p: s["kind"] for p, s in ga.PRODUCTS.items() if s["kind"] == "membership"} == {
        "card_plan": "membership", "dev_monthly": "membership", "dev_yearly": "membership",
        "premium_plus": "membership", "anime_plus": "membership"}
    assert ga._cents("dev_yearly") == 3000 and ga._cents("card_plan") == 200 and ga._cents("premium_yearly") == round(config.PREMIUM_YEARLY_FEE_USD * 100)


def test_paystack_creates_missing_plans_and_saves_codes(pdb, monkeypatch):
    calls = []

    def fake(method, path, payload=None):
        calls.append((method, path, payload))
        if method == "GET":
            return {"status": True, "data": []}
        return {"status": True, "data": {"plan_code": "PLN_" + payload["name"].split()[0][:3].lower() + payload["interval"][:2]}}
    monkeypatch.setattr(pa, "_request", fake)
    assert asyncio.run(pa.ensure_plans()) is True
    posts = [c[2] for c in calls if c[0] == "POST"]
    assert {p["name"] for p in posts} == {s["name"] for s in pa.PLAN_SPECS.values()}
    assert all(p["currency"] == "GHS" and p["amount"] == 1234 for p in posts)
    assert {p["interval"] for p in posts} == {"monthly", "annually"}
    assert set(pdb.s) >= {"paystack_plan:card_plan", "paystack_plan:dev_monthly", "paystack_plan:dev_yearly", "paystack_plan:anime_plus"}
    assert pa.plan_code_for("dev_yearly") and pa.code_to_product()[pa.plan_code_for("card_plan")] == "card_plan"


def test_paystack_adopts_an_existing_plan_instead_of_duplicating(pdb, monkeypatch):
    existing = [{"name": "Developer Mode - Monthly", "interval": "monthly", "currency": "GHS", "plan_code": "PLN_have"}]
    posted = []

    def fake(method, path, payload=None):
        if method == "GET":
            return {"status": True, "data": existing}
        posted.append(payload["name"])
        return {"status": True, "data": {"plan_code": "PLN_new_" + str(len(posted))}}
    monkeypatch.setattr(pa, "_request", fake)
    asyncio.run(pa.ensure_plans())
    assert pa.plan_code_for("dev_monthly") == "PLN_have" and "Developer Mode - Monthly" not in posted


def test_paystack_refusal_dms_the_owner_once_and_fails_closed(pdb, monkeypatch):
    sent = []

    async def alert(msg):
        sent.append(msg)
    import gumroad_payments
    monkeypatch.setattr(gumroad_payments, "_alert_owner", alert)
    monkeypatch.setattr(pa, "_request", lambda m, p, payload=None: {"status": True, "data": []} if m == "GET" else {"status": False, "message": "nope"})
    assert asyncio.run(pa.ensure_plans()) is False
    assert asyncio.run(pa.ensure_plans()) is False
    assert len(sent) == 4 and all("prime" not in m.lower() for m in sent)      # once per plan, not once per pass
    assert pa.plan_code_for("card_plan") == ""


def test_env_codes_still_win(pdb, monkeypatch):
    monkeypatch.setitem(config.USER_PLAN_PAYSTACK_CODES, "card_plan", "PLN_env")
    pa.RUNTIME["card_plan"] = "PLN_auto"
    assert pa.plan_code_for("card_plan") == "PLN_env"


def test_disguild_products_are_defined_and_neutral():
    for p in ("disguild_boost_7d", "disguild_boost_30d", "disguild_donate"):
        assert ga.PRODUCTS[p]["kind"] == "one_time" and p in ga.DISGUILD_ENV
    assert ga._cents("disguild_boost_7d") == round(config.DISGUILD_BOOST_7D_FEE_USD * 100)
    assert set(ga.DISGUILD_ENV.values()) == {"GUMROAD_BOOST_7D_URL", "GUMROAD_BOOST_30D_URL", "GUMROAD_DONATE_URL"}


def test_disguild_links_are_dm_d_to_the_owner_once(monkeypatch):
    fake = FakeDB()
    sent = []
    import gumroad_payments as gp

    async def alert(text):
        sent.append(text)
    monkeypatch.setattr(ga, "db", fake)
    monkeypatch.setattr(gp, "_alert_owner", alert)
    monkeypatch.setattr(ga, "RUNTIME", {"disguild_boost_7d": {"id": "a", "url": "https://x.gumroad.com/l/a"}})
    asyncio.run(ga._send_disguild_links_once())
    assert sent == []                      # not all three exist yet: nothing is sent
    ga.RUNTIME.update({"disguild_boost_30d": {"id": "b", "url": "https://x.gumroad.com/l/b"},
                       "disguild_donate": {"id": "c", "url": "https://x.gumroad.com/l/c"}})
    asyncio.run(ga._send_disguild_links_once())
    asyncio.run(ga._send_disguild_links_once())
    assert len(sent) == 1                  # sent once, not on every pass
    for var in ga.DISGUILD_ENV.values():
        assert var in sent[0]
    assert "https://x.gumroad.com/l/a" in sent[0] and "https://x.gumroad.com/l/c" in sent[0]


def test_disguild_boost_prices_match_the_owner_decision():
    assert ga._cents("disguild_boost_7d") == 299 and ga._cents("disguild_boost_30d") == 700


def test_deleted_disguild_product_is_forgotten_so_it_is_recreated(monkeypatch):
    fake = FakeDB()
    monkeypatch.setattr(ga, "db", fake)
    monkeypatch.setattr(config, "GUMROAD_ACCESS_TOKEN", "tok", raising=False)
    monkeypatch.setattr(ga, "RUNTIME", {"disguild_boost_7d": {"id": "old", "url": "u1"}, "disguild_donate": {"id": "keep", "url": "u3"}})

    async def fake_list(session, token):
        return [{"id": "keep", "name": "Disguild Donation"}, {"id": "old", "deleted": True}]
    monkeypatch.setattr(ga, "_list_products", fake_list)
    asyncio.run(ga._drop_deleted_disguild())
    assert "disguild_boost_7d" not in ga.RUNTIME and "disguild_donate" in ga.RUNTIME
    assert fake.s["gumroad_auto:disguild_boost_7d"] == ""
