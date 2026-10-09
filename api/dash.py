# path: api/dash.py

"""
Backend for the web dashboard (dashboard/ on Cloudflare Pages).

Sign-in is Discord OAuth2 (identify + guilds). The browser holds only an opaque
session id and sends it as `Authorization: Bearer <id>`; no cookies, so there is no
CSRF surface. The OAuth access token is used once to read the user's server list and
is never stored.

Every read and write is authorised against Discord itself, not against the cached
server list: the bot looks the user up in the guild and checks Administrator /
Manage Server (or ownership). A stale session can't keep editing a server the user
lost access to. Only settings declared in utils/dash_schema.py can be written, each
validated, and channel/role ids must belong to that guild.

Routes (all on /api/dash):
  GET  ?action=login                    -> redirect to Discord
  GET  ?code&state                      -> OAuth callback -> redirect to the dashboard
  GET  ?action=schema                   -> setting definitions (public)
  GET  ?action=me                       -> user + servers they can manage
  GET  ?action=guild&guild_id           -> overview + module status
  GET  ?action=meta&guild_id            -> channels and roles
  GET  ?action=config&guild_id&module   -> current values
  POST {action: save, guild_id, module, values}
  GET  ?action=announcements&guild_id   -> upcoming /announce posts for this server (same page as Scheduled messages)
  POST {action: announcement_add, guild_id, channel_id, content, mode, minutes|time_utc} / {action: announcement_delete, guild_id, id} -> audited, 30 writes/min
  POST {action: logout}
  POST {action: reset, guild_id, module} -> put one module back to factory settings (audited)
  GET  ?action=export&guild_id&module   -> downloadable settings file for one module
  POST {action: import_check, guild_id, module, data} -> validate an uploaded file, returns values to load as unsaved changes
  POST {action: bot_action, guild_id, id} -> bot posts a panel/test card (whitelist: S.BOT_ACTIONS)
  GET  ?action=billing&guild_id         -> Premium status + plans with server-side prices
  POST {action: checkout, guild_id, plan} -> {url}: existing /pay redirect bound to this server
  GET  ?action=audit&guild_id[&before][&module] -> change history for a server (newest first)
  GET  ?action=moderation&guild_id=&user_id=&kind=&before= -> cases from moderation_logs (+ that user's warns and count)
  POST {action: warn_remove, guild_id, user_id, warn_id} -> removes ONE warn (needs Timeout Members like /unwarn; audited; logged as an unwarn case). "Clear all" stays on Discord (/unwarn)
  GET  ?action=analytics&guild_id[&days=7|30|90] -> joins/leaves per day, active members, top XP members and inviters; read-only, no schema change (30 reads/min)
  GET  ?action=giveaways&guild_id=&status=&before= -> giveaways for this server (prize, status, entrant count, winners); read-only
  GET  ?action=welcome_preview&guild_id&theme&shape&use_template&bg&accent -> rendered welcome card (data URL)
  GET  ?action=friends_list -> friends, incoming/outgoing requests, blocked, my servers, unread, settings (Part D; session user only)
  GET  ?action=friends_search&guild_id&query -> people in ONE of my servers who use the web and allow requests (max 10)
  GET  ?action=messages_thread&other_id -> last 50 messages with an accepted friend (+ can_send / paused)
  POST ?action=friend_request {other_id} -> same reply whether or not it could be delivered (no web-account leak)
  POST ?action=friend_respond {other_id, accept} | friend_remove | friend_block | friend_unblock {other_id}
  POST ?action=message_send {other_id, body} -> friends only; text, Scam Shield, limits, shared-server check (409 paused)
  POST ?action=message_read | message_thread_delete {other_id} | message_report {other_id, also_block?} | msg_prefs_set {allow_requests?, dm_notify?}
  GET  ?action=dropbox                  -> drop box messages + unread count (any signed-in admin)
  POST {action: dropbox_read, id?}      -> mark one (or all) read
  POST {action: dropbox_send, ...}      -> OWNER ONLY (config.DISCORD_OWNER_BROADCAST_IDS)
  POST {action: dropbox_delete, id}     -> OWNER ONLY
  GET  ?action=dropbox_delivery&id      -> OWNER ONLY: DM sent/failed/pending counts + reads
  GET  ?action=me                       -> also returns owner_sections (owner area; [] for everyone else)
  POST {action: owner_stepup}           -> OWNER: {url}: Discord re-sign-in that makes this session "fresh" for DASH_STEPUP_MINUTES
  POST {action: owner_signout_all}      -> OWNER: drop every dashboard session of this user (audited)
  GET  ?action=owner_audit[&before][&section] -> OWNER (section "audit"): owner audit trail, newest first
  GET  ?action=owner_health|owner_servers|owner_server|owner_user|owner_payments|owner_expiries -> OWNER, read-only (api/dash_owner.py)
  GET  ?action=owner_logs|owner_config  -> OWNER: masked worker snapshot (logs / config), with its age
  GET  ?action=owner_controls|owner_blacklist|owner_premium|owner_feedback|owner_botaudit|owner_payment|owner_coupons -> OWNER, read-only
  GET  ?action=owner_ads|owner_ad|owner_market|owner_bump -> OWNER (ads / bump): ads queue, one ad, marketplace listings, bump network (api/dash_owner_growth.py)
  GET  ?action=owner_watchlist|owner_reports|owner_status|owner_honeypot|owner_scamshield -> OWNER: safety pages (api/dash_owner_safety.py)
  GET  ?action=owner_search|owner_alerts|owner_growth -> OWNER (inspect / health / servers): global search, alerts (notifications), server growth series (api/dash_owner_insights.py)
  POST {action: owner_ad_approve|owner_ad_reject|owner_ad_deactivate|owner_ad_reactivate|owner_listing_remove|owner_bump_cooldown, ...} -> OWNER (ads / bump)
  POST {action: owner_report_resolve|owner_status_add|owner_status_remove|owner_presence_set|owner_status_reset|owner_scam_toggle|owner_scam_add|owner_scam_remove, ...} -> OWNER (safety)
  GET  ?action=owner_helpers|owner_clones|owner_database -> OWNER (access / servers / database): helpers, clones (never the token), table counts (api/dash_owner_ops.py)
  GET  ?action=member_plans -> any signed-in user: the plan list with server prices and the user's own state
  GET  ?action=member_card -> own saved custom level-up card design, editor options, plan access, weekly AI chat meter (B4)
  POST ?action=member_card_preview {design} -> data-URL PNG on a placeholder avatar; open to every signed-in user, stores nothing
  POST ?action=member_card_save {design} -> saves for the SESSION user; 402 + plans_path without an effective card_plan
  POST ?action=member_card_asset {kind: background|logo, data: base64} -> card plan only; validated, re-encoded, AI-moderated (approved/pending/rejected)
  POST ?action=member_card_asset_delete {kind} -> removes the member's own upload
  POST ?action=member_chat {messages} -> card plan only (402 otherwise): one free website AI chat; 10 a week (source card_plan), refunded on failure
  GET  ?action=member_usage -> weekly AI chat meters for the plans the member has (read-only)
  POST ?action=member_stepup -> {url}: Discord re-sign-in that marks THIS session fresh (any member)
  POST ?action=member_data_delete {confirm:"DELETE MY DATA"} -> step-up required; deletes the dashboard's own data for the session user
  POST ?action=checkout_user {product} -> any signed-in user: the gateway (Paystack or Gumroad) checkout URL for the SESSION user, no /pay hop (webhook grants, never this call)
  GET  ?action=dev_status -> ANY signed-in user (#/dev): {unlocked, expires_at, export_available, plans, features}; drives the locked screen only
  GET  ?action=dev_overview (and every later dev_* route) -> 402 {code: subscription_required} without an active Developer entitlement (api/dash_dev.py, require_dev)
  GET  ?action=dev_usage -> Developer plan only: {used, limit, remaining, resets_at, models} for the weekly bot-AI chats
  GET  ?action=dev_keys -> Developer plan only: {connections:[{provider,label,last4,added_at,updated_at}], providers}; a key is NEVER returned
  POST {action: dev_key_save, provider, key} / {action: dev_key_remove, provider} -> Developer plan + fresh Discord sign-in (403 stepup_required); the key is validated with one free provider call, encrypted, never echoed
  GET  ?action=dev_export_list / dev_export_download&id -> active Developer plan OR within 7 days after it ends (ent.export_allowed, not require_dev): receipts / the decrypted file for the session user's own export only
  POST {action: dev_export_create, kind: note|text|json, name?, content} -> same gate: encrypts, uploads ciphertext to the private storage channel (opaque name), stores a receipt, DMs the plain file best-effort; 429 daily_limit; 503 when the owner's `dev_export` switch is on
  POST {action: dev_export_delete, id} -> same gate: deletes the storage message and the receipt
  GET  ?action=dev_github -> Developer plan only: {configured, connected, login, connected_at, access}; the token is NEVER returned
  GET  ?action=dev_github_repos|dev_github_browse&repo&path&ref|dev_github_diff&repo&kind=commit|compare|pull&a&b -> Developer plan + connected GitHub: READ-ONLY, size-capped (api/dash_dev.py, modules/dev_github.py)
  POST {action: dev_github_connect} / {action: dev_github_disconnect} -> Developer plan + fresh Discord sign-in; connect returns the GitHub authorize URL (state + PKCE, no write scope), disconnect deletes the token and asks GitHub to revoke it
  POST {action: dev_github_finish, code, state} -> Developer plan: the browser hands back what GitHub sent; the state must have been issued to this session's user; the token is encrypted and never echoed
  POST {action: dev_stepup} -> Developer plan only: {url}: Discord re-sign-in that makes this session fresh for DASH_STEPUP_MINUTES (returns to #/dev)
  POST {action: dev_chat, model?: default|anthropic|groq|openai, messages:[{role,content}]} -> Developer plan only: one bot-AI reply; spends 1 of 50 weekly chats (refunded if the model fails); 429 {code: weekly_limit}; 503 when the owner's `ai` switch is on. Nothing is stored.
  GET  ?action=member_status -> ANY signed-in user (#/me): their own entitlements only (api/dash_member.py); no route takes a user id
  GET  ?action=member_rank -> ANY signed-in user: their OWN global rank/XP/level and best/worst server rank; no other user is named
  GET  ?action=member_servers|member_prefs|member_purchases -> ANY signed-in user: own servers (level/XP/rank/coins), preferences, payments (no gateway refs)
  POST {action: member_pref_set, kind: language|currency|character|voice|level_ping, value[, guild_id]} -> own preference only (allowlisted)
  POST {action: owner_helper_set|owner_helper_remove|owner_clone_register|owner_clone_relink|owner_clone_stop|owner_db_cleanup_stale, ...} -> OWNER (Phase 5): step-up + typed confirm on all but helper_set
  POST {action: owner_switch|owner_blacklist_add|owner_blacklist_remove|owner_premium_revoke|owner_premium_grant|owner_payment_reverse|owner_coupon_create|owner_coupon_toggle|owner_failure_dismiss|owner_pending_clear|owner_announce|owner_announce_delete, ...}
                                        -> OWNER writes (api/dash_owner.WRITES): section + rate limit, step-up and typed confirm where destructive, fail-closed audit
Clone bots: every guild route also accepts `clone_id` (query or JSON body). The dashboard then
acts as that clone: its own bot token for every Discord call, its own settings rows (clone_id),
its own Premium state. Authorisation is unchanged and still checked against Discord with the
clone's token. Billing/checkout stay main-bot only (clone owners run their own monetisation).
  GET  ?action=me   -> each server also lists `clones` (active clone bots present in it)
"""

