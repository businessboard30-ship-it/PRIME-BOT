# path: gumroad_autocreate.py

"""Auto-provisioning of the Gumroad products (Premium Yearly/Lifetime, plus the card/Developer memberships).

On boot (main bot only) and then every few hours until both exist, this:
  1. loads any product links already saved in bot_global_settings,
  2. otherwise looks for a product with the expected name in the seller's
     Gumroad account (GET /v2/products) and adopts it,
  3. otherwise tries to create it (POST /v2/products),
  4. if Gumroad refuses creation (its public API may not allow it), DMs the
     owner ONCE per product with the exact name/price to create by hand. As
     soon as that product exists the next pass adopts it automatically — no
     env vars or code edits needed.

Links/ids are stored under `gumroad_auto:<payment_type>` so the API service and
the bot worker (separate processes) both see them via load_runtime().
Env vars (GUMROAD_PREMIUM_YEARLY_LINK etc.) still win when set.
"""

import asyncio
import json
import logging
import time
from typing import Dict, Optional

import aiohttp

import config
from database import db

logger = logging.getLogger(__name__)

API = "https://api.gumroad.com/v2/products"
SETTING_KEY = "gumroad_auto:{ptype}"
NOTIFIED_KEY = "gumroad_auto_notified:{ptype}"
REFRESH_SECONDS = 60
RETRY_SECONDS = 6 * 3600

def _plan_price(product):
    from modules import user_subs
    return user_subs.price_usd(product)


# Product names are neutral on purpose (no bot name). `legacy` names are still ADOPTED if they already exist.
# kind "membership" = recurring; Gumroad's API cannot create those, so the bot adopts one by name or DMs the owner
# the exact name/price/period to create by hand (once).
PRODUCTS = {
    "premium_yearly": {
        "name": "Server Premium - Yearly", "legacy": ["PRIME-BOT Premium - Yearly"], "kind": "one_time",
        "price": lambda: float(config.PREMIUM_YEARLY_FEE_USD),
        "description": "Premium for one whole Discord server, 365 days. One-time payment, every current and future premium feature included.",
    },
    "premium_lifetime": {
        "name": "Server Premium - Lifetime", "legacy": ["PRIME-BOT Premium - Lifetime"], "kind": "one_time",
        "price": lambda: float(config.PREMIUM_LIFETIME_FEE_USD),
        "description": "Premium for one whole Discord server, forever. One-time payment, every current and future premium feature included.",
    },
    "premium_plus": {
        "name": "Server Premium Plus - Monthly", "legacy": [], "kind": "membership", "period": "monthly",
        "price": lambda: float(config.PREMIUM_PLUS_FEE_USD),
        "description": "Premium Plus for one whole Discord server: everything in Premium plus much higher AI chat and "
                       "AI scam-check limits. Renews monthly, cancel any time.",
    },
    "premium_plus_yearly": {
        "name": "Server Premium Plus - Yearly", "legacy": [], "kind": "one_time",
        "price": lambda: float(config.PREMIUM_PLUS_YEARLY_FEE_USD),
        "description": "Premium Plus for one whole Discord server, 365 days. One-time payment: everything in Premium "
                       "plus much higher AI chat and AI scam-check limits.",
    },
    "level_pass": {
        "name": "Level Pass", "legacy": [], "kind": "one_time",
        "price": lambda: float(config.LEVEL_PASS_FEE_USD),
        "description": "One payment, no subscription: design your own level-up card (colours, font, background, logo) "
                       f"for {config.LEVEL_PASS_CARD_DAYS} days, plus a 2x XP boost for {config.XP_BOOST_DURATION_DAYS} days.",
    },
    "card_plan": {
        "name": "Custom Level-Up Card - Monthly", "legacy": [], "kind": "membership", "period": "monthly",
        "price": lambda: _plan_price("card_plan"),
        "description": "Design your own level-up card: colours, layout, background and logo. Renews monthly, cancel any time.",
    },
    "dev_monthly": {
        "name": "Developer Mode - Monthly", "legacy": [], "kind": "membership", "period": "monthly",
        "price": lambda: _plan_price("dev_monthly"),
        "description": "Developer tools with a weekly AI chat allowance. Renews monthly, cancel any time.",
    },
    "dev_yearly": {
        "name": "Developer Mode - Yearly", "legacy": [], "kind": "membership", "period": "yearly",
        "price": lambda: _plan_price("dev_yearly"),
        "description": "Developer tools with a weekly AI chat allowance. Renews yearly, cancel any time.",
    },
    "anime_plus": {
        "name": "Anime Feed Upgrade - Monthly", "legacy": [], "kind": "membership", "period": "monthly",
        "price": lambda: _plan_price("anime_plus"),
        "description": "Follow up to 50 anime, DM alerts and quiet hours, in any server. Renews monthly, cancel any time.",
    },
    "disguild_boost_7d": {
        "name": "Disguild Boost - 7 days", "legacy": [], "kind": "one_time",
        "price": lambda: float(config.DISGUILD_BOOST_7D_FEE_USD),
        "description": "Boost one approved listing to the top of its category on Disguild for 7 days. One-time payment, no subscription.",
    },
    "disguild_boost_30d": {
        "name": "Disguild Boost - 30 days", "legacy": [], "kind": "one_time",
        "price": lambda: float(config.DISGUILD_BOOST_30D_FEE_USD),
        "description": "Boost one approved listing to the top of its category on Disguild for 30 days. One-time payment, no subscription.",
    },
    "disguild_donate": {
        "name": "Disguild Donation", "legacy": [], "kind": "one_time",
        "price": lambda: float(config.DISGUILD_DONATE_FEE_USD),
        "description": "A voluntary donation to support Disguild. It does not unlock anything.",
    },
}

