# path: modules/paystack_user_plans.py
"""Paystack webhook events for the per-user plans (card_plan / dev_monthly / dev_yearly).

handle() returns None when the event is not about a per-user plan (the existing webhook logic
continues untouched), otherwise one of 'applied' | 'duplicate' | 'ignored'.

Trust model: the caller has already verified Paystack's HMAC signature. On top of that
  * a FIRST charge must match a pending payment_logs row we created for that same user (reference);
  * a RENEWAL / cancel / failure never creates an entitlement, it only changes one that exists;
  * the user is read from our own metadata, or from the checkout email we generated
    (user_<discord id>@animebot.com), and the product from OUR plan-code map, never from the amount.
"""
import logging
import re
from typing import Optional

from modules import user_billing, user_subs

logger = logging.getLogger(__name__)

_EMAIL_RE = re.compile(r"^user_(\d{5,25})@animebot\.com$")
_KIND_BY_EVENT = {
    "subscription.not_renew": "cancel",       # will not renew; access runs to the period end
    "subscription.disable": "ended",
    "invoice.payment_failed": "failed",
    "invoice.update": None,                   # only a failed invoice matters, handled below
}


def _plan_map() -> dict:
    import config
    return {code: product for product, code in config.USER_PLAN_PAYSTACK_CODES.items() if code}


def _product(data: dict) -> Optional[str]:
    meta = data.get("metadata") or {}
    if isinstance(meta, dict) and user_subs.is_plan(meta.get("type")):
        return meta["type"]
    plan = data.get("plan") or {}
    code = plan.get("plan_code") if isinstance(plan, dict) else None
    return _plan_map().get(code or "")


def _user_id(data: dict) -> Optional[str]:
    meta = data.get("metadata") or {}
    uid = meta.get("user_id") if isinstance(meta, dict) else None
    if uid and str(uid).isdigit():
        return str(uid)
    email = ((data.get("customer") or {}).get("email") or "").strip().lower()
    m = _EMAIL_RE.match(email)
    return m.group(1) if m else None


async def handle(db, event_type: str, data: dict, now=None) -> Optional[str]:
    if not isinstance(data, dict):
        return None
    product = _product(data)
    if not product:
        return None
    uid = _user_id(data)
    if not uid:
        logger.warning("[paystack-plan] %s for %s with no resolvable user", event_type, product)
        return "ignored"

    if event_type == "charge.success":
        if data.get("status") != "success":
            return "ignored"
        reference = data.get("reference")
        eid = f"ps:charge:{data.get('id') or reference}"
        pending = await db.get_payment_by_reference(reference) if reference else None
        existing = await db.entitlement_get(uid, product)
        if pending:
            same = (pending.get("provider") == "paystack" and str(pending.get("user_id")) == uid
                    and pending.get("payment_type") == product)
            if not same:
                logger.warning("[paystack-plan] reference %s does not match user %s / %s", reference, uid, product)
                return "ignored"
        elif not existing:
            logger.warning("[paystack-plan] renewal charge for user %s with no entitlement and no pending row", uid)
            return "ignored"
        result = await user_billing.apply_event(db, event_id=eid, user_id=uid, product=product,
                                                kind="charge", provider="paystack", now=now)
        if pending and pending.get("status") == "pending" and result in ("applied", "duplicate"):
            await db.mark_payment_paid(reference)
        return result

    kind = _KIND_BY_EVENT.get(event_type)
    if event_type == "invoice.update" and str(data.get("status", "")).lower() == "failed":
        kind = "failed"
    if kind is None:
        return None
    code = data.get("subscription_code") or (data.get("subscription") or {}).get("subscription_code") or ""
    eid = f"ps:{kind}:{code or uid}:{data.get('invoice_code') or data.get('next_payment_date') or data.get('id') or ''}"
    return await user_billing.apply_event(db, event_id=eid, user_id=uid, product=product,
                                          kind=kind, provider="paystack", now=now)
