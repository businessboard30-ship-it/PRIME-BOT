# path: api/dash_dev.py
"""Developer mode (#/dev). Visible to every signed-in user, usable only with an active dev entitlement.

Enforcement is SERVER-SIDE: `dev_status` works for everyone (it drives the locked screen); every other
dev_* handler must start with `gate = await require_dev(uid, db)` and return it when set (402,
code "subscription_required"). The locked screen in the browser is cosmetic and grants nothing.
Nothing here writes entitlements: only the payment webhook does. No route takes a user id from the client.
"""
import logging

from modules import ai_usage, dev_chat
from modules import entitlements as ent
from modules import user_subs

logger = logging.getLogger(__name__)
SOURCE = "dev"                 # counter source in user_ai_usage; the card plan uses "card_plan"
WEEKLY_BOT_CHATS = ai_usage.LIMITS[SOURCE]     # bot-provided AI messages per week for Developer subscribers
FEATURES = (
    {"key": "chat", "label": "AI chat", "ready": True},
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


def _usage_view(st: dict) -> dict:
    limit = WEEKLY_BOT_CHATS
    return {"used": st["used"], "limit": limit, "remaining": max(limit - st["used"], 0), "resets_at": st["resets_at"]}


async def dev_usage(uid, q, db):
    gate = await require_dev(uid, db)
    if gate:
        return gate
    return {**_usage_view(await ai_usage.status(db, uid, SOURCE)),
            "models": [{"id": "default", "label": "Bot AI (default)"}]}


async def dev_chat_send(uid, body, db):
    """POST {messages: [{role, content}]}. The conversation is kept by the browser, never stored here.
    Order matters: gate, kill switch, validate, spend one chat atomically, call the model, refund on failure."""
    gate = await require_dev(uid, db)
    if gate:
        return gate
    from modules import admin_controls
    if "ai" in await admin_controls.current_switches():
        return {"_status": 503, "message": "AI chat is switched off right now."}
    messages, err = dev_chat.clean_messages((body or {}).get("messages"))
    if err:
        return {"_status": 422, "message": err}
    ws = ai_usage.week_start()
    ok, st = await ai_usage.consume(db, uid, SOURCE)
    if not ok:
        reset = ai_usage.resets_at().strftime("%a %d %b, %H:%M UTC")
        return {"_status": 429, "code": "weekly_limit", "message": f"You've used all {WEEKLY_BOT_CHATS} chats this week. They reset {reset}."}
    try:
        text = await dev_chat.ask(messages)
    except Exception as e:
        await db.ai_usage_refund(uid, ws, SOURCE)
        if not isinstance(e, RuntimeError):
            logger.exception("dev chat failed")
        return {"_status": 502, "message": str(e) if isinstance(e, RuntimeError) else "The AI service is busy. Try again in a moment."}
    return {"reply": text, **_usage_view(st)}


ROUTES = {"dev_status": dev_status, "dev_overview": dev_overview, "dev_usage": dev_usage}
WRITES = {"dev_chat": dev_chat_send}
