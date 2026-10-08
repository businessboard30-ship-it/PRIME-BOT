# path: modules/user_subs.py
"""Per-user subscription rules (card plan, Developer mode). Pure functions: no I/O.

The payment webhooks translate a gateway notification into one of these events and
call `transition`; the result is what gets written to user_entitlements. The browser
never produces an event.

Events (kind):
  charge     a successful charge (first sale or renewal). Extends access by the plan's
             period, counted from the later of "now" and the current expiry, so an early
             renewal never loses paid time. Clears past_due and any pending cancel flag
             unless the event says the subscription is already set to cancel.
  cancel     cancel at period end: status 'cancelled', access kept until expires_at.
  ended      the gateway ended the subscription: access stops at the current expiry
             (an expiry in the future is kept; the entitlement just won't renew).
  failed     a renewal charge failed: status 'past_due' (3-day grace is applied by
             modules.entitlements.effective).

Idempotency is NOT decided here; the caller must claim the gateway event id first
(database.billing_event_claim) so a duplicate or retried event never reaches `transition`.
"""
from datetime import datetime, timedelta, timezone
from typing import Optional

# product -> (price_usd, period_days). Prices come from here, never from the client.
PLANS = {
    "card_plan": {"price_usd": 2.0, "period_days": 31, "label": "Custom level-up card"},
    "dev_monthly": {"price_usd": 5.0, "period_days": 31, "label": "Developer mode (monthly)"},
    "dev_yearly": {"price_usd": 30.0, "period_days": 366, "label": "Developer mode (yearly)"},
}
KINDS = ("charge", "cancel", "ended", "failed")
# Small slack so a charge that lands a little late never reads as underpaid by rounding.
MIN_PAID_RATIO = 0.98


def is_plan(product) -> bool:
    return product in PLANS


def price_usd(product) -> Optional[float]:
    p = PLANS.get(product)
    return p["price_usd"] if p else None


def paid_enough(product, paid_cents) -> bool:
    """True when the amount (in USD cents) covers the plan price. Unknown plan or junk -> False."""
    p = PLANS.get(product)
    try:
        cents = int(float(paid_cents))
    except (TypeError, ValueError):
        return False
    return bool(p) and cents >= round(p["price_usd"] * 100 * MIN_PAID_RATIO)


def _aware(dt):
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def transition(current: Optional[dict], product: str, kind: str, now=None,
               cancel_flag: bool = False) -> Optional[dict]:
    """Return {'status', 'expires_at', 'cancel_at_period_end'} to write, or None for 'no change'.

    `current` is the existing user_entitlements row for (user, product) or None.
    Unknown product or kind returns None (fail closed: nothing is granted)."""
    plan = PLANS.get(product)
    if plan is None or kind not in KINDS:
        return None
    now = _aware(now) or datetime.now(timezone.utc)
    exp = _aware((current or {}).get("expires_at"))

    if kind == "charge":
        base = exp if exp and exp > now else now
        new_exp = base + timedelta(days=plan["period_days"])
        return {"status": "cancelled" if cancel_flag else "active",
                "expires_at": new_exp, "cancel_at_period_end": bool(cancel_flag)}

    if current is None:
        # cancel / ended / failed for something that was never granted: nothing to change.
        return None

    if kind == "cancel":
        return {"status": "cancelled", "expires_at": exp, "cancel_at_period_end": True}
    if kind == "failed":
        return {"status": "past_due", "expires_at": exp, "cancel_at_period_end": False}
    # ended: keep paid time that is still in the future, never extend.
    if exp and exp > now:
        return {"status": "cancelled", "expires_at": exp, "cancel_at_period_end": True}
    return {"status": "expired", "expires_at": exp, "cancel_at_period_end": False}


# ---- checkout buttons: which gateways the dashboard offers, from the bot's payment mode ----
# split  -> Paystack AND Gumroad (the buyer picks)      auto    -> Paystack only
# gumroad -> Gumroad only                               anything else -> split
PAY_PROVIDERS = {
    "paystack": {"label": "Pay with Paystack", "region": "gh"},
    "gumroad": {"label": "Pay with Gumroad", "region": "intl"},
}


def pay_options(mode) -> list:
    """Buttons to show for the given payment mode. Pure; the server re-checks the choice at checkout."""
    m = (mode or "split").strip().lower()
    keys = ["paystack"] if m == "auto" else ["gumroad"] if m == "gumroad" else ["paystack", "gumroad"]
    return [{"provider": k, "label": PAY_PROVIDERS[k]["label"]} for k in keys]