# payment_type -> the Cloudflare Pages variable the site needs for it (Disguild Phase 7/33).
DISGUILD_ENV = {
    "disguild_boost_7d": "GUMROAD_BOOST_7D_URL",
    "disguild_boost_30d": "GUMROAD_BOOST_30D_URL",
    "disguild_donate": "GUMROAD_DONATE_URL",
}
DISGUILD_SENT_KEY = "gumroad_auto_disguild_links_sent"

# payment_type -> {"id": str, "url": str}; filled by load_runtime()/ensure_products()
RUNTIME: Dict[str, dict] = {}
_last_load = 0.0


def _cents(ptype: str) -> int:
    return int(round(float(PRODUCTS[ptype]["price"]()) * 100))


def link_for(payment_type: str) -> str:
    return config.GUMROAD_PRODUCT_LINKS.get(payment_type) or RUNTIME.get(payment_type, {}).get("url") or ""


def id_for(payment_type: str) -> str:
    return config.GUMROAD_PRODUCT_IDS.get(payment_type) or RUNTIME.get(payment_type, {}).get("id") or ""


async def load_runtime(force: bool = False) -> None:
    """Cheap, cached read of the saved product links (safe to call per request)."""
    global _last_load
    if not force and time.monotonic() - _last_load < REFRESH_SECONDS:
        return
    _last_load = time.monotonic()
    for ptype in PRODUCTS:
        try:
            raw = await db.get_global_setting(SETTING_KEY.format(ptype=ptype))
            if raw:
                RUNTIME[ptype] = json.loads(raw)
            else:
                RUNTIME.pop(ptype, None)
        except Exception:
            logger.exception("[gumroad-auto] couldn't load saved product for %s", ptype)


async def _save(ptype: str, product_id: str, url: str) -> None:
    RUNTIME[ptype] = {"id": product_id, "url": url}
    await db.set_global_setting(SETTING_KEY.format(ptype=ptype), json.dumps(RUNTIME[ptype]))


async def _list_products(session: aiohttp.ClientSession, token: str) -> Optional[list]:
    async with session.get(API, params={"access_token": token}) as r:
        data = await r.json(content_type=None)
    if not (data or {}).get("success"):
        logger.warning("[gumroad-auto] product list failed: %s", str(data)[:200])
        return None
    return data.get("products") or []


async def _enable(session: aiohttp.ClientSession, token: str, product_id: str) -> bool:
    """Gumroad's product create makes a DRAFT; publish it so the link can actually be bought. Best effort."""
    if not product_id:
        return False
    try:
        async with session.put(f"{API}/{product_id}/enable", data={"access_token": token}) as r:
            data = await r.json(content_type=None)
        ok = bool((data or {}).get("success"))
        if not ok:
            logger.warning("[gumroad-auto] couldn't publish product %s: %s", product_id, str(data)[:160])
        return ok
    except Exception:
        logger.exception("[gumroad-auto] publish call failed for %s", product_id)
        return False


async def _notify_owner_once(ptype: str, reason: str) -> None:
    key = NOTIFIED_KEY.format(ptype=ptype)
    try:
        if await db.get_global_setting(key):
            return
        spec = PRODUCTS[ptype]
        price = spec["price"]()
        from gumroad_payments import _alert_owner
        what = ("a **Membership** (recurring, billed %s)" % spec.get("period", "monthly")) if spec["kind"] == "membership" else "a one-time **Digital product**"
        await _alert_owner(
            "\U0001F6E0\ufe0f **Gumroad product needed** (%s)\n"
            "Create %s on gumroad.com/products/new:\n"
            "\u2022 Name: `%s`\n\u2022 Price: **$%s**\n\u2022 Description: %s\n"
            "Keep that exact name — the bot finds it automatically within a few hours (or on next restart). "
            "No env vars or code changes needed." % (reason, what, spec["name"], f"{price:g}", spec["description"])
        )
        await db.set_global_setting(key, "1")
    except Exception:
        logger.exception("[gumroad-auto] owner notice failed for %s", ptype)