import asyncio
import contextvars
import json
import logging
import secrets as _secrets
import time
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, urlencode, quote

import aiohttp

import config
from database import db
from utils import dash_schema as S

logger = logging.getLogger(__name__)

DISCORD_API = "https://discord.com/api/v10"
AUTHORIZE_URL = "https://discord.com/api/oauth2/authorize"
TOKEN_URL = "https://discord.com/api/oauth2/token"
MAX_BODY = 64 * 1024
MAX_UPLOAD_BODY = 3 * 1024 * 1024      # only member_card_asset, and only with an Authorization header
CLONE_ID = None            # main bot. Per-request clone context lives in _BOT below.
MAX_CLONE_SCAN = 60

# (clone_id, bot_token) for the current request; (None, None) means the main bot.
_BOT = contextvars.ContextVar("dash_bot", default=(None, None))


def _cid():
    return _BOT.get()[0]


def _token():
    return _BOT.get()[1] or config.DISCORD_BOT_TOKEN

_initialized = False
_cache: dict = {}


def _cached(key, ttl):
    key = (_cid(), key)
    hit = _cache.get(key)
    if hit and hit[0] > time.monotonic():
        return hit[1]
    return None


def _store(key, ttl, value):
    key = (_cid(), key)
    _cache[key] = (time.monotonic() + ttl, value)
    if len(_cache) > 2000:
        now = time.monotonic()
        for k in [k for k, v in _cache.items() if v[0] < now]:
            _cache.pop(k, None)
    return value


class DiscordError(Exception):
    def __init__(self, status):
        super().__init__(f"discord {status}")
        self.status = status


async def _bot_get(path: str):
    headers = {"Authorization": f"Bot {_token()}"}
    timeout = aiohttp.ClientTimeout(total=10)
    async with aiohttp.ClientSession(timeout=timeout) as s:
        for attempt in range(2):
            async with s.get(f"{DISCORD_API}{path}", headers=headers) as r:
                if r.status == 429 and attempt == 0:
                    retry = float((await r.json(content_type=None)).get("retry_after", 1))
                    await asyncio.sleep(min(retry, 3))
                    continue
                if r.status in (500, 502, 503, 504) and attempt == 0:
                    await asyncio.sleep(0.6)  # transient Discord hiccup: one quiet retry
                    continue
                if r.status != 200:
                    raise DiscordError(r.status)
                return await r.json(content_type=None)


async def _bot_guild_ids() -> set:
    hit = _cached("botguilds", 60)
    if hit is not None:
        return hit
    ids, after = set(), None
    for _ in range(25):
        q = "/users/@me/guilds?limit=200" + (f"&after={after}" if after else "")
        page = await _bot_get(q)
        ids.update(str(g["id"]) for g in page)
        if len(page) < 200:
            break
        after = page[-1]["id"]
    return _store("botguilds", 60, ids)


async def _guild_info(guild_id: int) -> dict:
    key = ("guild", guild_id)
    hit = _cached(key, 45)
    if hit is not None:
        return hit
    return _store(key, 45, await _bot_get(f"/guilds/{guild_id}?with_counts=true"))


async def _user_can_manage(guild_id: int, user_id: int) -> bool:
    key = ("perm", guild_id, user_id)
    hit = _cached(key, 60)
    if hit is not None:
        return hit
    try:
        info = await _guild_info(guild_id)
        member = await _bot_get(f"/guilds/{guild_id}/members/{user_id}")
    except DiscordError:
        return _store(key, 15, False)
    roles = {int(r["id"]): int(r.get("permissions") or 0) for r in info.get("roles", [])}
    ok = S.can_manage(int(info["owner_id"]), user_id, [int(x) for x in member.get("roles", [])], roles, guild_id)
    return _store(key, 60, ok)


async def _meta(guild_id: int) -> dict:
    key = ("meta", guild_id)
    hit = _cached(key, 30)
    if hit is not None:
        return hit
    info = await _guild_info(guild_id)
    chans = await _bot_get(f"/guilds/{guild_id}/channels")
    kind_of = {0: "text", 5: "text", 4: "category", 2: "voice", 13: "voice"}
    channels = {"text": [], "category": [], "voice": []}
    for c in sorted(chans, key=lambda c: (c.get("position", 0), int(c["id"]))):
        k = kind_of.get(c.get("type"))
        if k:
            channels[k].append({"id": str(c["id"]), "name": c.get("name", "")})
    roles = [{"id": str(r["id"]), "name": r.get("name", ""), "color": r.get("color", 0)}
             for r in sorted(info.get("roles", []), key=lambda r: -r.get("position", 0))
             if str(r["id"]) != str(guild_id) and not r.get("managed")]
    return _store(key, 30, {"channels": channels, "roles": roles})


def _id_sets(meta: dict):
    chans = {k: {c["id"] for c in v} for k, v in meta["channels"].items()}
    return chans, {r["id"] for r in meta["roles"]}


async def _get_cfg(module: dict, guild_id: int) -> dict:
    return await getattr(db, module["get"])(guild_id, _cid())


async def _write_values(sess: dict, gid: int, module: dict, clean: dict, before_cfg: dict, note=None) -> dict:
    """Shared by save and reset: write, then record who changed what. An audit failure never
    blocks the change itself."""
    await getattr(db, module["set"])(gid, _cid(), **clean)
    logger.info("dashboard %s guild=%s user=%s module=%s keys=%s", note or "save", gid, sess["user"]["id"], module["id"], sorted(clean))
    after = S.export_values(module, await _get_cfg(module, gid))
    try:
        changes = S.diff_values(module, S.export_values(module, before_cfg), after)
        if changes:
            await db.dash_audit_add(gid, str(sess["user"]["id"]), sess["user"].get("username") or "Unknown",
                                    module["id"], changes, S.AUDIT_RETENTION_DAYS)
    except Exception:
        logger.exception("dashboard: audit write failed (change itself succeeded)")
    return after


# ───────────────────────── request handling ─────────────────────────

class _Reply(Exception):
    def __init__(self, status, payload=None, location=None):
        self.status, self.payload, self.location = status, payload, location


def _fail(status, message, code=None):
    raise _Reply(status, {"ok": False, "message": message, **({"code": code} if code else {})})


def _bearer(headers) -> str:
    h = headers.get("Authorization") or ""
    return h[7:].strip() if h.lower().startswith("bearer ") else ""


async def _session(headers) -> dict:
    sid = _bearer(headers)
    payload = await db.get_login_session(sid, ttl_minutes=config.DASH_SESSION_MINUTES) if sid else None
    if not payload or payload.get("kind") != "dash":
        _fail(401, "Your session expired. Sign in again.")
    return {**payload, "_sid": sid}


def _is_owner(sess: dict) -> bool:
    try:
        return int(sess["user"]["id"]) in set(config.DISCORD_OWNER_BROADCAST_IDS)
    except (KeyError, TypeError, ValueError):
        return False


# ───────────────────────── owner area ─────────────────────────
# Same permission rules as the Discord Owner panel (modules/admin_access). The client never
# decides access: every owner_* route calls _require_section. v1 is owner-only (no helpers on the
# web yet), so the helper map is empty here.

def _owner_sections(sess: dict) -> set:
    from modules.admin_access import compute_sections
    try:
        uid = int(sess["user"]["id"])
    except (KeyError, TypeError, ValueError):
        return set()
    return compute_sections(uid, config.DASH_OWNER_IDS, config.DISCORD_OWNER_BROADCAST_IDS)


def _require_member(sess: dict) -> str:
    """Any valid dashboard session. Returns the user id FROM THE SESSION; member routes never take an id from the client."""
    try:
        uid = str(int(sess["user"]["id"]))
    except (KeyError, TypeError, ValueError):
        _fail(401, "Your session expired. Sign in again.")
    return uid


def _require_section(sess: dict, section: str) -> None:
    """403 unless this user may open `section`; 401 if the owner session is older than
    DASH_OWNER_SESSION_MINUTES (owners sign in again more often than guild admins)."""
    if section not in _owner_sections(sess):
        _fail(403, "You don't have access to that.")
    iat = sess.get("iat")
    if not isinstance(iat, (int, float)) or time.time() - iat > config.DASH_OWNER_SESSION_MINUTES * 60:
        _fail(401, "Owner sessions are short. Sign in again.")


def _require_fresh(sess: dict) -> None:
    """Step-up: the session must have re-authenticated with Discord within DASH_STEPUP_MINUTES."""
    fu = sess.get("fresh_until")
    if not isinstance(fu, (int, float)) or fu < time.time():
        raise _Reply(403, {"ok": False, "code": "stepup_required",
                           "message": "Confirm it's you: sign in with Discord again."})


def _require_confirm(body: dict, expected: str) -> None:
    """Typed confirmation for destructive actions (exact match, case-sensitive)."""
    if (body.get("confirm") or "") != expected:
        _fail(422, f"Type {expected} to confirm.")


