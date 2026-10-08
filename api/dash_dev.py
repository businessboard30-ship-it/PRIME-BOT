# path: api/dash_dev.py
"""Developer mode (#/dev). Visible to every signed-in user, usable only with an active dev entitlement.

Enforcement is SERVER-SIDE: `dev_status` works for everyone (it drives the locked screen); every other
dev_* handler must start with `gate = await require_dev(uid, db)` and return it when set (402,
code "subscription_required"). The locked screen in the browser is cosmetic and grants nothing.
Nothing here writes entitlements: only the payment webhook does. No route takes a user id from the client.
"""
from modules import entitlements as ent
from modules import user_subs

WEEKLY_BOT_CHATS = 50          # bot-provided AI messages per week for Developer subscribers (plan C1; counter lands with chat)
FEATURES = (
    {"key": "chat", "label": "AI chat", "ready": False},
    {"key": "export", "label": "Export to your DMs", "ready": False},
    {"key": "keys", "label": "Bring your own AI key (Claude, Groq, OpenAI)", "ready": False},
    {"key": "github", "label": "Connect GitHub (read-only)", "ready": False},
)


def _gate_denied() -> dict:
    return {"_status": 402, "code": "subscription_required", "message": "Developer mode needs an active subscription."}


async def require_dev(uid, db):
    """None when the SESSION user has active Developer access, else the 402 reply dict."""
    rows = await db.entitlements_list(uid)
    return None if ent.has_access(rows, ent.DEV_PRODUCTS) else _gate_denied()


def _dev_plans(rows) -> list:
    by_product = {r.get("product"): r for r in rows or []}
    out = []
    for product in ent.DEV_PRODUCTS:
        plan = user_subs.PLANS[product]
        state = ent.effective(by_product[product])["state"] if product in by_product else "none"
        out.append({"product": product, "label": plan["label"], "price_usd": plan["price_usd"],
                    "period_days": plan["period_days"], "state": state})
    return out


async def dev_status(uid, q, db):
    """Works for everyone. Only whitelisted fields; the browser uses `unlocked` to pick the screen."""
    rows = await db.entitlements_list(uid)
    unlocked = ent.has_access(rows, ent.DEV_PRODUCTS)
    exp = ent.latest_expiry(rows, ent.DEV_PRODUCTS)
    return {"unlocked": unlocked, "expires_at": exp.isoformat() if (exp and unlocked) else None,
            "export_available": ent.export_allowed(rows), "plans": _dev_plans(rows),
            "features": [dict(f) for f in FEATURES]}


async def dev_overview(uid, q, db):
    gate = await require_dev(uid, db)
    if gate:
        return gate
    return {"features": [dict(f) for f in FEATURES], "weekly_bot_chats": WEEKLY_BOT_CHATS}


ROUTES = {"dev_status": dev_status, "dev_overview": dev_overview}
WRITES = {}
