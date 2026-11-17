"""Replays Gumroad sales that were paid while the API was down (so the Ping never arrived).

Finds pending Gumroad checkouts whose order reference appears in Gumroad's own sales list, then feeds each sale through the
normal ping processor. Same checks as a live ping (product, price, sale verified, atomic claim), so it is safe to run
repeatedly: an already-unlocked payment answers "already processed". Never logs or sends a secret.
"""
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, List

import aiohttp

import config
from modules.payment_watch import GUMROAD_API, find_paid_matches, _gumroad_get

logger = logging.getLogger(__name__)

RECOVER_DAYS = 14


def sale_to_ping_fields(sale: dict, reference: str) -> dict:
    """Turns a /sales entry into the form fields a live Ping would have carried."""
    s = sale or {}
    fields = {
        "sale_id": str(s.get("id") or s.get("sale_id") or ""),
        "product_id": str(s.get("product_id") or ""),
        "product_permalink": str(s.get("product_permalink") or ""),
        "permalink": str(s.get("permalink") or ""),
        "product_name": str(s.get("product_name") or ""),
        "price": str(s.get("price") if s.get("price") is not None else 0),
        "email": str(s.get("email") or ""),
        "sale_timestamp": str(s.get("created_at") or s.get("sale_timestamp") or ""),
        "url_params[reference]": str(reference),
        "test": "true" if s.get("test") else "false",
    }
    if s.get("subscription_id"):
        fields["subscription_id"] = str(s["subscription_id"])
    return fields


async def _pending_all(days: int) -> List[dict]:
    from database import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT payment_id, user_id, amount, payment_type, paystack_reference, created_date FROM payment_logs "
            "WHERE provider = 'gumroad' AND status = 'pending' "
            "AND created_date > NOW() - make_interval(days => $1) "
            "ORDER BY created_date DESC LIMIT 200", days)
        return [dict(r) for r in rows]


async def _fetch_sales(days: int) -> List[dict]:
    token = {"access_token": config.GUMROAD_ACCESS_TOKEN}
    after = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    sales: List[dict] = []
    page_key = None
    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for _ in range(10):
            params = dict(token, after=after)
            if page_key:
                params["page_key"] = page_key
            st, data = await _gumroad_get(session, "/sales", params)
            if not data:
                break
            sales += [x for x in (data.get("sales") or []) if isinstance(x, dict)]
            page_key = data.get("next_page_key")
            if not page_key:
                break
    return sales


async def recover_missed_sales(days: int = RECOVER_DAYS) -> Dict[str, str]:
    """reference -> result message of the ping processor, for every pending checkout that Gumroad shows as paid."""
    if not config.GUMROAD_ACCESS_TOKEN:
        return {}
    from gumroad_payments import process_gumroad_ping
    pending = await _pending_all(days)
    if not pending:
        return {}
    matches = find_paid_matches(pending, await _fetch_sales(days))
    results: Dict[str, str] = {}
    for ref, sale in matches.items():
        try:
            _code, msg = await process_gumroad_ping(sale_to_ping_fields(sale, ref))
        except Exception:
            logger.exception("[payment-recover] replay crashed for %s", ref)
            msg = "error"
        results[ref] = msg
        logger.info("[payment-recover] %s -> %s", ref, msg)
    return results