def _merged(attr: str) -> dict:
    """ROUTES / WRITES of every owner module. A duplicate action name is a bug, so fail loudly (tested)."""
    from api import dash_owner, dash_owner_growth, dash_owner_insights, dash_owner_ops, dash_owner_safety
    out: dict = {}
    for mod in (dash_owner, dash_owner_growth, dash_owner_safety, dash_owner_ops, dash_owner_insights):
        part = getattr(mod, attr)
        dup = out.keys() & part.keys()
        if dup:
            raise RuntimeError(f"duplicate owner {attr} action(s): {sorted(dup)}")
        out.update(part)
    return out


MEMBER_FRESH_WRITES = frozenset({"member_data_delete"})


def _member_routes() -> dict:
    from api import dash_dev, dash_member, dash_msg
    return {**dash_member.ROUTES, **dash_dev.ROUTES, **dash_msg.ROUTES}


def _member_writes() -> dict:
    from api import dash_dev, dash_member, dash_msg
    return {**dash_member.WRITES, **dash_dev.WRITES, **dash_msg.WRITES}


def _owner_routes() -> dict:
    return _merged("ROUTES")


def _owner_writes() -> dict:
    return _merged("WRITES")


_owner_hits: dict = {}


def _owner_rate(sess: dict, action: str, limit: int, window: float) -> None:
    key = (str(sess["user"]["id"]), action)
    now = time.monotonic()
    hits = [t for t in _owner_hits.get(key, []) if now - t < window]
    if len(hits) >= limit:
        _fail(429, "Slow down a little.")
    hits.append(now)
    _owner_hits[key] = hits
    if len(_owner_hits) > 2000:
        _owner_hits.clear()


async def _owner_write(sess: dict, section: str, action: str, target: str, fn, *, detail=None):
    """Run an owner write with a FAIL-CLOSED audit: the audit row is written first and, if that
    fails, the action does not run (the opposite of the guild dashboard). The row's result is
    updated afterwards (best effort). Never put secrets in `target` or `detail`."""
    try:
        audit_id = await db.owner_audit_add(sess["user"]["id"], sess["user"].get("username") or "",
                                            section, action, target, "web", "started", detail)
    except Exception:
        logger.exception("dashboard: owner audit write failed; refusing the action")
        _fail(503, "Couldn't record this in the audit log, so it was not done.")
    try:
        result = await fn()
    except _Reply as r:
        await _owner_audit_result(audit_id, "denied" if r.status < 500 else "error")
        raise
    except Exception:
        await _owner_audit_result(audit_id, "error")
        raise
    await _owner_audit_result(audit_id, "ok")
    return result


async def _owner_audit_result(audit_id, result: str) -> None:
    try:
        await db.owner_audit_set_result(audit_id, result)
    except Exception:
        logger.exception("dashboard: owner audit result update failed")


_last_preview: dict = {}
PREVIEW_MIN_INTERVAL = 1.5


def _eff_premium(module: dict, premium: bool, cfg: dict) -> bool:
    """Welcome card themes are also sold as a one-time card pack, so a guild that bought
    the pack (card_pack_unlocked) may use them without a Premium subscription."""
    return bool(premium or (module["id"] == "welcome" and cfg.get("card_pack_unlocked")))


async def _avatar_bytes(url: str) -> bytes:
    """The viewer's own Discord avatar (CDN only), or a generated placeholder."""
    from io import BytesIO
    from PIL import Image, ImageDraw
    if isinstance(url, str) and url.startswith("https://cdn.discordapp.com/"):
        try:
            timeout = aiohttp.ClientTimeout(total=5)
            async with aiohttp.ClientSession(timeout=timeout) as s:
                async with s.get(url.replace("size=64", "size=256")) as r:
                    if r.status == 200:
                        data = await r.read()
                        if len(data) < 2_000_000:
                            Image.open(BytesIO(data)).verify()
                            return data
        except Exception:
            pass
    img = Image.new("RGB", (256, 256), (88, 101, 242))
    ImageDraw.Draw(img).ellipse((78, 56, 178, 156), fill=(255, 255, 255))
    ImageDraw.Draw(img).ellipse((48, 170, 208, 330), fill=(255, 255, 255))
    out = BytesIO(); img.save(out, format="PNG")
    return out.getvalue()


def _render_card_sync(avatar: bytes, name: str, sub: str, guild_name: str, opts: dict) -> bytes:
    from modules import welcome_card as wc
    data, _fmt = wc.render_welcome_card(
        avatar, name, sub, background_color=opts["bg"], accent_color=opts["accent"], animate=False,
        avatar_shape=opts["shape"], guild_name=guild_name, use_template=opts["use_template"], theme=opts["theme"])
    return data


async def _welcome_preview(sess: dict, gid: int, q) -> dict:
    import base64
    from modules import welcome_card as wc
    uid = str(sess["user"]["id"])
    now = time.monotonic()
    if now - _last_preview.get(uid, 0) < PREVIEW_MIN_INTERVAL:
        _fail(429, "Slow down a little.")
    _last_preview[uid] = now
    if len(_last_preview) > 5000:
        _last_preview.clear()
    theme, shape = q("theme") or "wolf", q("shape") or "circle"
    if theme not in wc.THEME_BACKGROUNDS:
        _fail(422, "Unknown theme.")
    if shape not in wc.AVATAR_SHAPES:
        _fail(422, "Unknown avatar shape.")
    bg, accent = q("bg") or "#2b2d31", q("accent") or "#5865F2"
    if not (S._HEX.match(bg) and S._HEX.match(accent)):
        _fail(422, "Colours must look like #RRGGBB.")
    use_template = (q("use_template") or "1") != "0"
    welcome = S.BY_ID["welcome"]
    cfg = await _get_cfg(welcome, gid)
    premium = bool(await db.is_guild_premium_active(gid, _cid()))
    if use_template and theme in wc.PREMIUM_THEMES and not _eff_premium(welcome, premium, cfg):
        _fail(403, "That theme needs Premium or the card pack.")
    info = await _guild_info(gid)
    avatar = await _avatar_bytes(sess["user"].get("avatar_url"))
    opts = {"bg": bg, "accent": accent, "shape": shape, "theme": theme, "use_template": use_template}
    count = info.get("approximate_member_count") or 1
    png = await asyncio.get_running_loop().run_in_executor(
        None, _render_card_sync, avatar, str(sess["user"].get("username") or "Member")[:32],
        f"Member #{count}", str(info.get("name") or "Server")[:40], opts)
    return {"ok": True, "image": "data:image/png;base64," + base64.b64encode(png).decode()}


PLANS = [
    {"id": "premium", "label": "Premium: Monthly", "period": "per month", "kind": "premium"},
    {"id": "premium_yearly", "label": "Premium: Yearly", "period": "per year", "kind": "premium"},
    {"id": "premium_lifetime", "label": "Premium: Lifetime", "period": "one time", "kind": "premium"},
    {"id": "welcome_card_pack", "label": "Welcome card pack", "period": "one time", "kind": "card_pack"},
]
PLAN_BY_ID = {p["id"]: p for p in PLANS}
_last_checkout: dict = {}
CHECKOUT_MIN_INTERVAL = 3.0


def _price_usd(plan_id: str):
    """The one price the payment verifier also expects (gumroad_payments.expected_price_usd)."""
    try:
        from gumroad_payments import expected_price_usd
        price = expected_price_usd(plan_id)
    except Exception:
        logger.exception("dashboard: price lookup failed for %s", plan_id)
        return None
    return price if price and price > 0 else None


async def _pay_options() -> list:
    """Payment buttons for the bot's payment mode (split: Paystack + Gumroad, auto: Paystack, gumroad: Gumroad)."""
    from modules import user_subs
    try:
        mode = await db.get_payment_mode(None)
    except Exception:
        logger.warning("payment mode lookup failed, defaulting to split", exc_info=True)
        mode = "split"
    return user_subs.pay_options(mode)


async def _billing(gid: int) -> dict:
    if _cid() is not None:
        active = bool(await db.is_guild_premium_active(gid, _cid()))
        return {"premium": active, "expires_at": None, "card_pack": False, "plans": [], "clone": True, "pay": []}
    welcome = S.BY_ID["welcome"]
    cfg = await _get_cfg(welcome, gid)
    row = await db.get_guild_premium(gid, _cid())
    active = bool(await db.is_guild_premium_active(gid, _cid()))
    expires = row.get("expires_at") if row else None
    plans = []
    for p in PLANS:
        price = _price_usd(p["id"])
        if price is None:
            continue
        owned = (p["kind"] == "card_pack" and bool(cfg.get("card_pack_unlocked")))
        plans.append({**p, "price_usd": price, "owned": owned,
                      "included": p["kind"] == "card_pack" and active and not owned})
    return {"premium": active, "expires_at": expires.isoformat() if hasattr(expires, "isoformat") else None,
            "card_pack": bool(cfg.get("card_pack_unlocked")), "plans": plans, "pay": await _pay_options()}


async def _checkout(sess: dict, gid: int, plan_id, provider=None) -> dict:
    uid = str(sess["user"]["id"])
    now = time.monotonic()
    if now - _last_checkout.get(uid, 0) < CHECKOUT_MIN_INTERVAL:
        _fail(429, "One moment, then try again.")
    _last_checkout[uid] = now
    if len(_last_checkout) > 5000:
        _last_checkout.clear()
    if _cid() is not None:
        _fail(409, "Premium for this bot is managed by its owner, not through the main checkout.")
    plan = PLAN_BY_ID.get(str(plan_id or ""))
    if not plan:
        _fail(422, "Unknown plan.")
    bill = await _billing(gid)
    entry = next((p for p in bill["plans"] if p["id"] == plan["id"]), None)
    if entry is None:
        _fail(503, "That plan isn't available right now.")
    if entry["owned"] or entry["included"]:
        _fail(409, "This server already has that.")
    options = await _pay_options()
    allowed = [o["provider"] for o in options]
    if provider in (None, ""):
        if len(allowed) != 1:
            _fail(422, "Choose Paystack or Gumroad to pay.")
        provider = allowed[0]
    elif provider not in allowed:
        _fail(422, "That payment method isn't available right now.")
    intent = {"payment_type": plan["id"], "user_id": int(uid), "guild_id": gid, "clone_id": _cid(),
              "price_usd": entry["price_usd"], "extra": {"source": "dashboard"}}
    from payments_manual import create_checkout_for_intent
    try:
        url = await create_checkout_for_intent(intent, "GH" if provider == "paystack" else "XX")
    except Exception:
        logger.exception("dashboard checkout crashed (%s)", provider)
        url = None
    if not url or not str(url).startswith("https://"):
        _fail(502, "Couldn't start checkout right now. Please try again shortly.")
    logger.info("dashboard checkout guild=%s user=%s plan=%s provider=%s", gid, uid, plan["id"], provider)
    return {"ok": True, "url": url, "provider": provider}


