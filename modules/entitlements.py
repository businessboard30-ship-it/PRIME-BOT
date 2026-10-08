# path: modules/entitlements.py
"""Per-user paid products (member dashboard). Pure functions: no I/O, so web and bot share one rule.

Products: card_plan ($2/mo, custom level-up card), dev_monthly ($5/mo) and dev_yearly ($30/yr) (Developer mode).
Status values: active | past_due | cancelled | expired.

Access rules (the browser never decides these):
  * active                        -> access while expires_at is in the future (or unset).
  * cancelled (cancel at period end) -> access until expires_at, then none.
  * past_due                      -> access for PAST_DUE_GRACE after expires_at (failed renewal), then none.
  * expired / unknown / no row    -> no access.
Only the payment webhook may write user_entitlements (database.entitlement_upsert).
"""
from datetime import datetime, timedelta, timezone

PRODUCTS = ("card_plan", "dev_monthly", "dev_yearly")
DEV_PRODUCTS = ("dev_monthly", "dev_yearly")
STATUSES = ("active", "past_due", "cancelled", "expired")
PAST_DUE_GRACE = timedelta(days=3)
DEV_EXPORT_GRACE = timedelta(days=7)      # export stays available this long after Developer mode ends


def _now(now):
    return now or datetime.now(timezone.utc)


def _aware(dt):
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def effective(row: dict, now=None) -> dict:
    """{'access': bool, 'state': str} for one entitlement row. `state` is what the UI should show."""
    now = _now(now)
    status = (row or {}).get("status")
    exp = _aware((row or {}).get("expires_at"))
    if status not in STATUSES:
        return {"access": False, "state": "none"}
    if status == "expired":
        return {"access": False, "state": "expired"}
    if status == "past_due":
        ok = exp is not None and now < exp + PAST_DUE_GRACE
        return {"access": ok, "state": "past_due" if ok else "expired"}
    # active / cancelled: need a future expiry (a missing one never grants, so a bad row fails closed)
    if exp is None or now >= exp:
        return {"access": False, "state": "expired"}
    return {"access": True, "state": "cancelled" if status == "cancelled" else "active"}


def has_access(rows, products, now=None) -> bool:
    return any(r.get("product") in products and effective(r, now)["access"] for r in rows or [])


def latest_expiry(rows, products):
    exps = [_aware(r.get("expires_at")) for r in rows or [] if r.get("product") in products and r.get("expires_at")]
    return max(exps) if exps else None


def export_allowed(rows, now=None) -> bool:
    """Developer export keeps working for DEV_EXPORT_GRACE after the plan ends."""
    now = _now(now)
    if has_access(rows, DEV_PRODUCTS, now):
        return True
    exp = latest_expiry(rows, DEV_PRODUCTS)
    return exp is not None and now < exp + DEV_EXPORT_GRACE


def summary(rows, now=None) -> dict:
    """Whitelisted, browser-safe view of a user's entitlements."""
    now = _now(now)
    items = []
    for r in rows or []:
        if r.get("product") not in PRODUCTS:
            continue
        e = effective(r, now)
        exp = _aware(r.get("expires_at"))
        items.append({"product": r["product"], "state": e["state"], "access": e["access"],
                      "expires_at": exp.isoformat() if exp else None,
                      "cancel_at_period_end": bool(r.get("cancel_at_period_end"))})
    return {"entitlements": items,
            "card_plan": has_access(rows, ("card_plan",), now),
            "dev_unlocked": has_access(rows, DEV_PRODUCTS, now)}
