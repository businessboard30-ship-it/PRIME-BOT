# path: paystack_autocreate.py

"""Auto-provisioning of the Paystack recurring plans for the per-user dashboard plans.

On boot (main bot only), and then every few hours until all plans exist, this:
  1. loads any plan codes already saved in bot_global_settings (`paystack_plan:<product>`),
  2. otherwise looks for a plan with the expected name/interval in the seller's Paystack account
     (GET /plan) and adopts it,
  3. otherwise creates it (POST /plan) in GHS, the only currency the Paystack account has enabled,
  4. if Paystack refuses, DMs the owner ONCE with the exact name/amount/interval to create by hand.

Plan names are neutral (no bot name). Env vars (PAYSTACK_PLAN_CARD_PLAN etc.) still win when set.
The web service and the bot worker are separate processes, so codes are shared via the database
(load_runtime) rather than in memory only.
"""

import asyncio
import json
import logging
import time
from typing import Dict, Optional

import config
from database import db

logger = logging.getLogger(__name__)

API = "https://api.paystack.co"
SETTING_KEY = "paystack_plan:{product}"
NOTIFIED_KEY = "paystack_plan_notified:{product}"
REFRESH_SECONDS = 60
RETRY_SECONDS = 6 * 3600

# product -> neutral plan name + Paystack interval. Prices/periods come from modules.user_subs.
PLAN_SPECS = {
    "card_plan": {"name": "Custom Level-Up Card - Monthly", "interval": "monthly",
                  "description": "Custom level-up card. Renews monthly, cancel any time."},
    "dev_monthly": {"name": "Developer Mode - Monthly", "interval": "monthly",
                    "description": "Developer tools. Renews monthly, cancel any time."},
    "dev_yearly": {"name": "Developer Mode - Yearly", "interval": "annually",
                   "description": "Developer tools. Renews yearly, cancel any time."},
}
CURRENCY = "GHS"        # the Paystack account only has GHS enabled

RUNTIME: Dict[str, str] = {}      # product -> PLN_ code
_last_load = 0.0


def plan_code_for(product: str) -> str:
    """Env override first, then the auto-created/adopted code. Empty = not available (checkout fails closed)."""
    return (config.USER_PLAN_PAYSTACK_CODES.get(product) or "").strip() or RUNTIME.get(product, "")


def code_to_product() -> Dict[str, str]:
    """PLN_ code -> product, for the webhook (env codes plus auto-created ones)."""
    out = {code: product for product, code in RUNTIME.items() if code}
    out.update({code: product for product, code in config.USER_PLAN_PAYSTACK_CODES.items() if code})
    return out


async def load_runtime(force: bool = False) -> None:
    """Cheap, cached read of the saved plan codes (safe to call per request)."""
    global _last_load
    if not force and time.monotonic() - _last_load < REFRESH_SECONDS:
        return
    _last_load = time.monotonic()
    for product in PLAN_SPECS:
        try:
            raw = await db.get_global_setting(SETTING_KEY.format(product=product))
            if raw:
                RUNTIME[product] = json.loads(raw)["code"]
        except Exception:
            logger.exception("[paystack-auto] couldn't load saved plan for %s", product)


async def _save(product: str, code: str) -> None:
    RUNTIME[product] = code
    await db.set_global_setting(SETTING_KEY.format(product=product), json.dumps({"code": code}))


def _amount_minor(product: str) -> int:
    import utils.currency as fx
    from modules import user_subs
    minor, _cur = fx.usd_to_minor_units(user_subs.price_usd(product), CURRENCY)
    return minor


def _request(method: str, path: str, payload: Optional[dict] = None):
    """Blocking Paystack call (run in a thread). Returns the decoded JSON or None."""
    import requests
    headers = {"Authorization": f"Bearer {config.PAYSTACK_SECRET_KEY}", "Content-Type": "application/json"}
    r = requests.request(method, API + path, headers=headers, json=payload, timeout=20)
    try:
        return r.json()
    except ValueError:
        return None


async def _notify_owner_once(product: str, reason: str) -> None:
    key = NOTIFIED_KEY.format(product=product)
    try:
        if await db.get_global_setting(key):
            return
        spec = PLAN_SPECS[product]
        from gumroad_payments import _alert_owner
        minor = _amount_minor(product)
        await _alert_owner(
            "\U0001F6E0\ufe0f **Paystack plan needed** (couldn't auto-create: %s)\n"
            "Create a plan on dashboard.paystack.com (Products > Plans):\n"
            "\u2022 Name: `%s`\n\u2022 Amount: **%s %.2f**\n\u2022 Interval: **%s**\n"
            "Keep that exact name and interval — the bot finds it automatically within a few hours "
            "(or on next restart). No env vars or code changes needed."
            % (reason, spec["name"], CURRENCY, minor / 100, spec["interval"]))
        await db.set_global_setting(key, "1")
    except Exception:
        logger.exception("[paystack-auto] owner notice failed for %s", product)


async def ensure_plans() -> bool:
    """One pass. Returns True when every plan has a code."""
    await load_runtime(force=True)
    missing = [p for p in PLAN_SPECS if not plan_code_for(p)]
    if not missing:
        return True
    if not config.PAYSTACK_SECRET_KEY:
        logger.warning("[paystack-auto] PAYSTACK_SECRET_KEY not set; can't provision %s", missing)
        return False
    try:
        listed = await asyncio.to_thread(_request, "GET", "/plan?perPage=200")
        if not (isinstance(listed, dict) and listed.get("status")):
            for product in missing:
                await _notify_owner_once(product, "couldn't read the Paystack plan list (check PAYSTACK_SECRET_KEY)")
            return False
        existing = listed.get("data") or []
        for product in missing:
            spec = PLAN_SPECS[product]
            amount = _amount_minor(product)
            found = next((p for p in existing
                          if (p.get("name") or "").strip().lower() == spec["name"].lower()
                          and (p.get("interval") or "") == spec["interval"]
                          and (p.get("currency") or CURRENCY).upper() == CURRENCY
                          and not p.get("is_deleted") and not p.get("is_archived")), None)
            if found and found.get("plan_code"):
                await _save(product, found["plan_code"])
                logger.info("[paystack-auto] adopted existing plan for %s: %s", product, found["plan_code"])
                continue
            made = await asyncio.to_thread(_request, "POST", "/plan", {
                "name": spec["name"], "amount": amount, "interval": spec["interval"],
                "currency": CURRENCY, "description": spec["description"], "send_invoices": True})
            data = (made or {}).get("data") if isinstance(made, dict) else None
            if isinstance(made, dict) and made.get("status") and data and data.get("plan_code"):
                await _save(product, data["plan_code"])
                logger.info("[paystack-auto] created plan for %s: %s", product, data["plan_code"])
            else:
                await _notify_owner_once(product, f"Paystack said: {str(made)[:120]}")
    except Exception:
        logger.exception("[paystack-auto] provisioning pass failed")
    return all(plan_code_for(p) for p in PLAN_SPECS)


async def run_forever() -> None:
    """Background task for the main bot: retry until every plan exists."""
    while True:
        try:
            if await ensure_plans():
                return
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[paystack-auto] loop error")
        await asyncio.sleep(RETRY_SECONDS)
