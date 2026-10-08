# path: modules/user_billing.py
"""Applies one gateway billing event to a user's plan. Called ONLY from the payment webhooks.

apply_event is idempotent by gateway event id: the id is claimed first (database
billing_event_claim), so a duplicate or retried webhook is a no-op and an out-of-order
cancel/charge can never double-extend. If writing the entitlement fails after the claim,
the claim is released so the gateway's retry can succeed.
"""
import logging
from typing import Optional

from modules import user_subs

logger = logging.getLogger(__name__)


async def apply_event(db, *, event_id: str, user_id, product: str, kind: str, provider: str,
                      subscription_id: Optional[str] = None, now=None,
                      cancel_flag: bool = False) -> str:
    """Returns one of: 'applied', 'duplicate', 'ignored'."""
    if not event_id or not user_id or not user_subs.is_plan(product) or kind not in user_subs.KINDS:
        return "ignored"
    uid = str(user_id)
    if not await db.billing_event_claim(event_id, uid, product, provider, kind):
        return "duplicate"
    try:
        current = await db.entitlement_get(uid, product)
        change = user_subs.transition(current, product, kind, now, cancel_flag=cancel_flag)
        if change is None:
            return "ignored"
        await db.entitlement_upsert(uid, product, change["status"], change["expires_at"],
                                    source=provider, subscription_id=subscription_id,
                                    cancel_at_period_end=change["cancel_at_period_end"])
    except Exception:
        logger.exception("[billing] failed applying %s %s for user %s; releasing claim", kind, event_id, uid)
        try:
            await db.billing_event_release(event_id)
        except Exception:
            logger.exception("[billing] could not release claim %s", event_id)
        raise
    logger.info("[billing] %s %s -> %s (user %s, %s)", kind, event_id, change["status"], uid, product)
    return "applied"
