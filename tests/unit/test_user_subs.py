"""Pure rules for per-user subscriptions (modules/user_subs.py)."""
from datetime import datetime, timedelta, timezone

from modules import entitlements as ent
from modules import user_subs as us

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)


def test_prices_are_server_side_and_fixed():
    assert us.price_usd("card_plan") == 2.0
    assert us.price_usd("dev_monthly") == 5.0
    assert us.price_usd("dev_yearly") == 30.0
    assert us.price_usd("anything_else") is None


def test_paid_enough():
    assert us.paid_enough("dev_monthly", 500)
    assert us.paid_enough("dev_monthly", "500")
    assert not us.paid_enough("dev_monthly", 200)
    assert not us.paid_enough("dev_monthly", "junk")
    assert not us.paid_enough("nope", 99999)


def test_first_charge_grants_period_from_now():
    r = us.transition(None, "dev_monthly", "charge", NOW)
    assert r["status"] == "active" and not r["cancel_at_period_end"]
    assert r["expires_at"] == NOW + timedelta(days=31)


def test_renewal_extends_from_current_expiry_not_now():
    cur = {"status": "active", "expires_at": NOW + timedelta(days=5)}
    r = us.transition(cur, "dev_monthly", "charge", NOW)
    assert r["expires_at"] == NOW + timedelta(days=36)


def test_renewal_after_lapse_counts_from_now_and_clears_past_due():
    cur = {"status": "past_due", "expires_at": NOW - timedelta(days=2)}
    r = us.transition(cur, "card_plan", "charge", NOW)
    assert r["status"] == "active"
    assert r["expires_at"] == NOW + timedelta(days=31)


def test_yearly_period():
    r = us.transition(None, "dev_yearly", "charge", NOW)
    assert r["expires_at"] == NOW + timedelta(days=366)


def test_cancel_keeps_access_until_expiry():
    cur = {"status": "active", "expires_at": NOW + timedelta(days=10)}
    r = us.transition(cur, "dev_monthly", "cancel", NOW)
    assert r == {"status": "cancelled", "expires_at": cur["expires_at"], "cancel_at_period_end": True}
    row = dict(cur, **r)
    assert ent.effective(row, NOW)["access"] is True
    assert ent.effective(row, NOW + timedelta(days=11))["access"] is False


def test_failed_renewal_is_past_due_with_three_day_grace():
    cur = {"status": "active", "expires_at": NOW - timedelta(hours=1)}
    r = us.transition(cur, "dev_monthly", "failed", NOW)
    assert r["status"] == "past_due"
    row = dict(cur, **r)
    assert ent.effective(row, NOW + timedelta(days=2))["access"] is True
    assert ent.effective(row, NOW + timedelta(days=4))["access"] is False


def test_ended_never_extends():
    future = {"status": "active", "expires_at": NOW + timedelta(days=3)}
    r = us.transition(future, "dev_monthly", "ended", NOW)
    assert r["expires_at"] == future["expires_at"] and r["status"] == "cancelled"
    past = {"status": "active", "expires_at": NOW - timedelta(days=1)}
    assert us.transition(past, "dev_monthly", "ended", NOW)["status"] == "expired"


def test_non_charge_events_for_unknown_user_change_nothing():
    for kind in ("cancel", "ended", "failed"):
        assert us.transition(None, "dev_monthly", kind, NOW) is None


def test_unknown_product_or_kind_fails_closed():
    assert us.transition(None, "premium", "charge", NOW) is None
    assert us.transition(None, "dev_monthly", "refund", NOW) is None


def test_charge_on_a_subscription_set_to_cancel_stays_cancelling():
    r = us.transition(None, "card_plan", "charge", NOW, cancel_flag=True)
    assert r["status"] == "cancelled" and r["cancel_at_period_end"] is True
