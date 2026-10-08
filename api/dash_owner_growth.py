# path: api/dash_owner_growth.py
"""Owner area, Phase 3 remainder: Ads, Marketplace listings, Bump network.

Same contract as api/dash_owner.py: ROUTES (GET, read-only) and WRITES (POST, (section, per-minute limit,
prepare)). Nothing here decides access; api/dash.py enforces section, rate limit, step-up, typed confirm and
the fail-closed audit. Every write calls the SAME function the Discord Owner panel / `/ad manage` calls
(modules/ads_marketplace.py), so web and Discord cannot drift.

NOT built here, on purpose:
  * Bump review (approve / reject a bot listing): the Discord flow posts and edits messages through the live
    bot, which the web process does not have. It needs the owner_jobs queue.
  * Referral giveaway, approve / reject payment: excluded by the owner for this round.
"""
import logging

logger = logging.getLogger(__name__)

AD_STATUS = {"pending": ("pending",), "live": ("approved",), "paused": ("deactivated",), "rejected": ("rejected",)}
LIST_LIMIT = 25
MARKET_PAGE = 10
REASON_MAX = 200                     # same cap as the /ad manage reject form
BUMP_MAX_MINUTES = 7 * 24 * 60       # sanity cap: a typo of 99999 must not freeze bumping for months
_AD_FIELDS = ("id", "company_name", "ad_title", "ad_description", "target_url", "budget_usd", "status", "user_id",
              "submitted_at", "rejection_reason", "image_message_id")
_LISTING_FIELDS = ("id", "service_name", "service_title", "description", "price_usd", "category", "user_id",
                   "clicks", "status", "created_at")


def _ads():
    from modules import ads_marketplace as ads
    return ads


def _clip(v, n):
    return None if v is None else "".join(ch for ch in str(v) if ch.isprintable() or ch in "\n ")[:n]


def _ad_view(row: dict) -> dict:
    """Whitelisted, clipped. Ad text is written by strangers: the front end shows it with textContent only,
    and the link is shown as text, never as a clickable href."""
    out = {k: row.get(k) for k in _AD_FIELDS}
    for k, n in (("company_name", 100), ("ad_title", 200), ("ad_description", 1000), ("target_url", 300),
                 ("rejection_reason", 300)):
        out[k] = _clip(out[k], n)
    out["budget_usd"] = None if out["budget_usd"] is None else float(out["budget_usd"])
    out["has_image"] = bool(out.pop("image_message_id"))
    return out


def _listing_view(row: dict) -> dict:
    out = {k: row.get(k) for k in _LISTING_FIELDS}
    for k, n in (("service_name", 100), ("service_title", 200), ("description", 1000), ("category", 50)):
        out[k] = _clip(out[k], n)
    out["id"] = str(out["id"])
    out["price_usd"] = None if out["price_usd"] is None else float(out["price_usd"])
    return out


def _int(raw, lo=1, hi=2 ** 62):
    s = str(raw if raw is not None else "").strip()
    return int(s) if s.isdigit() and lo <= int(s) <= hi else None


# ── reads ────────────────────────────────────────────────────────────────

async def ads_list(q) -> dict:
    flt = (q("status") or "pending").strip()
    if flt not in AD_STATUS:
        return {"_error": (422, "Unknown ad filter.")}
    ads = _ads()
    counts = await ads.count_ads_by_status() or {}
    rows = await ads.search_ads(None, "", AD_STATUS[flt], LIST_LIMIT) or []
    return {"filter": flt, "counts": counts, "rows": rows, "limit": LIST_LIMIT}


async def ad(q) -> dict:
    ad_id = _int(q("ad_id"))
    if ad_id is None:
        return {"_error": (422, "Bad ad id.")}
    row = await _ads().get_ad(ad_id)
    if not row:
        return {"_error": (404, "No such ad.")}
    return {"ad": _ad_view(row)}


async def market(q) -> dict:
    page = max(0, min(_int(q("page") or "0", 0, 10_000) or 0, 10_000))
    rows = await _ads().get_marketplace_listings(MARKET_PAGE, page * MARKET_PAGE) or []
    return {"page": page, "page_size": MARKET_PAGE, "rows": [_listing_view(r) for r in rows],
            "has_more": len(rows) >= MARKET_PAGE}


async def bump(q) -> dict:
    from api import dash
    db = dash.db
    raw = await db.get_config("bump_cooldown_seconds")
    try:
        cooldown = max(60, int(raw)) if raw is not None else None
    except (TypeError, ValueError):
        cooldown = None
    guilds = await db.bump_list_configured_guilds(None) or []     # main bot; clones keep their own lists
    pending = await db.bump_list_pending_listings(None) or []
    return {"cooldown_seconds": cooldown, "guilds": guilds[:100], "guild_total": len(guilds),
            "pending": [{"id": p.get("id"), "name": _clip(p.get("name"), 100),
                         "description": _clip(p.get("description"), 500), "guild_id": p.get("guild_id"),
                         "verified_owner_id": p.get("verified_owner_id"), "application_id": p.get("application_id"),
                         "created_at": p.get("created_at")} for p in pending[:25]],
            "pending_total": len(pending)}


# ── writes (prepare -> plan) ─────────────────────────────────────────────

def _actor(sess) -> int:
    return int(sess["user"]["id"])


