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
}

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


async def ensure_products() -> bool:
    """One pass. Returns True when every plan has a usable link."""
    await load_runtime(force=True)
    missing = [p for p in PRODUCTS if not link_for(p)]
    if not missing:
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
                        await _save(ptype, prod.get("id", ""), prod["short_url"])
                        logger.info("[gumroad-auto] created %s: %s", ptype, prod["short_url"])
                        continue
                    await _notify_owner_once(ptype, f"Gumroad API said: {str(data)[:120]}")
                elif existing is None:
                    await _notify_owner_once(ptype, "couldn't read the Gumroad product list (check GUMROAD_ACCESS_TOKEN)")
    except Exception:
        logger.exception("[gumroad-auto] provisioning pass failed")
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
