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


def _iso(v):
    return v.isoformat() if hasattr(v, "isoformat") else (str(v) if v is not None else None)


def server_view(r):
    """One server card. Snowflakes are strings (JS loses precision above 2^53)."""
    from modules.leveling import xp_progress
    xp = int(r.get("total_xp") or 0)
    pr = xp_progress(xp)
    return {"guild_id": str(r["guild_id"]), "name": str(r.get("guild_name") or "Server")[:100],
            "level": pr["level"], "total_xp": xp, "xp_in_level": pr["current_xp_in_level"],
            "xp_for_next": pr["xp_needed_for_next_level"], "rank": int(r.get("rank") or 0),
            "players": int(r.get("players") or 0),
            "coins": None if r.get("balance") is None else int(r["balance"]),
            "coin_symbol": str(r.get("currency_symbol") or "")[:8], "coin_name": str(r.get("currency_name") or "Coins")[:32],
            "ping_optout": bool(r.get("ping_optout"))}


def purchase_view(r):
    return {"amount": float(r.get("amount") or 0), "status": str(r.get("status") or ""),
            "type": str(r.get("payment_type") or "payment")[:40], "provider": str(r.get("provider") or "")[:20],
            "at": _iso(r.get("created_date"))}


async def member_servers(uid, q, db):
    return {"servers": [server_view(r) for r in await db.member_servers(uid)]}


async def member_purchases(uid, q, db):
    return {"purchases": [purchase_view(r) for r in await db.member_purchases(uid)]}


async def member_prefs(uid, q, db):
    from i18n import SUPPORTED_LANGUAGES
    from modules import ai_prefs
    from utils.currency import SUPPORTED_CURRENCIES
    n = int(uid)
    char, voice = await ai_prefs.get_prefs(n)
    return {"language": await db.get_user_language(n, 0), "languages": dict(SUPPORTED_LANGUAGES),
            "currency": await db.get_user_currency(n), "currencies": sorted(SUPPORTED_CURRENCIES),
            "character": char, "characters": {k: v["label"] for k, v in ai_prefs.CHARACTERS.items()},
            "voice": voice}


async def member_pref_set(uid, body, db):
    """POST {kind, value[, guild_id]}. kind is an allowlist; the user id is the session's, never the body's."""
    from i18n import SUPPORTED_LANGUAGES
    from modules import ai_prefs
    from utils.currency import SUPPORTED_CURRENCIES
    n, kind, value = int(uid), body.get("kind"), body.get("value")
    if kind == "language" and isinstance(value, str) and value in SUPPORTED_LANGUAGES:
        await db.set_user_language(n, value, 0)
    elif kind == "currency" and isinstance(value, str) and value.upper() in SUPPORTED_CURRENCIES:
        await db.set_user_currency(n, value.upper())
    elif kind == "character" and isinstance(value, str) and value in ai_prefs.CHARACTERS:
        await ai_prefs.set_character(n, value)
    elif kind == "voice" and value in (ai_prefs.VOICE_AUTO, ai_prefs.VOICE_OFF):
        await ai_prefs.set_voice_mode(n, value)
    elif kind == "level_ping" and isinstance(value, bool) and str(body.get("guild_id") or "").isdigit():
        gid = int(body["guild_id"])
        if gid not in {int(r["guild_id"]) for r in await db.member_servers(uid)}:   # only servers where they actually have XP
            return {"_status": 404, "message": "Server not found."}
        await db.member_level_ping_set(gid, n, value)
    else:
        return {"_status": 400, "message": "That setting isn't valid."}
    return {}


def _plans_view(rows) -> list:
    """Server-priced plan list for the page (price and period never come from the client)."""
    by_product = {r.get("product"): r for r in rows or []}
    out = []
    for product, plan in user_subs.PLANS.items():
        e = ent.effective(by_product[product]) if product in by_product else {"access": False, "state": "none"}
        out.append({"product": product, "label": plan["label"], "price_usd": plan["price_usd"],
                    "period_days": plan["period_days"], "state": e["state"]})
    return out


async def pay_view(db) -> list:
    """The payment buttons the dashboard shows. Follows the bot's payment mode (split / Paystack only / Gumroad only)."""
    try:
        mode = await db.get_payment_mode(None)
    except Exception:
        logger.warning("pay_view: payment mode lookup failed, defaulting to split", exc_info=True)
        mode = "split"
    return user_subs.pay_options(mode)


async def member_plans(uid, q, db):
    return {"plans": _plans_view(await db.entitlements_list(uid)), "pay": await pay_view(db)}


