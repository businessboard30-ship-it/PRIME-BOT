"""
Owner-panel referral giveaway: data helpers. The screens live in
discord_bot/cogs/_views_admin_panel_referral.py. See
database/migrations/027_referral_giveaway.sql.

How it works
* Users keep using the existing /referral mycode and /referral use. When a code
  is redeemed, modules/referrals.py also writes a timestamped row to
  ad_referral_redemptions.
* A giveaway counts the redemptions that landed between its starts_at and
  ends_at. Each referred user counts once (they can only redeem once, ever).
* Ending the giveaway takes the top `winner_count` referrers (ties go to whoever
  reached that count first) and either grants a Discord role automatically
  (prize_kind 'role') or leaves the prize for the owner to hand out
  ('manual', then marked given from the panel).
* Owner accounts never win their own giveaway.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

LIST_LIMIT = 25
STANDINGS_LIMIT = 10
MAX_DAYS = 365
MAX_WINNERS = 20
_ID_RE = re.compile(r"\d{15,21}")


async def _pool():
    from database import get_pool  # lazy: keeps this module importable in tests
    return await get_pool()


# ── parsing ──────────────────────────────────────────────────────────────

def parse_int(text: str, lo: int, hi: int) -> Optional[int]:
    try:
        n = int((text or "").strip())
    except ValueError:
        return None
    return n if lo <= n <= hi else None


def parse_role_spec(text: str) -> Tuple[bool, Optional[int], Optional[int]]:
    """'' -> (True, None, None): prize is handed out by hand.
    'SERVER_ID ROLE_ID' -> (True, guild_id, role_id). Anything else -> (False, None, None)."""
    raw = (text or "").strip()
    if not raw:
        return True, None, None
    ids = _ID_RE.findall(raw)
    if len(ids) != 2 or re.sub(r"[\d\s,;/<@&>]", "", raw):
        return False, None, None
    return True, int(ids[0]), int(ids[1])


# ── giveaways ────────────────────────────────────────────────────────────

async def create_giveaway(title: str, prize: str, days: int, winner_count: int, admin_id: int,
                          guild_id: Optional[int] = None, role_id: Optional[int] = None,
                          now: Optional[datetime] = None, description: Optional[str] = None) -> int:
    now = now or datetime.now(timezone.utc)
    kind = "role" if (guild_id and role_id) else "manual"
    pool = await _pool()
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "INSERT INTO referral_giveaways (title, prize, prize_kind, guild_id, role_id, winner_count, "
            "starts_at, ends_at, created_by, description) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10) RETURNING id",
            title[:100], prize[:200], kind, guild_id, role_id, winner_count, now, now + timedelta(days=days), admin_id,
            (description or "").strip()[:1000] or None)


async def list_giveaways(limit: int = LIST_LIMIT) -> List[dict]:
    pool = await _pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM referral_giveaways ORDER BY (status = 'active') DESC, id DESC LIMIT $1", limit)
    return [_decode(dict(r)) for r in rows]


def _decode(row: dict) -> dict:
    try:
        row["winners"] = json.loads(row.get("winners_json") or "[]")
    except ValueError:
        row["winners"] = []
    return row


# ── standings / ending ───────────────────────────────────────────────────

async def standings(giveaway: dict, exclude: Optional[set] = None, limit: int = STANDINGS_LIMIT) -> List[dict]:
    """Top referrers inside the giveaway window, best first. Ties: whoever hit
    their count first (earlier last redemption) ranks higher."""
    end = giveaway["ends_at"]
    pool = await _pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT referrer_id, COUNT(*) AS n, MAX(redeemed_at) AS last_at FROM ad_referral_redemptions "
            "WHERE redeemed_at >= $1 AND redeemed_at <= $2 GROUP BY referrer_id "
            "HAVING COUNT(*) >= $3 ORDER BY n DESC, last_at ASC, referrer_id ASC LIMIT $4",
            giveaway["starts_at"], end, giveaway.get("min_referrals") or 1, limit + len(exclude or ()))
    out = [{"user_id": r["referrer_id"], "count": int(r["n"])} for r in rows
           if r["referrer_id"] not in (exclude or set())]
    return out[:limit]


async def end_giveaway(giveaway_id: int, exclude: Optional[set] = None) -> Optional[dict]:
    """Close an active giveaway and store its winners. Returns the updated row,
    or None if it was missing or already ended (so a double click changes nothing).
    Does not grant roles: call grant_winner_roles() afterwards."""
    pool = await _pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM referral_giveaways WHERE id = $1", giveaway_id)
    if row is None or row["status"] != "active":
        return None
    g = dict(row)
    top = await standings(g, exclude, limit=g["winner_count"])
    winners = [{"user_id": w["user_id"], "count": w["count"], "awarded": False, "role_ok": None} for w in top]
    async with pool.acquire() as conn:
        done = await conn.fetchrow(
            "UPDATE referral_giveaways SET status = 'ended', ended_at = NOW(), winners_json = $2 "
            "WHERE id = $1 AND status = 'active' RETURNING *", giveaway_id, json.dumps(winners))
    return _decode(dict(done)) if done else None


async def _save_winners(giveaway_id: int, winners: List[dict]) -> None:
    pool = await _pool()
    async with pool.acquire() as conn:
        await conn.execute("UPDATE referral_giveaways SET winners_json = $2 WHERE id = $1 AND status = 'ended'",
                           giveaway_id, json.dumps(winners))


async def grant_winner_roles(giveaway: dict, only_failed: bool = False) -> int:
    """Grant the prize role to winners of a 'role' giveaway. Idempotent; returns
    how many winners now have it. Failures are recorded as role_ok=False so the
    panel can offer a retry. Never raises."""
    if giveaway.get("prize_kind") != "role" or not giveaway.get("winners"):
        return 0
    from discord_bot.role_grant import grant_role
    winners, ok_count = giveaway["winners"], 0
    for w in winners:
        if only_failed and w.get("role_ok") is not False:
            ok_count += 1 if w.get("role_ok") else 0
            continue
        try:
            ok = await grant_role(giveaway["guild_id"], w["user_id"], giveaway["role_id"],
                                  reason=f"Referral giveaway #{giveaway['id']} winner")
        except Exception:
            logger.exception("[referral-giveaway] role grant crashed for %s", w.get("user_id"))
            ok = False
        w["role_ok"] = bool(ok)
        if ok:
            w["awarded"] = True
            ok_count += 1
    try:
        await _save_winners(giveaway["id"], winners)
    except Exception:
        logger.exception("[referral-giveaway] couldn't save role results for #%s", giveaway.get("id"))
    return ok_count


async def mark_awarded(giveaway_id: int, user_id: int) -> bool:
    """Record that the owner handed this winner their prize. False if there is
    no such winner or it was already marked."""
    pool = await _pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT winners_json FROM referral_giveaways WHERE id = $1 AND status = 'ended' FOR UPDATE",
                giveaway_id)
            if row is None:
                return False
            winners = json.loads(row["winners_json"] or "[]")
            hit = next((w for w in winners if w["user_id"] == user_id and not w.get("awarded")), None)
            if hit is None:
                return False
            hit["awarded"] = True
            await conn.execute("UPDATE referral_giveaways SET winners_json = $2 WHERE id = $1",
                               giveaway_id, json.dumps(winners))
    return True


# ── public post, editing, entries ────────────────────────────────────────

async def get_giveaway(giveaway_id: int) -> Optional[dict]:
    pool = await _pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM referral_giveaways WHERE id = $1", giveaway_id)
    return _decode(dict(row)) if row else None


async def get_by_message(message_id: int) -> Optional[dict]:
    pool = await _pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM referral_giveaways WHERE message_id = $1", message_id)
    return _decode(dict(row)) if row else None


async def list_posted(active_only: bool = True) -> List[dict]:
    """Giveaways that have a public post (used by the refresher)."""
    pool = await _pool()
    sql = "SELECT * FROM referral_giveaways WHERE message_id IS NOT NULL"
    if active_only:
        sql += " AND status = 'active'"
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql + " ORDER BY id DESC LIMIT 50")
    return [_decode(dict(r)) for r in rows]


async def set_post(giveaway_id: int, channel_id: Optional[int], message_id: Optional[int]) -> None:
    pool = await _pool()
    async with pool.acquire() as conn:
        await conn.execute("UPDATE referral_giveaways SET channel_id = $2, message_id = $3 WHERE id = $1",
                           giveaway_id, channel_id, message_id)


async def update_giveaway(giveaway_id: int, title: str, prize: str, description: Optional[str],
                          winner_count: int, ends_at: Optional[datetime] = None) -> Optional[dict]:
    """Edit an ACTIVE giveaway. Counting window start never moves, so nobody loses referrals."""
    pool = await _pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "UPDATE referral_giveaways SET title = $2, prize = $3, description = $4, winner_count = $5, "
            "ends_at = COALESCE($6, ends_at) WHERE id = $1 AND status = 'active' RETURNING *",
            giveaway_id, title[:100], prize[:200], (description or "").strip()[:1000] or None,
            winner_count, ends_at)
    return _decode(dict(row)) if row else None


async def entry_stats(giveaway: dict, user_id: int, exclude: Optional[set] = None) -> dict:
    """How many people this user referred inside the window, and their rank (None if 0)."""
    table = await standings(giveaway, exclude, limit=1000)
    for n, r in enumerate(table, 1):
        if r["user_id"] == user_id:
            return {"count": r["count"], "rank": n, "total_referrers": len(table)}
    return {"count": 0, "rank": None, "total_referrers": len(table)}


async def total_entries(giveaway: dict) -> int:
    """Total referrals redeemed inside the window (all referrers, owners included)."""
    pool = await _pool()
    async with pool.acquire() as conn:
        return int(await conn.fetchval(
            "SELECT COUNT(*) FROM ad_referral_redemptions WHERE redeemed_at >= $1 AND redeemed_at <= $2",
            giveaway["starts_at"], giveaway["ends_at"]) or 0)


async def set_role(giveaway_id: int, guild_id: Optional[int], role_id: Optional[int]) -> Optional[dict]:
    """Change the automatic prize role of an ACTIVE giveaway (None, None = hand the prize out yourself)."""
    kind = "role" if (guild_id and role_id) else "manual"
    pool = await _pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "UPDATE referral_giveaways SET prize_kind = $2, guild_id = $3, role_id = $4 "
            "WHERE id = $1 AND status = 'active' RETURNING *", giveaway_id, kind, guild_id, role_id)
    return _decode(dict(row)) if row else None
