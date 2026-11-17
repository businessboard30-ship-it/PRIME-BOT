"""Payment health watch: DMs the owner when payments look broken (red) and when they recover (green).

Why it exists: a Gumroad sale only unlocks when Gumroad's "Ping" reaches /api/gumroad_webhook. If the Ping URL in
Gumroad points at the wrong host, nothing arrives, nothing is rejected, nothing is recorded: the buyer paid, the bot
never knew. This compares Gumroad's own sales list with the pending checkouts in the database, so that case is caught.

It also checks the API answers, the secrets are set and the Gumroad token works. Never logs or sends a secret.
"""
import asyncio
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import aiohttp

import config

logger = logging.getLogger(__name__)

MIN_AGE_MINUTES = 15          # a normal checkout unlocks within a minute or two
MAX_AGE_HOURS = 72            # older pending rows are abandoned checkouts, not missed sales
SALES_LOOKBACK_DAYS = 4
REMIND_SECONDS = 6 * 3600     # a still-red state is repeated this often
HTTP_TIMEOUT = 10
PING_KEY = "gumroad_last_ping_at"
STATE_KEY = "payment_watch_state"
GUMROAD_API = "https://api.gumroad.com/v2"


@dataclass(frozen=True)
class Issue:
    level: str   # "red" | "yellow"
    key: str     # stable id: tells "something changed" from "same problem as before"
    text: str


# ── recorded by the webhook ───────────────────────────────────────────────

async def record_ping() -> None:
    """Called for every Gumroad ping that reaches the bot, so the watch knows pings are arriving. Best effort."""
    try:
        from database import db
        await db.set_global_setting(PING_KEY, str(int(time.time())))
    except Exception:
        logger.debug("[payment-watch] couldn't record the ping time", exc_info=True)


# ── pure logic (unit-tested) ──────────────────────────────────────────────

def find_paid_matches(pending: List[dict], sales: List[dict]) -> Dict[str, dict]:
    """reference -> Gumroad sale, for pending checkouts whose reference appears in a real (not refunded) sale.
    Matches on the text of the whole sale, so it doesn't depend on which field Gumroad puts the reference in."""
    texts = []
    for s in sales or []:
        if not isinstance(s, dict) or s.get("refunded") or s.get("disputed") or s.get("chargedback"):
            continue
        texts.append((s, json.dumps(s, default=str)))
    out: Dict[str, dict] = {}
    for row in pending or []:
        ref = row.get("paystack_reference")
        if not ref:
            continue
        for sale, text in texts:
            if str(ref) in text:
                out[str(ref)] = sale
                break
    return out


def classify(f: dict) -> List[Issue]:
    """Turns the gathered facts into red/yellow issues. Pure: no network, no database."""
    issues: List[Issue] = []
    base = f.get("base_url") or "<api-host>"
    ping_url = f"{base}/api/gumroad_webhook?secret=<GUMROAD_WEBHOOK_SECRET>"

    if not f.get("secret_set"):
        issues.append(Issue("red", "no_secret", "`GUMROAD_WEBHOOK_SECRET` isn't set, so every Gumroad ping is rejected."))
    if f.get("api_ok") is False:
        issues.append(Issue("red", "api_down", f"The API at {base} didn't answer ({f.get('api_detail') or 'no reply'}). "
                                               "Gumroad pings can't reach the bot."))
    ts = f.get("token_status")
    if not f.get("token_set"):
        issues.append(Issue("yellow", "no_token", "`GUMROAD_ACCESS_TOKEN` isn't set: sales can't be verified or cross-checked."))
    elif ts == "rejected":
        issues.append(Issue("red", "token_bad", "Gumroad rejected `GUMROAD_ACCESS_TOKEN`: sales can't be verified, so paid sales "
                                                "may be refused as \"not verified\"."))
    elif ts == "no_scope":
        issues.append(Issue("yellow", "token_scope", "The Gumroad token can't read sales (it needs the *view_sales* scope), "
                                                     "so missed sales can't be cross-checked."))
    elif ts == "unreachable":
        issues.append(Issue("yellow", "gumroad_unreachable", "Couldn't reach Gumroad's API from the bot just now."))

    matches = f.get("matches") or {}
    last_ping = f.get("last_ping_at")
    for ref, sale in sorted(matches.items()):
        price = sale.get("price")
        try:
            amount = f"${float(price) / 100:.2f}"
        except (TypeError, ValueError):
            amount = "?"
        issues.append(Issue("red", f"paid:{ref}",
                            f"💸 **Paid on Gumroad but NOT unlocked** — reference `{ref}`, {sale.get('product_name', '?')}, "
                            f"{amount}, sale `{sale.get('id') or sale.get('sale_id') or '?'}`."))
    if matches:
        stale = last_ping is None or last_ping < int(f.get("now") or time.time()) - 3600
        hint = ("No Gumroad ping has reached the bot in the last hour" if last_ping else "The bot has never received a Gumroad ping")
        if stale:
            issues.append(Issue("red", "ping_url", f"{hint}. The **Ping URL in Gumroad** (Settings → Advanced) is probably wrong. "
                                                   f"It must be `{ping_url}`."))

    pending = f.get("pending") or []
    unmatched = [r for r in pending if str(r.get("paystack_reference")) not in matches]
    if unmatched:
        extra = "" if f.get("sales_checked") else " (couldn't cross-check against Gumroad, so some may really be paid)"
        issues.append(Issue("yellow", "pending", f"{len(unmatched)} Gumroad checkout(s) pending for over {MIN_AGE_MINUTES} min{extra}. "
                                                 "Most are abandoned; `/admin pending` lists them."))
    return issues


