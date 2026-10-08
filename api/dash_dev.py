# path: api/dash_dev.py
"""Developer mode (#/dev). Visible to every signed-in user, usable only with an active dev entitlement.

Enforcement is SERVER-SIDE: `dev_status` works for everyone (it drives the locked screen); every other
dev_* handler must start with `gate = await require_dev(uid, db)` and return it when set (402,
code "subscription_required"). The locked screen in the browser is cosmetic and grants nothing.
Nothing here writes entitlements: only the payment webhook does. No route takes a user id from the client.
"""
import logging

from modules import ai_usage, dev_chat, dev_keys
from modules import entitlements as ent
from modules import user_subs
from api.dash_member import pay_view

logger = logging.getLogger(__name__)
SOURCE = "dev"                 # counter source in user_ai_usage; the card plan uses "card_plan"
WEEKLY_BOT_CHATS = ai_usage.LIMITS[SOURCE]     # bot-provided AI messages per week for Developer subscribers
FEATURES = (
    {"key": "chat", "label": "AI chat", "ready": True},
    {"key": "export", "label": "Export to your DMs", "ready": False},
    {"key": "keys", "label": "Bring your own AI key (Claude, Groq, OpenAI)", "ready": True},
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
            "pay": await pay_view(db), "features": [dict(f) for f in FEATURES]}


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
    conns = await db.dev_connection_list(uid)
    models = [{"id": "default", "label": "Bot AI (default)"}] + [
        {"id": c["provider"], "label": dev_keys.PROVIDERS[c["provider"]]["label"] + " (your key)"}
        for c in conns if c.get("provider") in dev_keys.PROVIDERS]
    return {**_usage_view(await ai_usage.status(db, uid, SOURCE)), "models": models}


async def dev_chat_send(uid, body, db):
    """POST {messages: [{role, content}]}. The conversation is kept by the browser, never stored here.
    Order matters: gate, kill switch, validate, spend one chat atomically, call the model, refund on failure."""
    gate = await require_dev(uid, db)
    if gate:
        return gate
    model = str((body or {}).get("model") or "default").strip().lower()
    if model != "default":
        return await _chat_with_own_key(uid, body, db, model)
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


async def _chat_with_own_key(uid, body, db, provider):
    """The person's own key: server to provider only, NOT counted against the weekly 50, never returned."""
    provider = dev_keys.clean_provider(provider)
    if not provider:
        return {"_status": 422, "message": "Pick a model from the list."}
    messages, err = dev_chat.clean_messages((body or {}).get("messages"))
    if err:
        return {"_status": 422, "message": err}
    enc = await db.dev_connection_secret(uid, provider)
    key = _decrypt(enc) if enc else None
    if not key:
        return {"_status": 409, "code": "no_key", "message": "Add that key in Keys first."}
    try:
        text = await dev_keys.chat(provider, key, dev_chat.SYSTEM_PROMPT, messages)
    except RuntimeError as e:
        return {"_status": 502, "message": str(e)}
    except Exception:
        logger.error("dev own-key chat failed (provider=%s)", provider)       # no exception text: it could echo request data
        return {"_status": 502, "message": "The AI service is busy. Try again in a moment."}
    return {"reply": text, "own_key": True, "provider": provider}


def _decrypt(enc):
    from utils.crypto import secret_manager
    return secret_manager.decrypt(enc)


async def dev_keys_list(uid, q, db):
    gate = await require_dev(uid, db)
    if gate:
        return gate
    rows = await db.dev_connection_list(uid)
    return {"connections": [dev_keys.public_view(r) for r in rows if r.get("provider") in dev_keys.PROVIDERS],
            "providers": [{"id": k, "label": v["label"]} for k, v in dev_keys.PROVIDERS.items()],
            "kept_days_after_expiry": dev_keys.GRACE_DAYS}


async def dev_key_save(uid, body, db):
    """Add or replace one provider key. Step-up (fresh Discord sign-in) is enforced by the router before this runs.
    Validates with one free call, then stores ciphertext. The key is never returned, logged or put in an error."""
    gate = await require_dev(uid, db)
    if gate:
        return gate
    provider = dev_keys.clean_provider((body or {}).get("provider"))
    if not provider:
        return {"_status": 422, "message": "Pick a provider."}
    key, err = dev_keys.clean_key((body or {}).get("key"))
    if err:
        return {"_status": 422, "message": err}
    existing = {c["provider"] for c in await db.dev_connection_list(uid)}
    if provider not in existing and len(existing) >= dev_keys.MAX_CONNECTIONS:
        return {"_status": 422, "message": "You've reached the limit of saved keys."}
    ok, verr = await dev_keys.validate(provider, key)
    if not ok:
        return {"_status": 422, "message": verr}
    from utils.crypto import secret_manager
    await db.dev_connection_upsert(uid, provider, secret_manager.encrypt(key), dev_keys.last4(key))
    rows = await db.dev_connection_list(uid)
    return {"connections": [dev_keys.public_view(r) for r in rows if r.get("provider") in dev_keys.PROVIDERS]}


async def dev_key_remove(uid, body, db):
    gate = await require_dev(uid, db)
    if gate:
        return gate
    provider = dev_keys.clean_provider((body or {}).get("provider"))
    if not provider:
        return {"_status": 422, "message": "Pick a provider."}
    await db.dev_connection_delete(uid, provider)
    rows = await db.dev_connection_list(uid)
    return {"connections": [dev_keys.public_view(r) for r in rows if r.get("provider") in dev_keys.PROVIDERS]}


ROUTES = {"dev_status": dev_status, "dev_overview": dev_overview, "dev_usage": dev_usage, "dev_keys": dev_keys_list}
WRITES = {"dev_chat": dev_chat_send, "dev_key_save": dev_key_save, "dev_key_remove": dev_key_remove}
# Writes that also need a fresh Discord sign-in (step-up). The router checks this set; handlers never see the session.
FRESH_WRITES = frozenset({"dev_key_save", "dev_key_remove"})
