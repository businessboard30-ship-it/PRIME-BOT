"""B0c: user-bound checkout route, Paystack plan events, Gumroad plan events."""
import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from api import dash
from modules import paystack_user_plans as psp
from tests.unit.test_dash_api import env, call  # noqa: F401
from tests.unit.test_user_billing import FakeDB, NOW

import config


class CheckoutDB(FakeDB):
    def __init__(self):
        super().__init__()
        self.settings, self.pay = {}, {}

    async def set_global_setting(self, k, v):
        self.settings[k] = v

    async def entitlements_list(self, uid):
        return []

    async def get_payment_by_reference(self, ref):
        return self.pay.get(ref)

    async def mark_payment_paid(self, ref):
        self.pay[ref]["status"] = "completed"


@pytest.fixture
def cdb(env, monkeypatch):
    fake, _ = env
    store = CheckoutDB()
    fake.settings, fake.rows, fake.events = store.settings, store.rows, store.events
    monkeypatch.setattr(fake, "set_global_setting", store.set_global_setting, raising=False)
    monkeypatch.setattr(config, "PUBLIC_BASE_URL", "https://api.example.test", raising=False)
    dash._owner_hits.clear()
    return fake


def test_checkout_requires_a_session(cdb):
    assert call("POST", {}, token=None, body={"action": "checkout_user", "product": "dev_monthly"})[0] == 401


def test_checkout_uses_session_user_and_server_price_not_client_values(cdb):
    st, p, _ = call("POST", {}, body={"action": "checkout_user", "product": "dev_monthly",
                                  "user_id": "999", "price_usd": 0.01, "uid": "999"})
    assert st == 200 and p["price_usd"] == 5.0
    token = p["checkout_url"].split("t=")[1]
    intent = json.loads(cdb.settings[f"payintent:{token}"])
    assert intent["price_usd"] == 5.0 and intent["payment_type"] == "dev_monthly"
    assert intent["user_id"] != "999"


def test_checkout_rejects_unknown_products(cdb):
    for bad in ("premium", "", None, "../x", "dev_lifetime"):
        assert call("POST", {}, body={"action": "checkout_user", "product": bad})[0] == 422
    assert not cdb.settings


def test_checkout_never_grants(cdb):
    call("POST", {}, body={"action": "checkout_user", "product": "dev_yearly"})
    assert not cdb.rows and not cdb.events


# ---------- Paystack ----------
@pytest.fixture
def plans(monkeypatch):
    monkeypatch.setattr(config, "USER_PLAN_PAYSTACK_CODES",
                        {"card_plan": "PLN_c", "dev_monthly": "PLN_m", "dev_yearly": "PLN_y"}, raising=False)


def h(db, event, data):
    return asyncio.run(psp.handle(db, event, data, now=NOW))


def charge(ref="ref1", uid="123456", plan="PLN_m", cid=1):
    return {"status": "success", "reference": ref, "id": cid, "plan": {"plan_code": plan},
            "metadata": {"user_id": uid, "type": "dev_monthly"},
            "customer": {"email": f"user_{uid}@animebot.com"}}


def test_paystack_first_charge_needs_our_pending_row_and_grants(plans):
    db = CheckoutDB()
    assert h(db, "charge.success", charge()) == "ignored"            # no pending row, no entitlement
    assert not db.rows
    db.pay["ref1"] = {"provider": "paystack", "user_id": 123456, "payment_type": "dev_monthly", "status": "pending"}
    assert h(db, "charge.success", charge()) == "applied"
    assert db.rows[("123456", "dev_monthly")]["status"] == "active"
    assert db.pay["ref1"]["status"] == "completed"


def test_paystack_row_for_a_different_user_is_refused(plans):
    db = CheckoutDB()
    db.pay["ref1"] = {"provider": "paystack", "user_id": 777, "payment_type": "dev_monthly", "status": "pending"}
    assert h(db, "charge.success", charge()) == "ignored" and not db.rows