async def _drop_deleted_disguild() -> None:
    """If a saved Disguild product was deleted (or edited away) in Gumroad, forget it so the next step recreates it."""
    saved = [p for p in DISGUILD_ENV if RUNTIME.get(p, {}).get("id") and not config.GUMROAD_PRODUCT_LINKS.get(p)]
    token = config.GUMROAD_ACCESS_TOKEN
    if not saved or not token:
        return
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as s:
            existing = await _list_products(s, token)
        if existing is None:
            return                                     # can't tell: keep what we have
        alive = {p.get("id") for p in existing if not p.get("deleted")}
        for ptype in saved:
            if RUNTIME[ptype]["id"] not in alive:
                logger.info("[gumroad-auto] %s was deleted in Gumroad; will recreate", ptype)
                RUNTIME.pop(ptype, None)
                await db.set_global_setting(SETTING_KEY.format(ptype=ptype), "")
                await db.set_global_setting(NOTIFIED_KEY.format(ptype=ptype), "")
    except Exception:
        logger.exception("[gumroad-auto] deleted-product check failed")


async def _send_disguild_links_once() -> None:
    """When all three Disguild products have links, DM the owner the details to give the site developer (once per link set)."""
    try:
        links = {p: link_for(p) for p in DISGUILD_ENV}
        if not all(links.values()):
            return
        sig = json.dumps(links, sort_keys=True)
        if await db.get_global_setting(DISGUILD_SENT_KEY) == sig:
            return
        from gumroad_payments import _alert_owner
        lines = [f"\u2022 `{DISGUILD_ENV[p]}` = {links[p]}  ({PRODUCTS[p]['name']}, ${PRODUCTS[p]['price']():g})" for p in DISGUILD_ENV]
        await _alert_owner(
            "\u2705 **Disguild Gumroad products are ready.** Send these three lines to the site developer "
            "(product links are public, not secrets):\n" + "\n".join(lines) +
            "\nIf a product shows as a draft in Gumroad, publish it. For the donation, you can turn on 'Pay what you want' in its settings."
        )
        await db.set_global_setting(DISGUILD_SENT_KEY, sig)
    except Exception:
        logger.exception("[gumroad-auto] couldn't send Disguild links")


async def ensure_products() -> bool:
    """One pass. Returns True when every plan has a usable link."""
    await load_runtime(force=True)
    await _drop_deleted_disguild()
    missing = [p for p in PRODUCTS if not link_for(p)]
    if not missing:
        await _send_disguild_links_once()
        return True
    token = config.GUMROAD_ACCESS_TOKEN
    if not token:
        logger.warning("[gumroad-auto] GUMROAD_ACCESS_TOKEN not set; can't provision %s", missing)
        return False
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as s:
            existing = await _list_products(s, token)
            for ptype in missing:
                spec = PRODUCTS[ptype]
                found = None
                names = {spec["name"].lower(), *[n.lower() for n in spec.get("legacy", [])]}
                for p in existing or []:
                    if (p.get("name") or "").strip().lower() in names and not p.get("deleted"):
                        found = p
                        break
                if found:
                    url = found.get("short_url") or ""
                    if url:
                        await _save(ptype, found.get("id", ""), url)
                        logger.info("[gumroad-auto] adopted existing product for %s: %s", ptype, url)
                        continue
                if found is None and existing is not None and spec["kind"] == "membership":
                    await _notify_owner_once(ptype, "Gumroad's API can't create recurring memberships")
                elif found is None and existing is not None:
                    async with s.post(API, data={
                        "access_token": token, "name": spec["name"],
                        "price": _cents(ptype), "description": spec["description"],
                    }) as r:
                        data = await r.json(content_type=None)
                    prod = (data or {}).get("product") if isinstance(data, dict) else None
                    if isinstance(data, dict) and data.get("success") and prod and prod.get("short_url"):
                        if not prod.get("published", True):
                            await _enable(s, token, prod.get("id", ""))
                        await _save(ptype, prod.get("id", ""), prod["short_url"])
                        logger.info("[gumroad-auto] created %s: %s", ptype, prod["short_url"])
                        continue
                    await _notify_owner_once(ptype, f"Gumroad API said: {str(data)[:120]}")
                elif existing is None:
                    await _notify_owner_once(ptype, "couldn't read the Gumroad product list (check GUMROAD_ACCESS_TOKEN)")
    except Exception:
        logger.exception("[gumroad-auto] provisioning pass failed")
    await _send_disguild_links_once()
    return all(link_for(p) for p in PRODUCTS)


async def run_forever() -> None:
    """Background task for the main bot: retry until both products exist."""
    while True:
        try:
            if await ensure_products():
                return
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[gumroad-auto] loop error")
        await asyncio.sleep(RETRY_SECONDS)
