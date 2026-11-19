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
        if msg == "product mismatch":  # show what Gumroad says was bought, so the product id/link in config can be fixed
            msg = (f"product mismatch (Gumroad product_id `{sale.get('product_id')}`, "
                   f"link `{sale.get('short_url') or sale.get('product_permalink') or sale.get('permalink')}`)")
        results[ref] = msg
        logger.info("[payment-recover] %s -> %s", ref, msg)
    return results


async def audit_sales(days: int = RECOVER_DAYS) -> List[dict]:
    """Every real Gumroad sale of the last `days`, with the bot's record of it.
    state: ok (unlocked) | missed (paid, our checkout not completed) | no_ref (no bot checkout matches)."""
    import json
    if not config.GUMROAD_ACCESS_TOKEN:
        return []
    from database import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = [dict(r) for r in await conn.fetch(
            "SELECT payment_id, user_id, chat_id, payment_type, status, paystack_reference FROM payment_logs "
            "WHERE provider = 'gumroad' AND created_date > NOW() - make_interval(days => $1)", days + 1)]
    out: List[dict] = []
    for s in await _fetch_sales(days):
        if s.get("refunded") or s.get("disputed") or s.get("chargedback") or s.get("test"):
            continue
        text = json.dumps(s, default=str)
        row = next((r for r in rows if r.get("paystack_reference") and str(r["paystack_reference"]) in text), None)
        state = "no_ref" if not row else ("ok" if row["status"] == "completed" else "missed")
        out.append({
            "sale_id": str(s.get("id") or ""), "product": str(s.get("product_name") or "?"),
            "price": (s.get("price") or 0) / 100 if isinstance(s.get("price"), (int, float)) else 0,
            "email": str(s.get("email") or ""), "when": str(s.get("created_at") or "")[:16],
            "state": state, "guild_id": (row or {}).get("chat_id"), "user_id": (row or {}).get("user_id"),
            "type": (row or {}).get("payment_type"), "ref": (row or {}).get("paystack_reference"),
        })
    order = {"missed": 0, "no_ref": 1, "ok": 2}
    out.sort(key=lambda x: order[x["state"]])
    return out


def format_audit(items: List[dict], guild_name=lambda g: None) -> List[str]:
    """Plain-text report split into Discord-sized chunks."""
    if not items:
        return ["No paid Gumroad sales found (or `GUMROAD_ACCESS_TOKEN` isn't set)."]
    n = {k: sum(1 for i in items if i["state"] == k) for k in ("ok", "missed", "no_ref")}
    lines = [f"**Gumroad paid, last {RECOVER_DAYS} days:** {len(items)} sales. ✅ {n['ok']} unlocked, ❌ {n['missed']} MISSED, ⚠️ {n['no_ref']} no bot checkout"]
    icon = {"ok": "✅", "missed": "❌", "no_ref": "⚠️"}
    for i in items:
        gid = i["guild_id"]
        server = f"{guild_name(gid) or 'unknown server'} (`{gid}`)" if gid else "no server"
        lines.append(f"{icon[i['state']]} `{i['sale_id']}` · {i['product']} · ${i['price']:.2f} · {server} · {i['when']}")
    chunks, cur = [], ""
    for ln in lines:
        if len(cur) + len(ln) + 1 > 1900:
            chunks.append(cur)
            cur = ""
        cur += ln + "\n"
    chunks.append(cur)
    return chunks
