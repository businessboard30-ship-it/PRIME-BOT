# path: api/dash_member.py
"""Member dashboard (#/me). Any signed-in Discord user; every query is keyed to the SESSION's user id.
No member route accepts a user id from the client (a test asserts it). Read-only in B0.

ROUTES[action] = handler(uid: str, q) -> dict. dash.py runs _require_member and the per-user rate limit first.
"""
import json
import logging
import secrets
import time

import config
from modules import entitlements as ent
from modules import user_subs

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


BOARD_PAGE = 10          # same page size as the bot's /leaderboard
BOARD_MAX_PAGES = 50     # hard cap: the query aggregates the whole XP table, so deep paging is not offered
BOARD_TTL = 30


def _board_clear():
    from api import dash
    for k in [k for k in dash._cache if isinstance(k[1], tuple) and k[1][:1] == ("lb",)]:
        dash._cache.pop(k, None)


async def member_leaderboard(uid, q, db):
    """Global XP leaderboard. Parity with the bot: same query, same page size, rank = offset + position, level = compute_level(total).
    The cached page keeps ids server-side only; the response never contains a user id."""
    from api import dash
    from modules.leveling import compute_level, xp_progress
    try:
        page = int(q("page") or "0")
    except (TypeError, ValueError):
        return {"_status": 400, "message": "That page isn't valid."}
    if not 0 <= page < BOARD_MAX_PAGES:
        return {"_status": 400, "message": "That page isn't available."}
    key = ("lb", page)
    hit = dash._cached(key, BOARD_TTL)
    if hit is None:
        rows, total = await db.get_global_xp_leaderboard_page(limit=BOARD_PAGE, offset=page * BOARD_PAGE)
        profiles = await db.board_profiles([r["user_id"] for r in rows])
        entries = []
        for i, r in enumerate(rows):
            p = profiles.get(str(r["user_id"]))
            xp = int(r["total_xp"] or 0)
            entries.append({"_uid": str(r["user_id"]), "rank": page * BOARD_PAGE + i + 1, "xp": xp, "level": compute_level(xp),
                            "name": p["name"] if p else "Hidden player", "avatar": p["avatar"] if p else None, "hidden": p is None})
        hit = {"entries": entries, "total": int(total or 0)}
        dash._store(key, BOARD_TTL, hit)
    out = [{**{k: v for k, v in e.items() if k != "_uid"}, "you": e["_uid"] == str(uid)} for e in hit["entries"]]
    me = await db.get_global_xp_rank(int(uid))
    mine = None
    if me and int(me.get("total_xp") or 0) > 0:
        xp = int(me["total_xp"])
        mine = {"rank": int(me.get("rank") or 0), "xp": xp, "level": xp_progress(xp)["level"], "players": int(me.get("total_players") or 0)}
    total = hit["total"]
    return {"entries": out, "page": page, "page_size": BOARD_PAGE, "total": total,
            "pages": min(BOARD_MAX_PAGES, max(1, -(-total // BOARD_PAGE))), "me": mine, "show_me": await db.board_visible_get(uid)}


async def member_board_pref(uid, body, db):
    show = body.get("show")
    if not isinstance(show, bool):
        return {"_status": 400, "message": "That setting isn't valid."}
    await db.board_visible_set(uid, show)
    _board_clear()
    from modules import admin_controls
    await admin_controls.record_audit(int(uid), "member_board_pref", None, f"show={show}")
    return {"show": show}
async def member_rank(uid, q, db):
    """The viewer's OWN position only (no other user is named). Global rank is the same query the bot's /rank uses."""
    from modules.leveling import xp_progress
    n = int(uid)
    g = await db.get_global_xp_rank(n)
    servers = [server_view(r) for r in await db.member_servers(uid)]
    out = {"global": None, "servers": [], "best": None, "worst": None}
    if g and int(g.get("total_xp") or 0) > 0:
        xp = int(g["total_xp"])
        pr = xp_progress(xp)
        out["global"] = {"rank": int(g.get("rank") or 0), "players": int(g.get("total_players") or 0), "total_xp": xp,
                         "level": pr["level"], "xp_in_level": pr["current_xp_in_level"], "xp_for_next": pr["xp_needed_for_next_level"]}
    ranked = [{"guild_id": s_["guild_id"], "name": s_["name"], "rank": s_["rank"], "players": s_["players"], "level": s_["level"]}
              for s_ in servers if s_["rank"] > 0]
    out["servers"] = ranked[:10]
    if ranked:
        # best = smallest rank number; worst = largest (ties broken by the bigger server)
        out["best"] = min(ranked, key=lambda r: (r["rank"], -r["players"]))
        out["worst"] = max(ranked, key=lambda r: (r["rank"], r["players"]))
    return out


CLAN_MAX_SERVERS = 10     # clan lookups run per server; keep the fan-out small
CHIEF_SEATS = 5          # seats = ranks #1-5, same as the bot (recompute_clan_chiefs)


async def member_clans(uid, q, db):
    """READ-ONLY. The viewer's own clan per server (main bot only, same servers as member_servers), whether they
    hold a chief seat, the clan's member count and the 5 seats. Seat holders appear like the web leaderboard:
    by name only if signed in and not opted out, otherwise "Hidden player". No user id is ever returned and
    nothing here can change a clan (assignment is automatic and seat-bound)."""
    import config
    from modules.clan_cards import get_clan_label
    n = int(uid)
    min_level = int(config.CHIEF_MIN_LEVEL)    # the bot's own threshold, never a copy
    servers = [server_view(r) for r in await db.member_servers(uid)][:CLAN_MAX_SERVERS]
    out = []
    for sv in servers:
        gid = int(sv["guild_id"])
        card = await db.get_clan_card_if_assigned(gid, n)
        seat = await db.get_chief_seat_for_user(gid, n)
        seats = await db.get_clan_seats(gid)
        holders = [x["user_id"] for x in seats if x.get("user_id") is not None]
        profiles = await db.board_profiles(holders) if holders else {}
        seat_rows = []
        for x in seats[:CHIEF_SEATS]:
            hid = x.get("user_id")
            filled = hid is not None
            p = profiles.get(str(hid)) if filled else None
            seat_rows.append({"seat": int(x.get("seat_rank") or 0), "clan": str(x.get("clan_slug") or "")[:40],
                              "filled": filled, "holder": (p["name"] if p else "Hidden player") if filled else None,
                              "you": bool(filled and int(hid) == n)})
        out.append({
            "guild_id": sv["guild_id"], "name": sv["name"], "level": sv["level"], "rank": sv["rank"], "players": sv["players"],
            "clan": {"name": get_clan_label(card), "members": int(await db.get_clan_member_count(gid, card) or 0)} if card else None,
            "chief": {"seat": int(seat.get("seat_rank") or 0), "clan": str(seat.get("clan_slug") or "")[:40]} if seat else None,
            "seats": seat_rows,
            "needs": {"rank": CHIEF_SEATS, "level": min_level,
                      "rank_ok": 0 < sv["rank"] <= CHIEF_SEATS, "level_ok": sv["level"] >= min_level}})
    return {"servers": out}


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


def _plans_view(rows) -> list:
    """Server-priced plan list for the page (price and period never come from the client)."""
    by_product = {r.get("product"): r for r in rows or []}
    out = []
    for product, plan in user_subs.PLANS.items():
        e = ent.effective(by_product[product]) if product in by_product else {"access": False, "state": "none"}
        out.append({"product": product, "label": plan["label"], "price_usd": plan["price_usd"],
                    "period_days": plan["period_days"], "state": e["state"]})
    return out


async def pay_view(db) -> list:
    """The payment buttons the dashboard shows. Follows the bot's payment mode (split / Paystack only / Gumroad only)."""
    try:
        mode = await db.get_payment_mode(None)
    except Exception:
        logger.warning("pay_view: payment mode lookup failed, defaulting to split", exc_info=True)
        mode = "split"
    return user_subs.pay_options(mode)


async def member_plans(uid, q, db):
    return {"plans": _plans_view(await db.entitlements_list(uid)), "pay": await pay_view(db)}


async def checkout_user(uid, body, db):
    """Start checkout for ONE plan for the SIGNED-IN user and return the gateway's OWN checkout URL
    (Paystack or Gumroad); there is no /pay hop. The user id is the session's; the price is the server's.
    Which gateways are allowed follows the bot's payment mode. Nothing is granted here: only the payment
    webhook can grant."""
    product = (body or {}).get("product")
    if not user_subs.is_plan(product):
        return {"_status": 422, "message": "Unknown plan."}
    options = await pay_view(db)
    allowed = [o["provider"] for o in options]
    provider = (body or {}).get("provider")
    if provider in (None, ""):
        if len(allowed) != 1:
            return {"_status": 422, "message": "Choose Paystack or Gumroad to pay.", "extra": {"pay": options}}
        provider = allowed[0]
    elif provider not in allowed:
        return {"_status": 422, "message": "That payment method isn't available right now."}
    from payments_manual import _create_user_plan_checkout
    intent = {"payment_type": product, "user_id": uid, "guild_id": None, "clone_id": None,
              "price_usd": user_subs.price_usd(product), "extra": {}}
    try:
        url = await _create_user_plan_checkout(intent, "GH" if provider == "paystack" else "XX")
    except Exception:
        logger.exception("checkout_user: %s checkout crashed", provider)
        url = None
    if not url or not str(url).startswith("https://"):
        return {"_status": 502, "message": "Couldn't start checkout right now. Please try again shortly."}
    return {"checkout_url": url, "provider": provider, "pay": options, "product": product,
            "price_usd": user_subs.price_usd(product), "period_days": user_subs.PLANS[product]["period_days"]}


async def member_card(uid, q, db):
    """The member's saved design + what the editor may offer. `access` only drives the UI; Save re-checks."""
    from modules import ai_usage, level_card_design as lcd
    rows = await db.entitlements_list(uid)
    raw = await db.user_card_get(uid)
    design = None
    if raw:
        try:
            design, _ = lcd.validate(json.loads(raw))
        except Exception:
            design = None
    access = ent.has_access(rows, ("card_plan",))
    from modules import card_assets
    assets = {}
    for kind in card_assets.KINDS:
        a = await db.card_asset_get(uid, kind)
        assets[kind] = {"status": a["status"], "reason": a["reason"]} if a else None
    out = {"design": design or dict(lcd.DEFAULT_DESIGN), "saved": bool(design), "access": access,
           "options": lcd.options(), "assets": assets, "prompt": card_assets.prompt_template()}
    if access:
        out["ai"] = await ai_usage.status(db, uid, "card_plan")
    return out


async def member_card_preview(uid, body, db):
    """Render the design on a placeholder avatar. Open to everyone (preview mode); nothing is stored."""
    from modules import level_card_design as lcd
    design, err = lcd.validate((body or {}).get("design"))
    if err:
        return {"_status": 422, "message": err}
    bg = logo = None
    for kind, flag in (("background", "custom_bg"), ("logo", "logo")):
        if design.get(flag):                      # the member's OWN upload, even while pending (never rejected ones)
            a = await db.card_asset_get(uid, kind)
            if a and a.get("status") != "rejected":
                if kind == "background":
                    bg = bytes(a["data"])
                else:
                    logo = bytes(a["data"])
    return {"image": await lcd.preview_data_url_async(design, bg, logo)}


async def _need_card_plan(uid, db):
    if ent.has_access(await db.entitlements_list(uid), ("card_plan",)):
        return None
    return {"_status": 402, "message": "Uploading needs the card plan. Subscribe to upload.",
            "extra": {"plans_path": "#/me/plans", "pay": await pay_view(db)}}


async def member_card_asset(uid, body, db):
    """POST {kind, data(base64)}. Plan-gated, validated, re-encoded, moderated. The user id is the session's."""
    from modules import card_assets
    kind = (body or {}).get("kind")
    if kind not in card_assets.KINDS:
        return {"_status": 422, "message": "Unknown upload type."}
    raw = card_assets.decode_b64((body or {}).get("data"))
    if raw is None:
        return {"_status": 422, "message": "Couldn't read that file."}
    gate = await _need_card_plan(uid, db)
    if gate:
        return gate
    res = await card_assets.submit(db, uid, kind, raw)
    if not res["ok"]:
        return {"_status": 422, "message": res["message"]}
    return {"status": res["status"], "reason": res["reason"]}


async def member_card_asset_delete(uid, body, db):
    from modules import card_assets
    kind = (body or {}).get("kind")
    if kind not in card_assets.KINDS:
        return {"_status": 422, "message": "Unknown upload type."}
    await db.card_asset_delete(uid, kind)
    return {}


async def member_card_save(uid, body, db):
    """Save the design for the SESSION user. Entitlement is checked here, server-side: no plan -> 402 + checkout link."""
    from modules import level_card_design as lcd
    design, err = lcd.validate((body or {}).get("design"))
    if err:
        return {"_status": 422, "message": err}
    if not ent.has_access(await db.entitlements_list(uid), ("card_plan",)):
        return {"_status": 402, "message": "The custom level-up card needs the card plan. Subscribe to save your design.",
                "extra": {"plans_path": "#/me/plans", "pay": await pay_view(db)}}
    await db.user_card_set(uid, json.dumps(design, separators=(",", ":")))
    return {"design": design}


CARD_SOURCE = "card_plan"          # counter source in user_ai_usage: 10 website-only chats per week; Discord never spends it


def _card_usage_view(st: dict) -> dict:
    from modules import ai_usage
    limit = ai_usage.LIMITS[CARD_SOURCE]
    return {"used": st["used"], "limit": limit, "remaining": max(limit - st["used"], 0), "resets_at": st["resets_at"]}


async def member_chat(uid, body, db):
    """POST {messages: [{role, content}]}: the card plan's free website chats (10 a week, bot-provided AI).
    Order matters: plan gate (402), kill switch, validate, spend one chat atomically, ask, refund on failure.
    The conversation stays in the browser; only the weekly counter is stored."""
    from modules import ai_usage, dev_chat
    if not ent.has_access(await db.entitlements_list(uid), ("card_plan",)):
        return {"_status": 402, "code": "subscription_required", "message": "The free website chats come with the card plan.",
                "extra": {"plans_path": "#/me/plans", "pay": await pay_view(db)}}
    from modules import admin_controls
    if "ai" in await admin_controls.current_switches():
        return {"_status": 503, "message": "AI chat is switched off right now."}
    messages, err = dev_chat.clean_messages((body or {}).get("messages"))
    if err:
        return {"_status": 422, "message": err}
    ws = ai_usage.week_start()
    ok, st = await ai_usage.consume(db, uid, CARD_SOURCE)
    if not ok:
        reset = ai_usage.resets_at().strftime("%a %d %b, %H:%M UTC")
        return {"_status": 429, "code": "weekly_limit",
                "message": f"You've used all {ai_usage.LIMITS[CARD_SOURCE]} free chats this week. They reset {reset}."}
    try:
        text = await dev_chat.ask(messages, system=dev_chat.MEMBER_SYSTEM_PROMPT)
    except Exception as e:
        await db.ai_usage_refund(uid, ws, CARD_SOURCE)
        if not isinstance(e, RuntimeError):
            logger.exception("member chat failed")
        return {"_status": 502, "message": str(e) if isinstance(e, RuntimeError) else "The AI service is busy. Try again in a moment."}
    return {"reply": text, **_card_usage_view(st)}


DELETE_PHRASE = "DELETE MY DATA"


async def member_usage(uid, q, db):
    """Usage meters for the plans this person actually has. Counters are the server's; nothing here can spend them."""
    from modules import ai_usage
    rows = await db.entitlements_list(uid)
    meters = []
    for source, products, label in (("card_plan", ("card_plan",), "Card plan AI chats this week"),
                                    ("dev", ent.DEV_PRODUCTS, "Developer AI chats this week")):
        if ent.has_access(rows, products):
            st = await ai_usage.status(db, uid, source)
            meters.append({"id": source, "label": label, "used": st["used"], "limit": st["limit"], "resets_at": st["resets_at"]})
    return {"meters": meters}


async def member_stepup(uid, body, db):
    """Handled in the router (it needs the session); present so the action name is registered and rate limited."""
    return {"_status": 400, "message": "Not available."}


async def member_data_delete(uid, body, db):
    """POST {confirm: "DELETE MY DATA"}. The router has already required a fresh Discord sign-in. Deletes what the
    dashboard stores about the SESSION user (never an id from the body). Payments, plans, usage counters and in-server
    records are kept; the response says so. Stored export files are removed from the storage channel first; if any
    can't be removed nothing is deleted, so the person can simply retry."""
    if ((body or {}).get("confirm") or "") != DELETE_PHRASE:
        return {"_status": 422, "message": f"Type {DELETE_PHRASE} to confirm."}
    from api import dash, dash_dev
    channel = dash_dev._storage_channel()
    receipts = await db.dev_export_list(uid, 1000)
    if receipts and not channel:
        return {"_status": 502, "message": "Couldn't remove your saved exports right now. Try again later."}
    for r in receipts:
        try:
            status = await dash._bot_request("DELETE", f"/channels/{channel}/messages/{r['message_id']}")
        except Exception:
            status = None
        if status not in (200, 204, 404):
            return {"_status": 502, "message": "Couldn't remove your saved exports right now. Nothing was deleted; try again in a moment."}
    counts = await db.member_data_delete(uid)
    logger.info("member data delete: user=%s removed=%s", uid, {k: v for k, v in counts.items() if v})
    return {"deleted": True, "removed": counts, "kept": ["payments and plans", "weekly usage counters", "XP, coins and moderation records in servers"]}


ROUTES = {"member_status": member_status, "member_servers": member_servers, "member_rank": member_rank, "member_clans": member_clans, "member_leaderboard": member_leaderboard, "member_plans": member_plans,
          "member_prefs": member_prefs, "member_purchases": member_purchases, "member_card": member_card, "member_usage": member_usage}
WRITES = {"member_pref_set": member_pref_set, "member_board_pref": member_board_pref, "checkout_user": checkout_user,
          "member_card_preview": member_card_preview, "member_card_save": member_card_save,
          "member_card_asset": member_card_asset, "member_card_asset_delete": member_card_asset_delete,
          "member_chat": member_chat,
          "member_stepup": member_stepup, "member_data_delete": member_data_delete}