async def checkout_user(uid, body, db):
    """Start checkout for ONE plan for the SIGNED-IN user. The user id is the session's; the price
    is the server's. Returns a /pay link that detects the buyer's country (Paystack in Ghana,
    Gumroad elsewhere). Nothing is granted here: only the payment webhook can grant."""
    product = (body or {}).get("product")
    if not user_subs.is_plan(product):
        return {"_status": 422, "message": "Unknown plan."}
    base = (getattr(config, "PUBLIC_BASE_URL", "") or "").rstrip("/")
    if not base:
        logger.error("checkout_user: PUBLIC_BASE_URL is not set")
        return {"_status": 503, "message": "Checkout isn't available right now."}
    options = await pay_view(db)
    provider = (body or {}).get("provider")
    if provider in (None, ""):
        provider = options[0]["provider"] if len(options) == 1 else None      # split: the /pay page picks by country
    elif provider not in [o["provider"] for o in options]:
        return {"_status": 422, "message": "That payment method isn't available right now."}
    token = secrets.token_urlsafe(18)
    intent = {"payment_type": product, "user_id": uid, "guild_id": None, "clone_id": None,
              "price_usd": user_subs.price_usd(product), "extra": {}, "created": time.time()}
    await db.set_global_setting(f"payintent:{token}", json.dumps(intent))
    region = f"&r={user_subs.PAY_PROVIDERS[provider]['region']}" if provider else ""
    return {"checkout_url": f"{base}/pay?t={token}{region}", "provider": provider, "pay": options, "product": product,
            "price_usd": user_subs.price_usd(product), "period_days": user_subs.PLANS[product]["period_days"]}


async def member_card(uid, q, db):
    """The member's saved design + what the editor may offer. `access` only drives the UI; Save re-checks."""
    from modules import ai_usage, level_card_design as lcd
    rows = await db.entitlements_list(uid)
    raw = await db.user_card_get(uid)
    design = None
    if raw:
        try:
            design, _ = lcd.validate(json.loads(raw))
        except Exception:
            design = None
    access = ent.has_access(rows, ("card_plan",))
    from modules import card_assets
    assets = {}
    for kind in card_assets.KINDS:
        a = await db.card_asset_get(uid, kind)
        assets[kind] = {"status": a["status"], "reason": a["reason"]} if a else None
    out = {"design": design or dict(lcd.DEFAULT_DESIGN), "saved": bool(design), "access": access,
           "options": lcd.options(), "assets": assets, "prompt": card_assets.prompt_template()}
    if access:
        out["ai"] = await ai_usage.status(db, uid, "card_plan")
    return out


async def member_card_preview(uid, body, db):
    """Render the design on a placeholder avatar. Open to everyone (preview mode); nothing is stored."""
    from modules import level_card_design as lcd
    design, err = lcd.validate((body or {}).get("design"))
    if err:
        return {"_status": 422, "message": err}
    bg = logo = None
    for kind, flag in (("background", "custom_bg"), ("logo", "logo")):
        if design.get(flag):                      # the member's OWN upload, even while pending (never rejected ones)
            a = await db.card_asset_get(uid, kind)
            if a and a.get("status") != "rejected":
                if kind == "background":
                    bg = bytes(a["data"])
                else:
                    logo = bytes(a["data"])
    return {"image": await lcd.preview_data_url_async(design, bg, logo)}


async def _need_card_plan(uid, db):
    if ent.has_access(await db.entitlements_list(uid), ("card_plan",)):
        return None
    extra = {}
    started = await checkout_user(uid, {"product": "card_plan"}, db)
    if started.get("checkout_url"):
        extra["checkout_url"] = started["checkout_url"]
    return {"_status": 402, "message": "Uploading needs the card plan. Subscribe to upload.", "extra": extra}


async def member_card_asset(uid, body, db):
    """POST {kind, data(base64)}. Plan-gated, validated, re-encoded, moderated. The user id is the session's."""
    from modules import card_assets
    kind = (body or {}).get("kind")
    if kind not in card_assets.KINDS:
        return {"_status": 422, "message": "Unknown upload type."}
    raw = card_assets.decode_b64((body or {}).get("data"))
    if raw is None:
        return {"_status": 422, "message": "Couldn't read that file."}
    gate = await _need_card_plan(uid, db)
    if gate:
        return gate
    res = await card_assets.submit(db, uid, kind, raw)
    if not res["ok"]:
        return {"_status": 422, "message": res["message"]}
    return {"status": res["status"], "reason": res["reason"]}


async def member_card_asset_delete(uid, body, db):
    from modules import card_assets
    kind = (body or {}).get("kind")
    if kind not in card_assets.KINDS:
        return {"_status": 422, "message": "Unknown upload type."}
    await db.card_asset_delete(uid, kind)
    return {}


async def member_card_save(uid, body, db):
    """Save the design for the SESSION user. Entitlement is checked here, server-side: no plan -> 402 + checkout link."""
    from modules import level_card_design as lcd
    design, err = lcd.validate((body or {}).get("design"))
    if err:
        return {"_status": 422, "message": err}
    if not ent.has_access(await db.entitlements_list(uid), ("card_plan",)):
        extra = {}
        started = await checkout_user(uid, {"product": "card_plan"}, db)
        if started.get("checkout_url"):
            extra["checkout_url"] = started["checkout_url"]
        return {"_status": 402, "message": "The custom level-up card needs the card plan. Subscribe to save your design.",
                "extra": extra}
    await db.user_card_set(uid, json.dumps(design, separators=(",", ":")))
    return {"design": design}


ROUTES = {"member_status": member_status, "member_servers": member_servers, "member_plans": member_plans,
          "member_prefs": member_prefs, "member_purchases": member_purchases, "member_card": member_card}
WRITES = {"member_pref_set": member_pref_set, "checkout_user": checkout_user,
          "member_card_preview": member_card_preview, "member_card_save": member_card_save,
          "member_card_asset": member_card_asset, "member_card_asset_delete": member_card_asset_delete}
