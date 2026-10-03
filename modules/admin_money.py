"""
Owner-panel Batch 4 (money) data helpers: payment-failure recording and
viewer, revenue over time, premium reversal, discount codes and upcoming
premium expiries. The screens live in
discord_bot/cogs/_views_admin_panel_money.py. See
database/migrations/023_admin_money.sql.

`record_failure()` is called from live webhook code, so it NEVER raises and
never blocks for long: any error (or a slow database) is swallowed and logged.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

FAILURE_LIMIT = 25
RECORD_TIMEOUT = 3.0             # seconds; a webhook must never wait longer than this on us
DETAIL_MAX = 500
REVERSIBLE_TYPES = ("premium",)  # the only payment type with a clean, exact inverse
EXPIRY_WINDOWS = (7, 14, 30)
COUPON_MAX_LIST = 20
SPARK = "▁▂▃▄▅▆▇█"
# Anyone on the internet can hit a webhook URL with junk, so these "request was
# rejected" kinds are written at most once per window per process. Real
# reconciliation problems (mismatches, handler errors) are never throttled.
NOISY_KINDS = frozenset({"bad_signature", "bad_secret", "bad_json"})
NOISY_WINDOW = 300.0
_noisy_last: Dict[tuple, float] = {}


async def _pool():
    from database import get_pool  # lazy: keeps this module importable in tests
    return await get_pool()


def _mask(text) -> str:
    try:
        from modules.admin_ops import mask_secrets
        return mask_secrets(text)
    except Exception:
        return "[unavailable]"       # never store text we could not mask


# ── failure recording (called from the webhooks) ─────────────────────────

async def _insert_failure(source: str, kind: str, reference: Optional[str], detail: Optional[str]) -> None:
    pool = await _pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO payment_failures (source, kind, reference, detail) VALUES ($1, $2, $3, $4)",
            source[:20], kind[:60], _mask(reference)[:120] if reference else None,
            _mask(detail)[:DETAIL_MAX] if detail else None)


async def record_failure(source: str, kind: str, reference: Optional[str] = None,
                         detail: Optional[str] = None) -> bool:
    """Best-effort. Returns True if a row was written. Never raises."""
    if kind in NOISY_KINDS:
        now = time.monotonic()
        if now - _noisy_last.get((source, kind), -NOISY_WINDOW) < NOISY_WINDOW:
            return False
        _noisy_last[(source, kind)] = now
    try:
        await asyncio.wait_for(_insert_failure(source, kind, reference, detail), RECORD_TIMEOUT)
        return True
    except Exception as exc:                          # incl. timeouts: a webhook must go on
        logger.warning("[payment-failures] could not record %s/%s: %r", source, kind, exc)
        return False


def record_failure_sync(source: str, kind: str, reference: Optional[str] = None,
                        detail: Optional[str] = None) -> bool:
    """For the synchronous do_POST paths that have no running event loop."""
    try:
        return asyncio.run(record_failure(source, kind, reference, detail))
    except Exception as exc:
        logger.warning("[payment-failures] sync record failed: %r", exc)
        return False


async def list_failures(limit: int = FAILURE_LIMIT) -> List[dict]:
    pool = await _pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, source, kind, reference, detail, created_at FROM payment_failures "
            "WHERE dismissed_at IS NULL ORDER BY created_at DESC, id DESC LIMIT $1", limit)
    return [dict(r) for r in rows]


async def dismiss_failure(failure_id: int, admin_id: int) -> bool:
    pool = await _pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE payment_failures SET dismissed_by = $2, dismissed_at = NOW() "
            "WHERE id = $1 AND dismissed_at IS NULL", failure_id, admin_id)
    return result.endswith(" 1")


# ── pending / queued payments ────────────────────────────────────────────

PENDING_LIMIT = 25


async def list_pending(limit: int = PENDING_LIMIT) -> Dict[str, object]:
    """Payments still waiting (status 'pending'), newest first. Read-only.
    Returns {'rows': [...], 'total': N} so the screen can say when the list is cut."""
    pool = await _pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT payment_id, paystack_reference, user_id, amount, payment_type, provider, chat_id, "
            "created_date FROM payment_logs WHERE status = 'pending' "
            "ORDER BY created_date DESC, payment_id DESC LIMIT $1", limit)
        total = await conn.fetchval("SELECT COUNT(*) FROM payment_logs WHERE status = 'pending'")
    out = [dict(r) for r in rows]
    return {"rows": out, "total": int(total if total is not None else len(out))}


CLEAR_PENDING_HOURS = 6   # only checkouts older than this; a buyer can still be mid-payment on a fresh one


async def count_clearable_pending(hours: int = CLEAR_PENDING_HOURS) -> int:
    pool = await _pool()
    async with pool.acquire() as conn:
        return int(await conn.fetchval(
            "SELECT COUNT(*) FROM payment_logs WHERE status = 'pending' "
            "AND created_date < NOW() - ($1 || ' hours')::INTERVAL", str(hours)) or 0)


async def clear_old_pending(hours: int = CLEAR_PENDING_HOURS) -> int:
    """Marks abandoned 'pending' checkouts older than `hours` as 'expired' (rows are kept, only the
    status changes; 'awaiting_review' is never touched). Returns how many were flipped."""
    from database import db
    return int(await db.expire_old_pending_payments(hours))


# ── revenue over time ────────────────────────────────────────────────────
# payment_logs has no "completed at" column, so buckets use created_date (when
# the checkout started). Gumroad rows are logged in USD, everything else in
# GHS, so the two currencies are never added together.

def sparkline(values: List[float]) -> str:
    if not values:
        return ""
    top = max(values)
    if top <= 0:
        return SPARK[0] * len(values)
    return "".join(SPARK[min(len(SPARK) - 1, int(v / top * (len(SPARK) - 1) + 0.5))] for v in values)


def _bucket_starts(period: str, n: int, today: datetime) -> List[datetime]:
    day = today.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "week":
        monday = day - timedelta(days=day.weekday())
        return [monday - timedelta(weeks=k) for k in range(n - 1, -1, -1)]
    return [day - timedelta(days=k) for k in range(n - 1, -1, -1)]


async def revenue_series(period: str, n: int, now: Optional[datetime] = None) -> Dict[str, List[dict]]:
    """{'GHS': [{'start', 'total', 'count'}, ...], 'USD': [...]}, oldest first,
    gaps filled with zero. period is 'day' or 'week'."""
    if period not in ("day", "week"):
        raise ValueError(f"unknown period {period!r}")
    now = now or datetime.now(timezone.utc)
    starts = _bucket_starts(period, n, now)
    pool = await _pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT date_trunc('{period}', created_date) AS bucket,
                   CASE WHEN provider = 'gumroad' THEN 'USD' ELSE 'GHS' END AS currency,
                   COALESCE(SUM(amount), 0) AS total, COUNT(*) AS n
            FROM payment_logs
            WHERE status = 'completed' AND created_date >= $1
            GROUP BY 1, 2
            """, starts[0].replace(tzinfo=None))
    out: Dict[str, List[dict]] = {}
    for cur in ("GHS", "USD"):
        have = {}
        for r in rows:
            if r["currency"] == cur:
                b = r["bucket"].replace(tzinfo=None)
                have[b] = (float(r["total"]), int(r["n"]))
        out[cur] = [{"start": s, "total": have.get(s.replace(tzinfo=None), (0.0, 0))[0],
                     "count": have.get(s.replace(tzinfo=None), (0.0, 0))[1]} for s in starts]
    return out


