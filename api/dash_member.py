# path: api/dash_member.py
"""Member dashboard (#/me). Any signed-in Discord user; every query is keyed to the SESSION's user id.
No member route accepts a user id from the client (a test asserts it). Read-only in B0.

ROUTES[action] = handler(uid: str, q) -> dict. dash.py runs _require_member and the per-user rate limit first.
"""
import json
import logging
import secrets
import time

import config
from modules import entitlements as ent
from modules import user_subs

logger = logging.getLogger(__name__)


async def member_status(uid, q, db):
    rows = await db.entitlements_list(uid)
    return ent.summary(rows)


def _plans_view(rows) -> list:
    """Server-priced plan list for the page (price and period never come from the client)."""
    by_product = {r.get("product"): r for r in rows or []}
    out = []
    for product, plan in user_subs.PLANS.items():
        e = ent.effective(by_product[product]) if product in by_product else {"access": False, "state": "none"}
        out.append({"product": product, "label": plan["label"], "price_usd": plan["price_usd"],
                    "period_days": plan["period_days"], "state": e["state"]})
    return out


async def member_plans(uid, q, db):
    return {"plans": _plans_view(await db.entitlements_list(uid))}


async def checkout_user(uid, body, db):
    """Start checkout for ONE plan for the SIGNED-IN user. The user id is the session's; the price
    is the server's. Returns a /pay link that detects the buyer's country (Paystack in Ghana,
    Gumroad elsewhere). Nothing is granted here: only the payment webhook can grant."""
    product = (body or {}).get("product")
    if not user_subs.is_plan(product):
        return {"ok": False, "error": "Unknown plan."}
    base = (getattr(config, "PUBLIC_BASE_URL", "") or "").rstrip("/")
    if not base:
        logger.error("checkout_user: PUBLIC_BASE_URL is not set")
        return {"ok": False, "error": "Checkout isn't available right now."}
    token = secrets.token_urlsafe(18)
    intent = {"payment_type": product, "user_id": uid, "guild_id": None, "clone_id": None,
              "price_usd": user_subs.price_usd(product), "extra": {}, "created": time.time()}
    await db.set_global_setting(f"payintent:{token}", json.dumps(intent))
    return {"checkout_url": f"{base}/pay?t={token}", "product": product,
            "price_usd": user_subs.price_usd(product), "period_days": user_subs.PLANS[product]["period_days"]}


ROUTES = {"member_status": member_status, "member_plans": member_plans}
WRITES = {"checkout_user": checkout_user}
