# path: api/dash_member.py
"""Member dashboard (#/me). Any signed-in Discord user; every query is keyed to the SESSION's user id.
No member route accepts a user id from the client (a test asserts it). Read-only in B0.

ROUTES[action] = handler(uid: str, q) -> dict. dash.py runs _require_member and the per-user rate limit first.
"""
import logging

from modules import entitlements as ent

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


ROUTES = {"member_status": member_status, "member_servers": member_servers,
          "member_prefs": member_prefs, "member_purchases": member_purchases}
WRITES = {"member_pref_set": member_pref_set}