_last_action: dict = {}
ACTION_MIN_INTERVAL = 8.0
PANEL_PERMS_HINT = "Give me View Channel, Send Messages, Embed Links and Attach Files there, then try again."


async def _bot_post(path: str, json_body=None, form=None):
    """POST as the bot. JSON bodies retry once on a rate limit; multipart uploads can't be
    replayed, so a 429 there is reported to the caller instead."""
    headers = {"Authorization": f"Bot {_token()}"}
    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(timeout=timeout) as s:
        for attempt in range(2):
            kw = {"data": form} if form is not None else {"json": json_body}
            async with s.post(f"{DISCORD_API}{path}", headers=headers, **kw) as r:
                if r.status == 429 and attempt == 0 and form is None:
                    retry = float((await r.json(content_type=None)).get("retry_after", 1))
                    await asyncio.sleep(min(retry, 3))
                    continue
                if r.status not in (200, 201):
                    raise DiscordError(r.status)
                return await r.json(content_type=None)


def _channel_name(meta: dict, cid) -> str:
    for c in meta["channels"]["text"]:
        if c["id"] == str(cid):
            return "#" + c["name"]
    return "the channel"


async def _post_to_channel(meta: dict, cid, **kw):
    try:
        return await _bot_post(f"/channels/{int(cid)}/messages", **kw)
    except DiscordError as e:
        name = _channel_name(meta, cid)
        if e.status == 403:
            _fail(422, f"I can't post in {name}. {PANEL_PERMS_HINT}")
        if e.status == 404:
            _fail(422, f"{name} no longer exists. Pick the channel again and save.")
        if e.status == 429:
            _fail(429, "Discord is rate limiting posts. Try again in a few seconds.")
        raise


async def _bot_action(sess: dict, gid: int, action_id) -> dict:
    uid = str(sess["user"]["id"])
    mod_id = S.BOT_ACTIONS.get(str(action_id or ""))
    if not mod_id:
        _fail(422, "Unknown action.")
    now = time.monotonic()
    key = (uid, gid, action_id, _cid())
    if now - _last_action.get(key, 0) < ACTION_MIN_INTERVAL:
        _fail(429, "Just did that. Wait a few seconds.")
    _last_action[key] = now
    if len(_last_action) > 5000:
        _last_action.clear()
    module = S.BY_ID[mod_id]
    cfg = await _get_cfg(module, gid)
    meta = await _meta(gid)
    chans, roles = _id_sets(meta)

    def need_channel(k, label):
        v = cfg.get(k)
        if not v or str(v) not in chans["text"]:
            _fail(422, f"Pick and save the {label} first.")
        return int(v)

    if action_id == "verify_panel":
        cid = need_channel("channel_id", "verify channel")
        if not cfg.get("unverified_role_id") or str(cfg["unverified_role_id"]) not in roles:
            _fail(422, "Pick and save the Unverified role first.")
        desc = "Click the button below to verify you're a real person and unlock the rest of the server."
        if cfg.get("mode") == "captcha":
            desc += "\nYou'll be asked to pass a quick security check."
        info = await _guild_info(gid)
        msg = await _post_to_channel(meta, cid, json_body={
            "embeds": [{"title": f"Welcome to {info.get('name') or 'the server'} 👋", "description": desc, "color": 0x2ECC71}],
            "components": [{"type": 1, "components": [{"type": 2, "style": 3, "label": "I'm not a bot",
                                                        "emoji": {"name": "✅"}, "custom_id": f"verify_btn:{gid}"}]}],
            "allowed_mentions": {"parse": []}})
        await db.set_verification_config(gid, _cid(), message_id=int(msg["id"]))
        summary, label = _channel_name(meta, cid), "Verify panel posted"
    elif action_id == "ticket_panel":
        cid = need_channel("panel_channel_id", "panel channel")
        msg = await _post_to_channel(meta, cid, json_body={
            "embeds": [{"title": "🎫 Support Tickets", "color": 0x5865F2,
                        "description": "Click **Open Ticket** to create a private support channel."}],
            "components": [{"type": 1, "components": [{"type": 2, "style": 1, "label": "Open Ticket",
                                                        "emoji": {"name": "🎫"}, "custom_id": "ticket:open"}]}],
            "allowed_mentions": {"parse": []}})
        await db.set_ticket_config(gid, _cid(), panel_channel_id=cid, panel_message_id=int(msg["id"]))
        summary, label = _channel_name(meta, cid), "Ticket panel posted"
    else:  # welcome_test
        from modules import welcome_card as wc
        cid = need_channel("channel_id", "welcome channel")
        premium = bool(await db.is_guild_premium_active(gid, _cid()))
        theme = cfg.get("card_theme") or "wolf"
        if theme not in wc.THEME_BACKGROUNDS or (theme in wc.PREMIUM_THEMES and not _eff_premium(module, premium, cfg)):
            theme = "wolf"
        shape = cfg.get("avatar_shape") if cfg.get("avatar_shape") in wc.AVATAR_SHAPES else "circle"
        info = await _guild_info(gid)
        avatar = await _avatar_bytes(sess["user"].get("avatar_url"))
        opts = {"bg": cfg.get("background_color") or "#2b2d31", "accent": cfg.get("accent_color") or "#5865F2",
                "shape": shape, "theme": theme, "use_template": cfg.get("use_template") is not False}
        png = await asyncio.get_running_loop().run_in_executor(
            None, _render_card_sync, avatar, str(sess["user"].get("username") or "Member")[:32],
            f"Member #{info.get('approximate_member_count') or 1}", str(info.get("name") or "Server")[:40], opts)
        form = aiohttp.FormData()
        form.add_field("payload_json", json.dumps({
            "content": "🧪 Test welcome card, sent from the dashboard. Nobody joined.",
            "attachments": [{"id": 0, "filename": "welcome.png"}], "allowed_mentions": {"parse": []}}),
            content_type="application/json")
        form.add_field("files[0]", png, filename="welcome.png", content_type="image/png")
        await _post_to_channel(meta, cid, form=form)
        summary, label = _channel_name(meta, cid), "Test welcome sent"
    try:
        await db.dash_audit_add(gid, uid, sess["user"].get("username") or "Unknown", mod_id,
                                {label: {"from": None, "to": summary}}, S.AUDIT_RETENTION_DAYS)
    except Exception:
        logger.exception("dashboard: audit write failed for bot action")
    logger.info("dashboard bot_action guild=%s user=%s action=%s", gid, uid, action_id)
    return {"ok": True, "message": f"Done: posted in {summary}."}


async def _bot_request(method: str, path: str, reason=None, json_body=None):
    """PUT/PATCH/DELETE as the bot. Returns the status code; callers decide what each means."""
    headers = {"Authorization": f"Bot {_token()}"}
    if reason:
        headers["X-Audit-Log-Reason"] = quote(reason[:400], safe="")
    timeout = aiohttp.ClientTimeout(total=10)
    async with aiohttp.ClientSession(timeout=timeout) as s:
        for attempt in range(2):
            kw = {"json": json_body} if json_body is not None else {}
            async with s.request(method, f"{DISCORD_API}{path}", headers=headers, **kw) as r:
                if r.status == 429 and attempt == 0:
                    retry = float((await r.json(content_type=None)).get("retry_after", 1))
                    await asyncio.sleep(min(retry, 3))
                    continue
                return r.status


RAID_LIST_MAX = 50
_last_raid_op: dict = {}
RAID_OP_MIN_INTERVAL = 0.8


async def _actor_perms(gid: int, uid: int):
    """(owner_id, member role ids, role permission map, role position map) for the signed-in admin."""
    info = await _guild_info(gid)
    member = await _bot_get(f"/guilds/{gid}/members/{uid}")
    roles = {int(r["id"]): int(r.get("permissions") or 0) for r in info.get("roles", [])}
    pos = {int(r["id"]): int(r.get("position") or 0) for r in info.get("roles", [])}
    return int(info["owner_id"]), [int(x) for x in member.get("roles", [])], roles, pos


async def _raid_review(gid: int) -> dict:
    rows = [r for r in await db.list_quarantined(gid, _cid(), 200) if S.is_raid_row(r)]
    rows.sort(key=lambda r: r.get("created_at") or 0)
    total = len(rows)
    rows = rows[:RAID_LIST_MAX]

    async def one(row):
        try:
            return S.raid_row_view(row, await _bot_get(f"/guilds/{gid}/members/{int(row['user_id'])}"))
        except DiscordError as e:
            if e.status == 404:
                return S.raid_row_view(row, None)
            raise
    people = await asyncio.gather(*[one(r) for r in rows])
    return {"ok": True, "people": list(people), "total": total}


async def _warn_remove(sess: dict, gid: int, raw_user, raw_warn) -> dict:
    """Remove a single warn. Same permission Discord asks for /unwarn (Timeout Members; owner/Administrator pass).
    The delete is scoped to this server AND this member, so a warn id from anywhere else matches nothing."""
    uid = int(sess["user"]["id"])
    _owner_rate(sess, "warn_remove", 20, 60)
    try:
        target = S.parse_mod_user(raw_user)
        warn_id = S.parse_warn_id(raw_warn)
    except ValueError as e:
        _fail(400, str(e))
    if target is None:
        _fail(400, "Pick a member first.")
    owner_id, my_roles, role_perms, _pos = await _actor_perms(gid, uid)
    if not S.has_permission(owner_id, uid, my_roles, role_perms, gid, S.MODERATE_MEMBERS):
        _fail(403, "You need the Timeout Members permission to remove warns (the same as /unwarn).")
    gone = await db.dash_warn_remove(gid, target, warn_id)
    if gone is None:
        _fail(404, "That warn is already gone. Refresh the list.")
    actor_name = sess["user"].get("username") or "Unknown"
    try:                                                    # same case trail Discord's /unwarn leaves
        from modules import moderation_extra as modx
        await modx.log_action(gid, "unwarn", uid, target_user_id=target, reason=f"Removed warn #{warn_id} via dashboard")
    except Exception:
        logger.exception("dashboard: moderation log failed for warn_remove")
    try:
        await db.dash_audit_add(gid, str(uid), actor_name, "moderation",
                                {f"Warn #{warn_id} removed (member {target})": {"from": S._clean_text(gone.get("reason"), 120) or "(no reason)", "to": None}},
                                S.AUDIT_RETENTION_DAYS)
    except Exception:
        logger.exception("dashboard: audit write failed for warn_remove")
    logger.info("dashboard warn_remove guild=%s user=%s target=%s warn=%s", gid, uid, target, warn_id)
    return {"ok": True, "message": "Warn removed.", "warn_count": gone["remaining"]}