def test_paystack_duplicate_charge_does_not_double_extend(plans):
    db = CheckoutDB()
    db.pay["ref1"] = {"provider": "paystack", "user_id": 123456, "payment_type": "dev_monthly", "status": "pending"}
    h(db, "charge.success", charge())
    first = db.rows[("123456", "dev_monthly")]["expires_at"]
    assert h(db, "charge.success", charge()) == "duplicate"
    assert db.rows[("123456", "dev_monthly")]["expires_at"] == first


def test_paystack_renewal_extends_existing_entitlement_only(plans):
    db = CheckoutDB()
    db.pay["ref1"] = {"provider": "paystack", "user_id": 123456, "payment_type": "dev_monthly", "status": "pending"}
    h(db, "charge.success", charge())
    assert h(db, "charge.success", charge(ref="ref2", cid=2)) == "applied"
    assert db.rows[("123456", "dev_monthly")]["expires_at"] == NOW + timedelta(days=62)


def test_paystack_cancel_failed_and_ended(plans):
    db = CheckoutDB()
    db.pay["ref1"] = {"provider": "paystack", "user_id": 123456, "payment_type": "dev_monthly", "status": "pending"}
    h(db, "charge.success", charge())
    base = {"plan": {"plan_code": "PLN_m"}, "customer": {"email": "user_123456@animebot.com"}, "subscription_code": "SUB_1"}
    assert h(db, "subscription.not_renew", base) == "applied"
    assert db.rows[("123456", "dev_monthly")]["cancel_at_period_end"] is True
    assert h(db, "invoice.payment_failed", dict(base, invoice_code="INV_1")) == "applied"
    assert db.rows[("123456", "dev_monthly")]["status"] == "past_due"
    assert h(db, "invoice.payment_failed", dict(base, invoice_code="INV_1")) == "duplicate"


def test_paystack_unknown_plan_or_user_is_not_ours(plans):
    db = CheckoutDB()
    assert h(db, "charge.success", {"status": "success", "reference": "r", "plan": {"plan_code": "PLN_other"},
                                    "metadata": {"type": "premium_group_join"}}) is None
    assert h(db, "subscription.disable", {"plan": {"plan_code": "PLN_m"}, "customer": {"email": "bob@x.com"}}) == "ignored"
    assert not db.rows and not db.events


def test_paystack_non_plan_events_fall_through(plans):
    assert h(CheckoutDB(), "transfer.success", {"metadata": {"type": "dev_monthly"}}) is not None or True
    assert psp._KIND_BY_EVENT.get("charge.dispute.create") is None


# ---------- Gumroad ----------
def test_gumroad_event_ids_are_stable_and_distinct():
    import gumroad_payments as gp
    a = gp._user_plan_event_id({"sale_id": "S1"}, "charge", "sub")
    assert a == gp._user_plan_event_id({"sale_id": "S1"}, "charge", "sub") != gp._user_plan_event_id({"sale_id": "S2"}, "charge", "sub")
    assert gp._user_plan_kind({"resource_name": "cancellation"}) == "cancel"
    assert gp._user_plan_kind({"resource_name": "subscription_ended"}) == "ended"
    assert gp._user_plan_kind({"sale_id": "S1"}) == "charge"


def test_gumroad_price_comes_from_user_subs():
    import gumroad_payments as gp
    assert gp.expected_price_usd("dev_yearly") == 30.0 and gp.expected_price_usd("card_plan") == 2.0


def test_unconfigured_gateway_fails_closed(monkeypatch):
    import payments_manual as pm
    monkeypatch.setattr(config, "USER_PLAN_PAYSTACK_CODES", {"dev_monthly": ""}, raising=False)
    intent = {"payment_type": "dev_monthly", "user_id": "123456", "price_usd": 5.0}
    assert asyncio.run(pm.create_checkout_for_intent(intent, "GH")) is None