# ── reverse a payment ────────────────────────────────────────────────────

async def find_payment(reference: str) -> Optional[dict]:
    pool = await _pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM payment_logs WHERE paystack_reference = $1", reference.strip())
    return dict(row) if row else None


def reversal_problem(row: Optional[dict]) -> Optional[str]:
    """Why this payment can't be reversed (None = it can)."""
    if not row:
        return "No payment found with that reference."
    if row.get("status") == "reversed":
        return "That payment was already reversed. Nothing to do."
    if row.get("payment_type") not in REVERSIBLE_TYPES:
        return (f"`{row.get('payment_type')}` payments can't be reversed from here: undoing them has no exact, "
                "safe inverse. Handle it by hand.")
    if row.get("status") != "completed":
        return f"Only completed payments can be reversed (this one is `{row.get('status')}`)."
    return None


async def reverse_payment(payment_id: int, admin_id: int) -> dict:
    """Exact inverse of approving a `premium` payment: take PREMIUM_DAYS back
    off the server's premium expiry and mark the payment row 'reversed' (the row is
    kept). One transaction, and the status flip is conditional on 'completed',
    so a second call changes nothing. Raises on database errors (the
    transaction rolls back, so nothing is half-done)."""
    from config import PREMIUM_DAYS
    pool = await _pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "UPDATE payment_logs SET status = 'reversed', reversed_at = NOW(), reversed_by = $2 "
                "WHERE payment_id = $1 AND status = 'completed' AND payment_type = ANY($3::text[]) RETURNING *",
                payment_id, admin_id, list(REVERSIBLE_TYPES))
            if row is None:
                return {"ok": False, "changed": False}
            new_expiry = None
            if row["chat_id"]:
                new_expiry = await conn.fetchval(
                    "UPDATE discord_guild_subscriptions "
                    "SET expires_at = expires_at - make_interval(days => $3), reminded_for = NULL, updated_at = NOW() "
                    "WHERE guild_id = $1 AND clone_id IS NOT DISTINCT FROM $2 RETURNING expires_at",
                    row["chat_id"], row.get("clone_id"), PREMIUM_DAYS)
    return {"ok": True, "changed": True, "row": dict(row), "days": PREMIUM_DAYS, "expires_at": new_expiry}