async def _raid_action(sess: dict, gid: int, raw_user, op) -> dict:
    uid = int(sess["user"]["id"])
    now = time.monotonic()
    if now - _last_raid_op.get(uid, 0) < RAID_OP_MIN_INTERVAL:
        _fail(429, "Slow down a little.")
    _last_raid_op[uid] = now
    if len(_last_raid_op) > 5000:
        _last_raid_op.clear()
    try:
        target = int(str(raw_user))
    except (TypeError, ValueError):
        _fail(400, "Pick a person first.")
    op = str(op or "")
    owner_id, my_roles, role_perms, role_pos = await _actor_perms(gid, uid)
    why = S.raid_op_allowed(
        op,
        S.has_permission(owner_id, uid, my_roles, role_perms, gid, S.MANAGE_GUILD | S.BAN_MEMBERS),
        S.has_permission(owner_id, uid, my_roles, role_perms, gid, S.BAN_MEMBERS),
        S.has_permission(owner_id, uid, my_roles, role_perms, gid, S.KICK_MEMBERS))
    if why:
        _fail(403, why)
    # Only people the anti-raid itself holds can be acted on here, never an arbitrary member id.
    row = await db.get_quarantined(gid, _cid(), target)
    if row is None or not S.is_raid_row(row):
        _fail(404, "They're no longer waiting for review. Refresh the list.")
    try:
        member = await _bot_get(f"/guilds/{gid}/members/{target}")
    except DiscordError as e:
        if e.status != 404:
            raise
        member = None
    if member is not None and uid != owner_id and op != "approve":
        if S.top_position([int(x) for x in member.get("roles", [])], role_pos) >= S.top_position(my_roles, role_pos):
            _fail(403, "You can only act on people ranked below you.")
    actor_name = sess["user"].get("username") or "Unknown"
    reason = f"[anti-raid review] {op} by {actor_name} via dashboard"
    q_role = await db.get_quarantine_role(gid, _cid())

    if op == "approve":
        if member is None:
            await db.remove_quarantined(gid, _cid(), target)
            done = "They'd already left, so they were removed from the list."
        else:
            existing = [int(x) for x in member.get("roles", [])]
            valid = set(role_pos)
            keep = [r for r in existing if not (q_role and r == int(q_role))]
            restore = [int(r) for r in (row.get("saved_role_ids") or []) if int(r) in valid and int(r) not in keep]
            st = await _bot_request("PATCH", f"/guilds/{gid}/members/{target}", reason=reason,
                                    json_body={"roles": [str(r) for r in keep + restore]})
            if st == 403:
                _fail(422, "Discord refused to change their roles. Make sure my role is above theirs and above the roles being restored.")
            if st not in (200, 204):
                _fail(502, "Discord didn't accept that. Try again.")
            await db.remove_quarantined(gid, _cid(), target)
            done = f"Released. {len(restore)} role(s) restored."
    elif op == "ban":
        st = await _bot_request("PUT", f"/guilds/{gid}/bans/{target}", reason=reason,
                                json_body={"delete_message_seconds": 3600})
        if st == 403:
            _fail(422, "Discord refused that ban. Make sure my role is above theirs and I have Ban Members. They stay quarantined.")
        if st not in (200, 201, 204):
            _fail(502, "Discord didn't accept that. Try again.")
        await db.remove_quarantined(gid, _cid(), target)
        done = "Banned."
    else:  # kick
        if member is None:
            await db.remove_quarantined(gid, _cid(), target)
            done = "They'd already left, so they were removed from the list."
        else:
            st = await _bot_request("DELETE", f"/guilds/{gid}/members/{target}", reason=reason)
            if st == 403:
                _fail(422, "Discord refused that kick. Make sure my role is above theirs and I have Kick Members. They stay quarantined.")
            if st not in (200, 204):
                _fail(502, "Discord didn't accept that. Try again.")
            await db.remove_quarantined(gid, _cid(), target)
            done = "Kicked."
    try:
        await db.dash_audit_add(gid, str(uid), actor_name, "antiraid",
                                {f"Review: {op}": {"from": None, "to": str(target)}}, S.AUDIT_RETENTION_DAYS)
    except Exception:
        logger.exception("dashboard: audit write failed for raid action")
    logger.info("dashboard raid_action guild=%s user=%s op=%s target=%s", gid, uid, op, target)
    return {"ok": True, "message": done}
def _schedule_audit(sess, gid, label, summary):
    async def go():
        try:
            await db.dash_audit_add(gid, str(sess["user"]["id"]), sess["user"].get("username") or "Unknown", "scheduled",
                                    {label: {"from": None, "to": summary}}, S.AUDIT_RETENTION_DAYS)
        except Exception:
            logger.exception("dashboard: audit write failed for schedule")
    return go()


async def _live_announcements(gid: int) -> list:
    return await db.get_scheduled_announcements(gid, _cid())


async def _live_schedules(gid: int) -> list:
    rows = await db.list_scheduled_messages(gid, _cid())
    return [r for r in rows if r.get("enabled")]