def decide_dm(prev: Optional[dict], sig: str, red: bool, now: float) -> bool:
    """Whether to DM the owner: the first report ever, any change, or a still-red state after REMIND_SECONDS."""
    if not prev:
        return True
    if prev.get("sig") != sig:
        return True
    return bool(red and now - float(prev.get("at") or 0) >= REMIND_SECONDS)


def render(issues: List[Issue], recovered: bool = False) -> str:
    reds = [i for i in issues if i.level == "red"]
    yellows = [i for i in issues if i.level == "yellow"]
    if not issues:
        return ("🟢 **Payments look healthy again.**" if recovered else
                "🟢 **Payments look healthy.** The API answers, the secrets are set, the Gumroad token works and no paid sale is stuck.")
    head = "🔴 **Payment problem**" if reds else "🟡 **Payment notice**"
    body = "\n".join(f"• {i.text}" for i in reds + yellows)
    text = f"{head}\n{body}"
    return text if len(text) <= 1900 else text[:1890] + "…"


# ── gathering the facts ───────────────────────────────────────────────────

async def _gumroad_get(session: aiohttp.ClientSession, path: str, params: dict) -> Tuple[int, Optional[dict]]:
    try:
        async with session.get(f"{GUMROAD_API}{path}", params=params) as r:
            data = await r.json(content_type=None) if r.status == 200 else None
            return r.status, data if isinstance(data, dict) else None
    except Exception as e:                    # never include the URL: it carries the access token
        logger.warning("[payment-watch] Gumroad request failed (%s)", type(e).__name__)
        return 0, None


async def _pending_rows() -> List[dict]:
    from database import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT payment_id, user_id, amount, payment_type, paystack_reference, created_date FROM payment_logs "
            "WHERE provider = 'gumroad' AND status = 'pending' "
            "AND created_date < NOW() - make_interval(mins => $1) "
            "AND created_date > NOW() - make_interval(hours => $2) "
            "ORDER BY created_date DESC LIMIT 50", MIN_AGE_MINUTES, MAX_AGE_HOURS)
        return [dict(r) for r in rows]


async def gather_facts() -> dict:
    from database import db
    facts: dict = {"now": int(time.time()), "base_url": (getattr(config, "PUBLIC_BASE_URL", "") or "").rstrip("/"),
                   "secret_set": bool(config.GUMROAD_WEBHOOK_SECRET), "token_set": bool(config.GUMROAD_ACCESS_TOKEN),
                   "sales_checked": False, "token_status": None, "matches": {}, "api_ok": None, "api_detail": ""}
    timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        if facts["base_url"]:
            try:
                async with session.get(facts["base_url"] + "/") as r:
                    facts["api_ok"] = r.status < 500
                    facts["api_detail"] = f"HTTP {r.status}"
            except Exception as e:
                facts["api_ok"], facts["api_detail"] = False, type(e).__name__
        try:
            facts["pending"] = await _pending_rows()
        except Exception:
            logger.exception("[payment-watch] couldn't read pending payments")
            facts["pending"] = []
        try:
            raw = await db.get_global_setting(PING_KEY)
            facts["last_ping_at"] = int(raw) if raw else None
        except Exception:
            facts["last_ping_at"] = None
        if facts["token_set"]:
            token = {"access_token": config.GUMROAD_ACCESS_TOKEN}
            status, _ = await _gumroad_get(session, "/user", token)
            if status in (401, 403):
                facts["token_status"] = "rejected"
            elif status == 0 or status >= 500:
                facts["token_status"] = "unreachable"
            else:
                after = (datetime.now(timezone.utc) - timedelta(days=SALES_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
                sales: List[dict] = []
                page_key = None
                for _ in range(3):
                    params = dict(token, after=after)
                    if page_key:
                        params["page_key"] = page_key
                    st, data = await _gumroad_get(session, "/sales", params)
                    if st in (401, 403):
                        facts["token_status"] = "no_scope"
                        break
                    if not data:
                        facts["token_status"] = "unreachable" if st == 0 or st >= 500 else facts["token_status"]
                        break
                    sales += [s for s in (data.get("sales") or []) if isinstance(s, dict)]
                    page_key = data.get("next_page_key")
                    facts["sales_checked"] = True
                    if not page_key:
                        break
                if facts["token_status"] is None:
                    facts["token_status"] = "ok"
                if facts["sales_checked"]:
                    facts["matches"] = find_paid_matches(facts["pending"], sales)
    return facts


async def run_check(force: bool = False) -> Tuple[List[Issue], str, bool]:
    """(issues, message, send_dm). With force=True it only reports (no state change); the scheduled run decides."""
    from database import db
    facts = await gather_facts()
    issues = classify(facts)
    red = any(i.level == "red" for i in issues)
    sig = ",".join(sorted(i.key for i in issues)) or "ok"
    if force:
        return issues, render(issues), False
    prev = None
    try:
        raw = await db.get_global_setting(STATE_KEY)
        prev = json.loads(raw) if raw else None
    except Exception:
        prev = None
    now = time.time()
    send = decide_dm(prev, sig, red, now)
    recovered = bool(prev and prev.get("sig") not in (None, "ok") and sig == "ok")
    if send:
        try:
            await db.set_global_setting(STATE_KEY, json.dumps({"sig": sig, "at": now, "red": red}))
        except Exception:
            logger.exception("[payment-watch] couldn't save state")
    return issues, render(issues, recovered=recovered), send