# ── discount codes (management only; checkout does not read them yet) ────

_CODE_RE = re.compile(r"^[A-Z0-9_-]{3,24}$")


def normalize_code(text: str) -> Optional[str]:
    code = (text or "").strip().upper()
    return code if _CODE_RE.match(code) else None


def parse_int(text: str, lo: int, hi: int) -> Optional[int]:
    try:
        n = int((text or "").strip())
    except ValueError:
        return None
    return n if lo <= n <= hi else None


async def create_coupon(code: str, percent_off: int, max_uses: Optional[int],
                        days_valid: Optional[int], admin_id: int) -> bool:
    """False if the code already exists."""
    expires = datetime.now(timezone.utc) + timedelta(days=days_valid) if days_valid else None
    pool = await _pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "INSERT INTO discount_codes (code, percent_off, max_uses, expires_at, created_by) "
            "VALUES ($1, $2, $3, $4, $5) ON CONFLICT (code) DO NOTHING",
            code, percent_off, max_uses, expires, admin_id)
    return result.endswith(" 1")


async def list_coupons(limit: int = COUPON_MAX_LIST) -> List[dict]:
    pool = await _pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT code, percent_off, max_uses, uses, expires_at, active FROM discount_codes "
            "ORDER BY created_at DESC LIMIT $1", limit)
    return [dict(r) for r in rows]


async def set_coupon_active(code: str, active: bool) -> bool:
    pool = await _pool()
    async with pool.acquire() as conn:
        result = await conn.execute("UPDATE discount_codes SET active = $2 WHERE code = $1", code, active)
    return result.endswith(" 1")


def coupon_state(c: dict, now: Optional[datetime] = None) -> str:
    now = now or datetime.now(timezone.utc)
    exp = c.get("expires_at")
    if exp is not None and exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    if not c.get("active"):
        return "disabled"
    if exp is not None and exp <= now:
        return "expired"
    if c.get("max_uses") is not None and c.get("uses", 0) >= c["max_uses"]:
        return "used up"
    return "active"


# ── upcoming premium expiries ────────────────────────────────────────────

async def upcoming_expiries(days: int, limit: int = 40) -> List[dict]:
    if days not in EXPIRY_WINDOWS:
        days = EXPIRY_WINDOWS[0]
    pool = await _pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT guild_id, clone_id, expires_at, (subscription_id IS NOT NULL) AS auto_renews "
            "FROM discord_guild_subscriptions "
            "WHERE expires_at > NOW() AND expires_at <= NOW() + make_interval(days => $1) "
            "ORDER BY expires_at ASC LIMIT $2", days, limit)
    return [dict(r) for r in rows]