async def _ticket_messages(channel_id: int) -> tuple:
    """Up to TICKET_HISTORY_MAX messages, oldest first. (messages, truncated) or (None, False)
    when the channel is gone."""
    key = ("tkmsgs", channel_id)
    hit = _cached(key, 30)
    if hit is not None:
        return hit
    out, before = [], None
    for _ in range(S.TICKET_HISTORY_MAX // 100):
        path = f"/channels/{channel_id}/messages?limit=100" + (f"&before={before}" if before else "")
        try:
            page = await _bot_get(path)
        except DiscordError as e:
            if e.status in (403, 404):
                if not out:
                    return _store(key, 15, (None, False))
                return _store(key, 30, ([S.ticket_message_view(m) for m in out[::-1]], True))
            raise
        out.extend(page)
        if len(page) < 100:
            return _store(key, 30, ([S.ticket_message_view(m) for m in out[::-1]], False))
        before = page[-1]["id"]
    return _store(key, 30, ([S.ticket_message_view(m) for m in out[::-1]], True))


async def _authorised_guild(sess: dict, raw_gid) -> int:
    try:
        gid = int(str(raw_gid))
    except (TypeError, ValueError):
        _fail(400, "Pick a server first.")
    uid = int(sess["user"]["id"])
    if str(gid) not in {g["id"] for g in sess.get("guilds", [])}:
        _fail(403, "You can't manage that server.")
    try:
        present = str(gid) in await _bot_guild_ids()
    except DiscordError:
        _fail(502, "Couldn't reach Discord. Try again in a moment.")
    if not present:
        _fail(404, "The bot isn't in that server yet.")
    if not await _user_can_manage(gid, uid):
        _fail(403, "You need Manage Server in that server.")
    return gid


def _check_oauth_configured():
    if not (config.DASH_PAGES_URL and config.DASH_OAUTH_REDIRECT_URI and config.DISCORD_OAUTH_CLIENT_SECRET):
        _fail(503, "The dashboard isn't configured yet.")


def _authorize_url(state: str, prompt: str) -> str:
    q = {"client_id": config.DISCORD_OAUTH_CLIENT_ID, "redirect_uri": config.DASH_OAUTH_REDIRECT_URI,
         "response_type": "code", "scope": "identify guilds", "state": state, "prompt": prompt}
    return f"{AUTHORIZE_URL}?{urlencode(q)}"


async def _oauth_login():
    _check_oauth_configured()
    state = _secrets.token_urlsafe(24)
    await db.create_login_oauth_state(state, return_to="dash")
    raise _Reply(302, location=_authorize_url(state, "none"))


def _back(fragment: str):
    raise _Reply(302, location=f"{config.DASH_PAGES_URL}/#{fragment}")


def _github_callback(query: dict):
    """GitHub sends the browser back to the same URL as Discord. Nothing is exchanged here: the browser carries the
    code and state to the signed-in dashboard, which calls dev_github_finish with its own session (so a link started by
    someone else can't connect your account). The state is NOT consumed here."""
    if query.get("error") or not query.get("code", [None])[0]:
        _back("gh_error=1")
    _back(urlencode({"gh_code": query["code"][0], "gh_state": query["state"][0]}))


async def _oauth_callback(query: dict):
    if (query.get("state", [""])[0] or "").startswith("gh."):
        _github_callback(query)
    if query.get("error"):
        _back("error=" + urlencode({"": "Sign-in was cancelled."})[1:])
    state, code = query.get("state", [None])[0], query.get("code", [None])[0]
    popped = await db.pop_login_oauth_state(state) if state else None
    rt = (popped or {}).get("return_to") or ""
    stepup_dev = rt.startswith("dash_stepup_dev:") or rt.startswith("dash_stepup_me:")
    stepup_me = rt.startswith("dash_stepup_me:")
    stepup_sid = (rt[rt.index(":") + 1:] if stepup_dev else rt[len("dash_stepup:"):]) if (stepup_dev or rt.startswith("dash_stepup:")) else None
    if not popped or not (rt == "dash" or stepup_sid) or not code:
        _back("error=" + urlencode({"": "That sign-in link expired. Try again."})[1:])
    try:
        timeout = aiohttp.ClientTimeout(total=12)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.post(TOKEN_URL, data={
                "client_id": config.DISCORD_OAUTH_CLIENT_ID, "client_secret": config.DISCORD_OAUTH_CLIENT_SECRET,
                "grant_type": "authorization_code", "code": code, "redirect_uri": config.DASH_OAUTH_REDIRECT_URI,
            }) as r:
                r.raise_for_status()
                token = (await r.json())["access_token"]
            h = {"Authorization": f"Bearer {token}"}
            async with s.get(f"{DISCORD_API}/users/@me", headers=h) as r:
                r.raise_for_status()
                me = await r.json()
            async with s.get(f"{DISCORD_API}/users/@me/guilds", headers=h) as r:
                r.raise_for_status()
                guilds = await r.json()
    except Exception:
        logger.exception("dashboard OAuth exchange failed")
        _back("error=" + urlencode({"": "Something went wrong signing you in."})[1:])
    uid = me["id"]
    if stepup_sid:
        # Step-up: the same Discord account must have just re-authenticated. Mark THAT session
        # fresh; never create a new one (so a different account can't take over the session).
        cur = await db.get_login_session(stepup_sid, ttl_minutes=config.DASH_SESSION_MINUTES if stepup_dev else config.DASH_OWNER_SESSION_MINUTES)
        if not cur or cur.get("kind") != "dash" or str((cur.get("user") or {}).get("id")) != str(uid):
            _back("error=" + urlencode({"": "That confirmation didn't match your session."})[1:])
        await db.update_login_session_payload(
            stepup_sid, {"fresh_until": int(time.time()) + config.DASH_STEPUP_MINUTES * 60})
        _back("me=stepup_ok" if stepup_me else ("dev=stepup_ok" if stepup_dev else "owner=stepup_ok"))
    avatar = (f"https://cdn.discordapp.com/avatars/{uid}/{me['avatar']}.png?size=64" if me.get("avatar")
              else f"https://cdn.discordapp.com/embed/avatars/{(int(uid) >> 22) % 6}.png")
    sid = await db.create_login_session({
        "kind": "dash",
        "iat": int(time.time()),
        "user": {"id": uid, "username": me.get("global_name") or me.get("username") or "Discord user", "avatar_url": avatar},
        "guilds": S.guild_list_manageable(guilds),
    })
    try:                                    # registry for member messaging; never blocks a sign-in
        await db.dash_web_user_touch(uid)
    except Exception:
        logger.exception("dashboard: web-user registry write failed")
    _back("session=" + sid)


# ───────────────────────── clone bots ─────────────────────────

_clone_rows: dict = {}


async def _clone_row(clone_id: int):
    """Active clone row (cached 60s). Contains the encrypted token; never sent to the browser."""
    hit = _clone_rows.get(clone_id)
    if hit and hit[0] > time.monotonic():
        return hit[1]
    row = await db.get_discord_clone(clone_id)
    if row and row.get("status") != "active":
        row = None
    _clone_rows[clone_id] = (time.monotonic() + 60, row)
    if len(_clone_rows) > 500:
        _clone_rows.clear()
    return row


def _clone_token(row) -> str:
    try:
        from utils.crypto import secret_manager
        return secret_manager.decrypt(row["bot_token_encrypted"]) or ""
    except Exception:
        logger.exception("dashboard: couldn't decrypt a clone token")
        return ""


def _parse_clone_id(raw):
    if raw in (None, "", "0", 0, "main"):
        return None
    try:
        n = int(str(raw))
    except (TypeError, ValueError):
        _fail(400, "Unknown bot.")
    if n <= 0:
        _fail(400, "Unknown bot.")
    return n


async def _enter_clone(raw):
    """Point every Discord call and settings read/write of this request at the chosen clone.
    Anyone may ask for any clone id; what protects a server is still _authorised_guild, which
    checks Discord (with that clone's token) that the bot is in the guild and the user manages it."""
    cid = _parse_clone_id(raw)
    if cid is None:
        _BOT.set((None, None))
        return
    row = await _clone_row(cid)
    token = _clone_token(row) if row else ""
    if not token:
        _fail(404, "That bot isn't available.")
    _BOT.set((cid, token))


async def _clone_guild_ids(row) -> set:
    """Guild ids a clone bot is in (cached 60s per clone), scoped by its own token."""
    cid = int(row["clone_id"])
    token = _clone_token(row)
    if not token:
        return set()
    _BOT.set((cid, token))
    try:
        return await _bot_guild_ids()
    except Exception:
        return set()


async def _clone_presence(user_guild_ids: set) -> dict:
    """{guild_id: [{clone_id, name}]} for active clones present in the user's servers."""
    try:
        listed = (await db.list_active_discord_clones())[:MAX_CLONE_SCAN]
    except Exception:
        logger.exception("dashboard: clone list failed")
        return {}
    sem = asyncio.Semaphore(8)

    async def one(c):
        async with sem:
            row = await _clone_row(int(c["clone_id"]))
            if not row:
                return c, set()
            return c, await _clone_guild_ids(row)

    out: dict = {}
    for c, ids in await asyncio.gather(*[one(c) for c in listed], return_exceptions=False):
        for gid in ids & user_guild_ids:
            out.setdefault(gid, []).append({"clone_id": int(c["clone_id"]), "name": c.get("bot_username") or f"Clone {c['clone_id']}"})
    return out


def _icon(g):
    return f"https://cdn.discordapp.com/icons/{g['id']}/{g['icon']}.png?size=128" if g.get("icon") else None


async def _route(method: str, query: dict, headers, body: dict):
    global _initialized
    if not _initialized:
        from init_system import initialize_system
        await initialize_system()
        _initialized = True

    q = lambda k: (query.get(k) or [None])[0]
    action = body.get("action") if method == "POST" else q("action")

    if method == "GET" and (q("code") or q("error")):
        await _oauth_callback(query)
    if method == "GET" and action == "login":
        await _oauth_login()
    if method == "GET" and action == "schema":
        raise _Reply(200, {"ok": True, **S.public_schema()})

    sess = await _session(headers)

    if method == "POST" and action == "logout":
        await db.delete_login_session(_bearer(headers))
        raise _Reply(200, {"ok": True})

    if action != "me":
        await _enter_clone(body.get("clone_id") if method == "POST" else q("clone_id"))

    if method == "GET" and action == "me":
        try:
            present = await _bot_guild_ids()
        except DiscordError:
            _fail(502, "Couldn't reach Discord. Try again in a moment.")
        clones = await _clone_presence({g["id"] for g in sess.get("guilds", [])})
        servers = [{**g, "icon_url": _icon(g), "bot_present": g["id"] in present, "clones": clones.get(g["id"], [])}
                   for g in sess.get("guilds", [])]
        servers.sort(key=lambda g: (not (g["bot_present"] or g["clones"]), g["name"].lower()))
        uid = str(sess["user"]["id"])
        try:
            unread = sum(1 for m in await db.dropbox_list(uid) if not m["read"])
        except Exception:
            logger.exception("dashboard: dropbox unread count failed")
            unread = 0
        from api import dash_msg
        raise _Reply(200, {"ok": True, "user": sess["user"], "servers": servers,
                           "is_owner": _is_owner(sess), "unread": unread, "member": True,
                           "msg_unread": await dash_msg.unread_total(uid, db),
                           "owner_sections": sorted(_owner_sections(sess))})

    if method == "GET" and action in _member_routes():
        uid = _require_member(sess)
        _owner_rate(sess, "member:" + action, *{"friends_search": (15, 60), "messages_thread": (40, 60)}.get(action, (60, 60)))
        out = await _member_routes()[action](uid, q, db)
        if out.get("_status"):
            _fail(out["_status"], out.get("message") or "Something went wrong.", out.get("code"))
        raise _Reply(200, {"ok": True, **out})

    if method == "POST" and action in _member_writes():
        uid = _require_member(sess)
        _owner_rate(sess, "member:" + action, *{"checkout_user": (10, 300), "member_card_save": (10, 60), "member_card_preview": (20, 60),
                                                      "member_card_asset": (6, 300), "member_card_asset_delete": (10, 300), "dev_chat": (8, 60), "member_chat": (8, 60),
                                                      "dev_key_save": (6, 300), "dev_key_remove": (10, 300),
                                                      "dev_export_create": (6, 300), "dev_export_delete": (10, 300),
                                                      "dev_github_connect": (5, 300), "dev_github_finish": (8, 300), "dev_github_disconnect": (10, 300),
                                                      "friend_request": (10, 300), "message_send": (15, 60), "message_report": (6, 300),
                                                      "friend_block": (10, 300),
                                                      "member_stepup": (5, 300), "member_data_delete": (3, 3600)}.get(action, (30, 60)))
        from api import dash_dev
        if action in dash_dev.FRESH_WRITES:          # gate first (402), then step-up (403); handlers never see the session
            gate = await dash_dev.require_dev(uid, db)
            if gate:
                raise _Reply(gate["_status"], {"ok": False, "message": gate["message"], "code": gate["code"]})
            _require_fresh(sess)
        if action == "member_stepup":                # any signed-in member; same OAuth round trip as the Developer one
            _check_oauth_configured()
            state = _secrets.token_urlsafe(24)
            await db.create_login_oauth_state(state, return_to="dash_stepup_me:" + sess["_sid"])
            raise _Reply(200, {"ok": True, "url": _authorize_url(state, "consent"), "minutes": config.DASH_STEPUP_MINUTES})
        if action in MEMBER_FRESH_WRITES:            # step-up first; the confirm phrase is checked by the handler
            _require_fresh(sess)
        out = await _member_writes()[action](uid, body, db)
        if out.get("_status"):
            raise _Reply(out["_status"], {"ok": False, "message": out.get("message") or "Something went wrong.",
                                          **({"code": out["code"]} if out.get("code") else {}), **(out.get("extra") or {})})
        raise _Reply(200, {"ok": True, **out})

    if method == "POST" and action == "dev_stepup":
        from api import dash_dev
        uid = _require_member(sess)
        _owner_rate(sess, "dev_stepup", 5, 300)
        gate = await dash_dev.require_dev(uid, db)
        if gate:
            _fail(gate["_status"], gate["message"], gate["code"])
        _check_oauth_configured()
        state = _secrets.token_urlsafe(24)
        await db.create_login_oauth_state(state, return_to="dash_stepup_dev:" + sess["_sid"])
        raise _Reply(200, {"ok": True, "url": _authorize_url(state, "consent"), "minutes": config.DASH_STEPUP_MINUTES})

    if method == "POST" and action == "owner_stepup":
        _require_section(sess, "controls")      # any real owner; helpers never get here
        _owner_rate(sess, "stepup", 5, 300)
        _check_oauth_configured()

        async def _start():
            state = _secrets.token_urlsafe(24)
            await db.create_login_oauth_state(state, return_to="dash_stepup:" + sess["_sid"])
            return _authorize_url(state, "consent")
        url = await _owner_write(sess, "session", "stepup_start", "", _start)
        raise _Reply(200, {"ok": True, "url": url, "minutes": config.DASH_STEPUP_MINUTES})

    if method == "POST" and action == "owner_signout_all":
        _require_section(sess, "controls")
        _owner_rate(sess, "signout_all", 5, 300)
        _require_fresh(sess)
        _require_confirm(body, "SIGN OUT")
        n = await _owner_write(sess, "session", "signout_all", "",
                               lambda: db.delete_login_sessions_for_user(str(sess["user"]["id"])))
        raise _Reply(200, {"ok": True, "signed_out": n})

    if method == "GET" and action == "owner_audit":
        _require_section(sess, "audit")
        _owner_rate(sess, "audit_read", 60, 60)
        before, section = q("before"), q("section")
        if before is not None and not str(before).isdigit():
            _fail(422, "Bad cursor.")
        rows = await db.owner_audit_list(int(before) if before else None, section if section else None, 50)
        raise _Reply(200, {"ok": True, "entries": rows})

    if method == "GET" and action in _owner_routes():
        section, handler = _owner_routes()[action]
        _require_section(sess, section)
        _owner_rate(sess, "read:" + action, 60, 60)
        from api import dash_owner
        out = handler(q, _owner_sections(sess)) if getattr(handler, "wants_sections", False) else handler(q)
        out = await out if hasattr(out, "__await__") else out
        if "_error" in out:
            _fail(*out["_error"])
        raise _Reply(200, {"ok": True, **dash_owner.jsonable(out)})

    if method == "POST" and action in _owner_writes():
        section, limit, prepare = _owner_writes()[action]
        _require_section(sess, section)
        _owner_rate(sess, "write:" + action, limit, 60)
        plan = await prepare(sess, body)
        if "_error" in plan:
            _fail(*plan["_error"])
        if plan.get("fresh"):
            _require_fresh(sess)
        if plan.get("confirm"):
            _require_confirm(body, plan["confirm"])
        from api import dash_owner
        result = await _owner_write(sess, section, action, plan["target"], plan["fn"], detail=plan.get("detail"))
        raise _Reply(200, {"ok": True, **dash_owner.jsonable(result or {})})

    if method == "GET" and action == "guild":
        gid = await _authorised_guild(sess, q("guild_id"))
        info = await _guild_info(gid)
        premium = bool(await db.is_guild_premium_active(gid, _cid()))
        status = {}
        for m in S.MODULES:
            try:
                vals = S.export_values(m, await _get_cfg(m, gid))
            except Exception:
                logger.exception("dashboard: status read failed for %s", m["id"])
                continue
            status[m["id"]] = vals.get("enabled") if "enabled" in vals else None
        raise _Reply(200, {"ok": True, "guild": {
            "id": str(gid), "name": info.get("name"), "icon_url": _icon(info),
            "members": info.get("approximate_member_count"), "online": info.get("approximate_presence_count"),
            "premium": premium, "status": status,
            "bot_name": ((await _clone_row(_cid())) or {}).get("bot_username") if _cid() else None}})

    if method == "GET" and action == "meta":
        gid = await _authorised_guild(sess, q("guild_id"))
        raise _Reply(200, {"ok": True, **await _meta(gid)})

    if method == "GET" and action == "config":
        gid = await _authorised_guild(sess, q("guild_id"))
        module = S.BY_ID.get(q("module") or "")
        if not module:
            _fail(404, "Unknown module.")
        premium = bool(await db.is_guild_premium_active(gid, _cid()))
        cfg = await _get_cfg(module, gid)
        raise _Reply(200, {"ok": True, "values": S.export_values(module, cfg), "premium": _eff_premium(module, premium, cfg)})

    if method == "POST" and action == "save":
        gid = await _authorised_guild(sess, body.get("guild_id"))
        module = S.BY_ID.get(str(body.get("module") or ""))
        if not module:
            _fail(404, "Unknown module.")
        meta = await _meta(gid)
        chans, roles = _id_sets(meta)
        premium = bool(await db.is_guild_premium_active(gid, _cid()))
        before_cfg = await _get_cfg(module, gid)
        clean, errors = S.validate_values(module, body.get("values"), chans, roles, _eff_premium(module, premium, before_cfg))
        if errors:
            raise _Reply(422, {"ok": False, "message": errors[0], "errors": errors})
        try:
            after = await _write_values(sess, gid, module, clean, before_cfg)
        except S.ValidationError as e:                       # a rule only the database layer can check (e.g. auto-post needs a channel)
            raise _Reply(422, {"ok": False, "message": str(e), "errors": [str(e)]})
        raise _Reply(200, {"ok": True, "values": after})

    if method == "POST" and action == "reset":
        gid = await _authorised_guild(sess, body.get("guild_id"))
        module = S.BY_ID.get(str(body.get("module") or ""))
        if not module:
            _fail(404, "Unknown module.")
        before_cfg = await _get_cfg(module, gid)
        defaults, left = S.default_values(module, await getattr(db, module["get"])(0, _cid()))
        if not defaults:
            _fail(422, "Nothing to reset here.")
        after = await _write_values(sess, gid, module, defaults, before_cfg, note="reset")
        raise _Reply(200, {"ok": True, "values": after, "left_alone": left})

    if method == "GET" and action == "export":
        gid = await _authorised_guild(sess, q("guild_id"))
        module = S.BY_ID.get(q("module") or "")
        if not module:
            _fail(404, "Unknown module.")
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        raise _Reply(200, {"ok": True, "file": S.export_payload(module, await _get_cfg(module, gid), stamp),
                           "filename": f"prime-bot-{module['id']}-settings.json"})

    if method == "POST" and action == "import_check":
        gid = await _authorised_guild(sess, body.get("guild_id"))
        module = S.BY_ID.get(str(body.get("module") or ""))
        if not module:
            _fail(404, "Unknown module.")
        meta = await _meta(gid)
        chans, roles = _id_sets(meta)
        premium = bool(await db.is_guild_premium_active(gid, _cid()))
        cfg = await _get_cfg(module, gid)
        values, skipped, err = S.import_values(module, body.get("data"), chans, roles, _eff_premium(module, premium, cfg))
        if err:
            _fail(422, err)
        raise _Reply(200, {"ok": True, "values": values, "skipped": skipped})
    if method == "GET" and action == "raid_review":
        gid = await _authorised_guild(sess, q("guild_id"))
        raise _Reply(200, await _raid_review(gid))

    if method == "POST" and action == "warn_remove":
        gid = await _authorised_guild(sess, body.get("guild_id"))
        raise _Reply(200, await _warn_remove(sess, gid, body.get("user_id"), body.get("warn_id")))

    if method == "POST" and action == "raid_action":
        gid = await _authorised_guild(sess, body.get("guild_id"))
        raise _Reply(200, await _raid_action(sess, gid, body.get("user_id"), body.get("op")))
    if method == "GET" and action == "schedules":
        gid = await _authorised_guild(sess, q("guild_id"))
        rows = await _live_schedules(gid)
        raise _Reply(200, {"ok": True, "schedules": [S.schedule_row_view(r) for r in rows[:S.SCHEDULE_MAX_ACTIVE + 5]],
                           "limit": S.SCHEDULE_MAX_ACTIVE})

    if method == "POST" and action == "schedule_add":
        gid = await _authorised_guild(sess, body.get("guild_id"))
        meta = await _meta(gid)
        chans, _roles = _id_sets(meta)
        from datetime import datetime, timezone
        clean, err = S.validate_schedule(body, chans["text"], datetime.now(timezone.utc))
        if err:
            _fail(422, err)
        if len(await _live_schedules(gid)) >= S.SCHEDULE_MAX_ACTIVE:
            _fail(422, f"You can have up to {S.SCHEDULE_MAX_ACTIVE} scheduled messages. Delete one first.")
        job = await db.create_scheduled_message(gid, clean["channel_id"], clean["content"], clean["run_at"],
                                                clean["interval_seconds"], int(sess["user"]["id"]), clone_id=_cid())
        await _schedule_audit(sess, gid, "Scheduled message added", f"{_channel_name(meta, clean['channel_id'])}: {clean['content'][:80]}")
        logger.info("dashboard schedule_add guild=%s user=%s id=%s", gid, sess["user"]["id"], job["id"])
        raise _Reply(200, {"ok": True, "schedule": S.schedule_row_view(job)})

    if method == "POST" and action == "schedule_delete":
        gid = await _authorised_guild(sess, body.get("guild_id"))
        try:
            sid = int(str(body.get("id")))
        except ValueError:
            _fail(400, "Invalid schedule.")
        if not await db.delete_scheduled_message(gid, sid, _cid()):
            _fail(404, "That schedule no longer exists.")
        await _schedule_audit(sess, gid, "Scheduled message removed", f"#{sid}")
        raise _Reply(200, {"ok": True})
    if method == "GET" and action == "announcements":
        gid = await _authorised_guild(sess, q("guild_id"))
        _owner_rate(sess, "announcements_read", 60, 60)
        rows = await _live_announcements(gid)
        raise _Reply(200, {"ok": True, "schedules": [S.announcement_row_view(r) for r in rows[:S.ANNOUNCEMENT_MAX_ACTIVE + 5]],
                           "limit": S.ANNOUNCEMENT_MAX_ACTIVE})

    if method == "POST" and action == "announcement_add":
        gid = await _authorised_guild(sess, body.get("guild_id"))
        _owner_rate(sess, "announcement_add", 30, 60)
        meta = await _meta(gid)
        chans, _roles = _id_sets(meta)
        from datetime import datetime, timezone
        clean, err = S.validate_announcement(body, chans["text"], datetime.now(timezone.utc))
        if err:
            _fail(422, err)
        if len(await _live_announcements(gid)) >= S.ANNOUNCEMENT_MAX_ACTIVE:
            _fail(422, f"You can have up to {S.ANNOUNCEMENT_MAX_ACTIVE} announcements. Delete one first.")
        new_id = await db.add_scheduled_announcement(gid, clean["channel_id"], clean["message"], clean["run_at"],
                                                     int(sess["user"]["id"]), interval_minutes=clean["interval_minutes"], clone_id=_cid())
        await _schedule_audit(sess, gid, "Announcement added", f"{_channel_name(meta, clean['channel_id'])}: {clean['message'][:80]}")
        logger.info("dashboard announcement_add guild=%s user=%s id=%s", gid, sess["user"]["id"], new_id)
        raise _Reply(200, {"ok": True, "schedule": S.announcement_row_view({
            "id": new_id, "channel_id": clean["channel_id"], "message": clean["message"], "next_run_at": clean["run_at"],
            "interval_minutes": clean["interval_minutes"], "active": True, "created_by": sess["user"]["id"]})})

    if method == "POST" and action == "announcement_delete":
        gid = await _authorised_guild(sess, body.get("guild_id"))
        _owner_rate(sess, "announcement_delete", 30, 60)
        try:
            aid = int(str(body.get("id")))
        except ValueError:
            _fail(400, "Invalid announcement.")
        if not await db.remove_scheduled_announcement(gid, aid, _cid()):
            _fail(404, "That announcement no longer exists.")
        await _schedule_audit(sess, gid, "Announcement removed", f"#{aid}")
        raise _Reply(200, {"ok": True})

    if method == "GET" and action == "tickets":
        gid = await _authorised_guild(sess, q("guild_id"))
        status = q("status")
        if status and status not in S.TICKET_STATUSES:
            _fail(400, "Unknown status.")
        try:
            before = int(q("before")) if q("before") else None
        except ValueError:
            _fail(400, "Invalid page.")
        rows = await db.list_tickets(gid, _cid(), status or None, before, 31)
        raise _Reply(200, {"ok": True, "tickets": [S.ticket_row_view(r) for r in rows[:30]], "more": len(rows) > 30})

    if method == "GET" and action == "ticket_messages":
        gid = await _authorised_guild(sess, q("guild_id"))
        try:
            tid = int(str(q("id")))
        except ValueError:
            _fail(400, "Invalid ticket.")
        row = await db.get_ticket_by_id(gid, _cid(), tid)     # the channel comes from OUR row, never from the client
        if row is None:
            _fail(404, "That ticket doesn't exist.")
        msgs, truncated = await _ticket_messages(int(row["channel_id"]))
        raise _Reply(200, {"ok": True, "ticket": S.ticket_row_view(row), "messages": msgs, "truncated": truncated,
                           "gone": msgs is None})

    if method == "POST" and action == "bot_action":
        gid = await _authorised_guild(sess, body.get("guild_id"))
        raise _Reply(200, await _bot_action(sess, gid, body.get("id")))

    if method == "GET" and action == "billing":
        gid = await _authorised_guild(sess, q("guild_id"))
        raise _Reply(200, {"ok": True, **await _billing(gid)})

    if method == "POST" and action == "checkout":
        gid = await _authorised_guild(sess, body.get("guild_id"))
        raise _Reply(200, await _checkout(sess, gid, body.get("plan"), body.get("provider")))

    if method == "GET" and action == "audit":
        gid = await _authorised_guild(sess, q("guild_id"))
        mod = q("module")
        if mod and mod not in S.BY_ID:
            _fail(404, "Unknown module.")
        try:
            before = int(q("before")) if q("before") else None
        except ValueError:
            _fail(400, "Invalid page.")
        rows = await db.dash_audit_list(gid, before, mod, 31)
        raise _Reply(200, {"ok": True, "entries": rows[:30], "more": len(rows) > 30})

    if method == "GET" and action == "moderation":
        gid = await _authorised_guild(sess, q("guild_id"))
        try:
            user = S.parse_mod_user(q("user_id"))
            before = int(q("before")) if q("before") else None
        except ValueError as e:
            _fail(400, str(e) if "user ID" in str(e) else "Invalid page.")
        if before is not None and not 0 < before < 2 ** 31:
            _fail(400, "Invalid page.")
        kind = q("kind") or None
        if kind and not S.MOD_KIND_RE.match(kind):
            _fail(400, "Unknown action type.")
        rows = await db.dash_mod_cases(gid, user, kind, before, 31)
        out = {"ok": True, "cases": [S.mod_case_view(r) for r in rows[:30]], "more": len(rows) > 30}
        if before is None:
            out["kinds"] = [k for k in await db.dash_mod_kinds(gid) if S.MOD_KIND_RE.match(str(k))]
        if user is not None and before is None:
            warns, total = await db.dash_mod_warns(gid, user)
            out["warns"], out["warn_count"] = [S.mod_warn_view(w) for w in warns], total
        raise _Reply(200, out)

    if method == "GET" and action == "analytics":
        gid = await _authorised_guild(sess, q("guild_id"))
        try:
            days = S.parse_analytics_days(q("days"))
        except ValueError as e:
            _fail(400, str(e))
        _owner_rate(sess, "analytics", 30, 60)
        ck = ("analytics", gid, days)
        out = _cached(ck, 60)
        if out is None:
            try:
                info = await _guild_info(gid)
            except DiscordError:
                info = {}
            data = await db.dash_analytics(gid, _cid(), days)
            out = _store(ck, 60, S.analytics_view(data, info.get("approximate_member_count")))
        raise _Reply(200, {"ok": True, **out})
    if method == "GET" and action == "giveaways":
        gid = await _authorised_guild(sess, q("guild_id"))
        _owner_rate(sess, "giveaways_read", 60, 60)
        status = q("status") or None
        if status and status not in S.GIVEAWAY_STATUSES:
            _fail(400, "Unknown status.")
        try:
            before = int(q("before")) if q("before") else None
        except ValueError:
            _fail(400, "Invalid page.")
        if before is not None and not 0 < before < 2 ** 31:
            _fail(400, "Invalid page.")
        rows = await db.dash_giveaways(gid, _cid(), status, before, 31)
        raise _Reply(200, {"ok": True, "giveaways": [S.giveaway_view(r) for r in rows[:30]], "more": len(rows) > 30})

    if method == "GET" and action == "welcome_preview":
        gid = await _authorised_guild(sess, q("guild_id"))
        raise _Reply(200, await _welcome_preview(sess, gid, q))

    if method == "GET" and action == "dropbox":
        try:
            msgs = await db.dropbox_list(str(sess["user"]["id"]))
        except Exception:
            # The inbox is polled on every page; never let it take the panel down.
            logger.exception("dashboard: dropbox list failed")
            msgs = []
        raise _Reply(200, {"ok": True, "messages": msgs, "unread": sum(1 for m in msgs if not m["read"]),
                           "is_owner": _is_owner(sess)})

    if method == "GET" and action == "dropbox_delivery":
        if not _is_owner(sess):
            _fail(403, "Only the bot owner can do that.")
        try:
            mid = int(str(q("id")))
        except ValueError:
            _fail(400, "Invalid message.")
        raise _Reply(200, {"ok": True, **await db.dropbox_delivery(mid)})

    if method == "POST" and action == "dropbox_read":
        raw = body.get("id")
        try:
            mid = None if raw in (None, "") else int(str(raw))
        except ValueError:
            _fail(400, "Invalid message.")
        await db.dropbox_mark_read(str(sess["user"]["id"]), mid)
        raise _Reply(200, {"ok": True})

    if method == "POST" and action in ("dropbox_send", "dropbox_delete"):
        if not _is_owner(sess):
            _fail(403, "Only the bot owner can do that.")
        if action == "dropbox_delete":
            try:
                mid = int(str(body.get("id")))
            except ValueError:
                _fail(400, "Invalid message.")
            if not await db.dropbox_delete(mid):
                _fail(404, "That message no longer exists.")
            logger.info("dashboard dropbox delete id=%s by=%s", mid, sess["user"]["id"])
            raise _Reply(200, {"ok": True})
        clean, err = S.validate_dropbox(body)
        if err:
            _fail(422, err)
        # Targeted messages (and pushed ones) go to guild owners of the main bot.
        recipients = []
        if clean["audience"] != "all" or clean["push_dm"]:
            recipients = await db.dropbox_resolve_recipients(
                clean["audience"], clean["min_members"], clean["target_guild_id"])
            if not recipients:
                _fail(422, "No servers match that audience, so nobody would receive it.")
        mid = await db.dropbox_create(clean["title"], clean["body"], clean["kind"], clean["announce"],
                                      str(sess["user"]["id"]), clean["expires_hours"],
                                      clean["audience"], clean["min_members"], clean["target_guild_id"],
                                      clean["push_dm"])
        queued = await db.dropbox_enqueue(mid, recipients, clean["push_dm"]) if recipients else 0
        logger.info("dashboard dropbox send id=%s by=%s announce=%s audience=%s push_dm=%s recipients=%s",
                    mid, sess["user"]["id"], clean["announce"], clean["audience"], clean["push_dm"], queued)
        raise _Reply(200, {"ok": True, "id": str(mid), "recipients": queued})

    _fail(404, "Unknown action.")


class handler(BaseHTTPRequestHandler):

    def _cors(self):
        origin = config.DASH_PAGES_URL
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.send_header("Vary", "Origin")

    def _finish(self, status, payload=None, location=None):
        self.send_response(status)
        if location:
            self.send_header("Location", location)
            self.send_header("Cache-Control", "no-store")
            self._cors()
            self.end_headers()
            return
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self._cors()
        self.end_headers()
        self.wfile.write(json.dumps(payload if payload is not None else {}).encode())

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.send_header("Access-Control-Max-Age", "600")
        self.end_headers()

    def _dispatch(self, method):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        body = {}
        if method == "POST":
            try:
                n = int(self.headers.get("Content-Length", 0))
                big = query.get("action", [""])[0] == "member_card_asset" and bool(self.headers.get("Authorization"))
                if n > (MAX_UPLOAD_BODY if big else MAX_BODY):
                    raise ValueError
                body = json.loads(self.rfile.read(n) or b"{}")
                if not isinstance(body, dict):
                    raise ValueError
            except Exception:
                return self._finish(400, {"ok": False, "message": "Invalid request."})
        try:
            asyncio.run(_route(method, query, self.headers, body))
        except _Reply as r:
            return self._finish(r.status, r.payload, r.location)
        except DiscordError as e:
            logger.warning("dashboard: discord error %s", e.status)
            return self._finish(502, {"ok": False, "message": "Discord didn't answer. Try again in a moment."})
        except Exception:
            logger.exception("dashboard request failed")
            return self._finish(500, {"ok": False, "message": "Something went wrong. Try again."})
        self._finish(500, {"ok": False, "message": "Something went wrong."})

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")