async def _need_ad(body):
    ad_id = _int(body.get("ad_id"))
    if ad_id is None:
        return None, {"_error": (422, "Bad ad id.")}
    row = await _ads().get_ad(ad_id)
    if not row:
        return None, {"_error": (404, "No such ad.")}
    return (ad_id, row), None


def _state_plan(act, fn_name, needs):
    async def prep(sess, body) -> dict:
        got, err = await _need_ad(body)
        if err:
            return err
        ad_id, row = got
        if row.get("status") != needs:
            return {"_error": (409, f"That ad is {row.get('status')}, not {needs}. Refresh the list.")}
        async def fn():
            ok = bool(await getattr(_ads(), fn_name)(ad_id))
            return {"changed": ok, "ad_id": ad_id,
                    "message": "Done." if ok else "Nothing changed: someone else already handled it, or the database was busy."}
        return {"target": str(ad_id), "detail": {"op": act}, "fn": fn}
    return prep


prep_ad_approve = _state_plan("approve", "approve_ad", "pending")
prep_ad_deactivate = _state_plan("deactivate", "deactivate_ad", "approved")
prep_ad_reactivate = _state_plan("reactivate", "reactivate_ad", "deactivated")


async def _dm_rejection(ad_row: dict, reason: str) -> bool:
    """Best effort over Discord REST with the bot token (the web service has no bot client). A failed or
    impossible DM never hides or undoes a rejection that already succeeded."""
    try:
        import aiohttp
        from config import DISCORD_BOT_TOKEN
        from api.cron_discord_owner_broadcast import _dm_user
        if not DISCORD_BOT_TOKEN:
            return False
        text = (f"❌ Your ad #{ad_row['id']} was not approved\n"
                f"**{_clip(ad_row.get('company_name'), 100)} — {_clip(ad_row.get('ad_title'), 200)}**\n\n"
                f"**Reason:** {reason}\n\n"
                f"You can fix the issue and submit a new ad with `/ad submit`, or check `/ad status ad_id:{ad_row['id']}`.")
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
            return await _dm_user(s, DISCORD_BOT_TOKEN, int(ad_row["user_id"]), text) is None
    except Exception:
        logger.exception("owner: rejection DM failed for ad %s", ad_row.get("id"))
        return False


async def prep_ad_reject(sess, body) -> dict:
    got, err = await _need_ad(body)
    if err:
        return err
    ad_id, row = got
    reason = " ".join(str(body.get("reason") or "").split())[:REASON_MAX]
    if not reason:
        return {"_error": (422, "Give a reason. The submitter sees it.")}
    if row.get("status") != "pending":
        return {"_error": (409, f"That ad is {row.get('status')}, not pending. Refresh the list.")}
    async def fn():
        ok = bool(await _ads().reject_ad(ad_id, reason))
        if not ok:
            return {"changed": False, "message": "Nothing changed: someone else already handled it, or the database was busy."}
        sent = await _dm_rejection(row, reason)
        return {"changed": True, "ad_id": ad_id, "dm_sent": sent,
                "message": "Rejected. Submitter notified by DM." if sent else
                           "Rejected. Couldn't DM the submitter (DMs closed); they can see the reason in /ad status."}
    return {"target": str(ad_id), "detail": {"op": "reject", "reason": reason[:80]}, "fn": fn}


async def prep_listing_remove(sess, body) -> dict:
    lid = str(body.get("listing_id") or "").strip()
    if not lid or len(lid) > 64 or not all(c.isalnum() or c in "-_" for c in lid):
        return {"_error": (422, "Bad listing id.")}
    listing = await _ads().get_listing(lid)
    if not listing or listing.get("status") != "active":
        return {"_error": (404, "That listing is already gone.")}
    seller = listing["user_id"]
    async def fn():
        # deactivate_listing is scoped to the seller, so the real seller's id is passed (same as Discord).
        ok = bool(await _ads().deactivate_listing(seller, lid))
        return {"removed": ok, "message": "Listing removed." if ok else "Nothing changed: already gone or the database was busy."}
    # No "put back" exists for a removed listing, so this one needs a fresh sign-in and a typed word.
    return {"target": lid, "detail": {"seller": str(seller)}, "fn": fn, "fresh": True, "confirm": "REMOVE"}


async def prep_bump_cooldown(sess, body) -> dict:
    minutes = _int(body.get("minutes"), 1, BUMP_MAX_MINUTES)
    if minutes is None:
        return {"_error": (422, f"Minutes must be a whole number from 1 to {BUMP_MAX_MINUTES}.")}
    async def fn():
        from api import dash
        await dash.db.update_config("bump_cooldown_seconds", minutes * 60)
        return {"minutes": minutes, "message": "Saved. The bot reads it on the next bump."}
    return {"target": f"{minutes}m", "detail": {"minutes": minutes}, "fn": fn}


WRITES = {
    "owner_ad_approve": ("ads", 20, prep_ad_approve),
    "owner_ad_reject": ("ads", 20, prep_ad_reject),
    "owner_ad_deactivate": ("ads", 20, prep_ad_deactivate),
    "owner_ad_reactivate": ("ads", 20, prep_ad_reactivate),
    "owner_listing_remove": ("ads", 10, prep_listing_remove),
    "owner_bump_cooldown": ("bump", 10, prep_bump_cooldown),
}

ROUTES = {
    "owner_ads": ("ads", ads_list),
    "owner_ad": ("ads", ad),
    "owner_market": ("ads", market),
    "owner_bump": ("bump", bump),
}
