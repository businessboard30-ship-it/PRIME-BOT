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
  POST {action: logout}
  POST {action: bot_action, guild_id, id} -> bot posts a panel/test card (whitelist: S.BOT_ACTIONS)
  GET  ?action=billing&guild_id         -> Premium status + plans with server-side prices
  POST {action: checkout, guild_id, plan} -> {url}: existing /pay redirect bound to this server
  GET  ?action=audit&guild_id[&before][&module] -> change history for a server (newest first)
  GET  ?action=welcome_preview&guild_id&theme&shape&use_template&bg&accent -> rendered welcome card (data URL)
  GET  ?action=dropbox                  -> drop box messages + unread count (any signed-in admin)
  POST {action: dropbox_read, id?}      -> mark one (or all) read
  POST {action: dropbox_send, ...}      -> OWNER ONLY (config.DISCORD_OWNER_BROADCAST_IDS)
  POST {action: dropbox_delete, id}     -> OWNER ONLY
  GET  ?action=dropbox_delivery&id      -> OWNER ONLY: DM sent/failed/pending counts + reads
Scope: the main PRIME BOT (clone_id None). Clone bots are not covered yet.
"""

import asyncio
import json
import logging
import secrets as _secrets
import time
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, urlencode

import aiohttp

import config
from database import db
from utils import dash_schema as S

logger = logging.getLogger(__name__)

DISCORD_API = "https://discord.com/api/v10"
AUTHORIZE_URL = "https://discord.com/api/oauth2/authorize"
TOKEN_URL = "https://discord.com/api/oauth2/token"
MAX_BODY = 64 * 1024
CLONE_ID = None

_initialized = False
_cache: dict = {}


def _cached(key, ttl):
    hit = _cache.get(key)
    if hit and hit[0] > time.monotonic():
        return hit[1]
    return None


def _store(key, ttl, value):
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
    headers = {"Authorization": f"Bot {config.DISCORD_BOT_TOKEN}"}
    timeout = aiohttp.ClientTimeout(total=10)
    async with aiohttp.ClientSession(timeout=timeout) as s:
        for attempt in range(2):
            async with s.get(f"{DISCORD_API}{path}", headers=headers) as r:
                if r.status == 429 and attempt == 0:
                    retry = float((await r.json(content_type=None)).get("retry_after", 1))
                    await asyncio.sleep(min(retry, 3))
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
    return await getattr(db, module["get"])(guild_id, CLONE_ID)


# ───────────────────────── request handling ─────────────────────────

class _Reply(Exception):
    def __init__(self, status, payload=None, location=None):
        self.status, self.payload, self.location = status, payload, location


def _fail(status, message):
    raise _Reply(status, {"ok": False, "message": message})


def _bearer(headers) -> str:
    h = headers.get("Authorization") or ""
    return h[7:].strip() if h.lower().startswith("bearer ") else ""


async def _session(headers) -> dict:
    sid = _bearer(headers)
    payload = await db.get_login_session(sid, ttl_minutes=config.DASH_SESSION_MINUTES) if sid else None
    if not payload or payload.get("kind") != "dash":
        _fail(401, "Your session expired. Sign in again.")
    return payload


def _is_owner(sess: dict) -> bool:
    try:
        return int(sess["user"]["id"]) in set(config.DISCORD_OWNER_BROADCAST_IDS)
    except (KeyError, TypeError, ValueError):
        return False


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
    premium = bool(await db.is_guild_premium_active(gid, CLONE_ID))
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


def _pay_base() -> str:
    u = urlparse(config.DASH_OAUTH_REDIRECT_URI or "")
    return f"{u.scheme}://{u.netloc}" if u.scheme and u.netloc else ""


async def _billing(gid: int) -> dict:
    welcome = S.BY_ID["welcome"]
    cfg = await _get_cfg(welcome, gid)
    row = await db.get_guild_premium(gid, CLONE_ID)
    active = bool(await db.is_guild_premium_active(gid, CLONE_ID))
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
            "card_pack": bool(cfg.get("card_pack_unlocked")), "plans": plans}


async def _checkout(sess: dict, gid: int, plan_id) -> dict:
    uid = str(sess["user"]["id"])
    now = time.monotonic()
    if now - _last_checkout.get(uid, 0) < CHECKOUT_MIN_INTERVAL:
        _fail(429, "One moment, then try again.")
    _last_checkout[uid] = now
    if len(_last_checkout) > 5000:
        _last_checkout.clear()
    plan = PLAN_BY_ID.get(str(plan_id or ""))
    if not plan:
        _fail(422, "Unknown plan.")
    bill = await _billing(gid)
    entry = next((p for p in bill["plans"] if p["id"] == plan["id"]), None)
    if entry is None:
        _fail(503, "That plan isn't available right now.")
    if entry["owned"] or entry["included"]:
        _fail(409, "This server already has that.")
    base = _pay_base()
    if not base:
        _fail(503, "Checkout isn't configured yet.")
    token = _secrets.token_urlsafe(16)
    intent = {"payment_type": plan["id"], "user_id": int(uid), "guild_id": gid, "clone_id": CLONE_ID,
              "price_usd": entry["price_usd"], "extra": {"source": "dashboard"}, "created": time.time()}
    await db.set_global_setting(f"payintent:{token}", json.dumps(intent))
    logger.info("dashboard checkout guild=%s user=%s plan=%s", gid, uid, plan["id"])
    return {"ok": True, "url": f"{base}/pay?t={token}"}


_last_action: dict = {}
ACTION_MIN_INTERVAL = 8.0
PANEL_PERMS_HINT = "Give me View Channel, Send Messages, Embed Links and Attach Files there, then try again."


async def _bot_post(path: str, json_body=None, form=None):
    """POST as the bot. JSON bodies retry once on a rate limit; multipart uploads can't be
    replayed, so a 429 there is reported to the caller instead."""
    headers = {"Authorization": f"Bot {config.DISCORD_BOT_TOKEN}"}
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
    key = (uid, gid, action_id)
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
        await db.set_verification_config(gid, CLONE_ID, message_id=int(msg["id"]))
        summary, label = _channel_name(meta, cid), "Verify panel posted"
    elif action_id == "ticket_panel":
        cid = need_channel("panel_channel_id", "panel channel")
        msg = await _post_to_channel(meta, cid, json_body={
            "embeds": [{"title": "🎫 Support Tickets", "color": 0x5865F2,
                        "description": "Click **Open Ticket** to create a private support channel."}],
            "components": [{"type": 1, "components": [{"type": 2, "style": 1, "label": "Open Ticket",
                                                        "emoji": {"name": "🎫"}, "custom_id": "ticket:open"}]}],
            "allowed_mentions": {"parse": []}})
        await db.set_ticket_config(gid, CLONE_ID, panel_channel_id=cid, panel_message_id=int(msg["id"]))
        summary, label = _channel_name(meta, cid), "Ticket panel posted"
    else:  # welcome_test
        from modules import welcome_card as wc
        cid = need_channel("channel_id", "welcome channel")
        premium = bool(await db.is_guild_premium_active(gid, CLONE_ID))
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


async def _oauth_login():
    if not (config.DASH_PAGES_URL and config.DASH_OAUTH_REDIRECT_URI and config.DISCORD_OAUTH_CLIENT_SECRET):
        _fail(503, "The dashboard isn't configured yet.")
    state = _secrets.token_urlsafe(24)
    await db.create_login_oauth_state(state, return_to="dash")
    q = {"client_id": config.DISCORD_OAUTH_CLIENT_ID, "redirect_uri": config.DASH_OAUTH_REDIRECT_URI,
         "response_type": "code", "scope": "identify guilds", "state": state, "prompt": "none"}
    raise _Reply(302, location=f"{AUTHORIZE_URL}?{urlencode(q)}")


def _back(fragment: str):
    raise _Reply(302, location=f"{config.DASH_PAGES_URL}/#{fragment}")


async def _oauth_callback(query: dict):
    if query.get("error"):
        _back("error=" + urlencode({"": "Sign-in was cancelled."})[1:])
    state, code = query.get("state", [None])[0], query.get("code", [None])[0]
    popped = await db.pop_login_oauth_state(state) if state else None
    if not popped or popped.get("return_to") != "dash" or not code:
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
    avatar = (f"https://cdn.discordapp.com/avatars/{uid}/{me['avatar']}.png?size=64" if me.get("avatar")
              else f"https://cdn.discordapp.com/embed/avatars/{(int(uid) >> 22) % 6}.png")
    sid = await db.create_login_session({
        "kind": "dash",
        "user": {"id": uid, "username": me.get("global_name") or me.get("username") or "Discord user", "avatar_url": avatar},
        "guilds": S.guild_list_manageable(guilds),
    })
    _back("session=" + sid)


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

    if method == "GET" and action == "me":
        try:
            present = await _bot_guild_ids()
        except DiscordError:
            _fail(502, "Couldn't reach Discord. Try again in a moment.")
        servers = [{**g, "icon_url": _icon(g), "bot_present": g["id"] in present} for g in sess.get("guilds", [])]
        servers.sort(key=lambda g: (not g["bot_present"], g["name"].lower()))
        uid = str(sess["user"]["id"])
        try:
            unread = sum(1 for m in await db.dropbox_list(uid) if not m["read"])
        except Exception:
            logger.exception("dashboard: dropbox unread count failed")
            unread = 0
        raise _Reply(200, {"ok": True, "user": sess["user"], "servers": servers,
                           "is_owner": _is_owner(sess), "unread": unread})

    if method == "GET" and action == "guild":
        gid = await _authorised_guild(sess, q("guild_id"))
        info = await _guild_info(gid)
        premium = bool(await db.is_guild_premium_active(gid, CLONE_ID))
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
            "premium": premium, "status": status}})

    if method == "GET" and action == "meta":
        gid = await _authorised_guild(sess, q("guild_id"))
        raise _Reply(200, {"ok": True, **await _meta(gid)})

    if method == "GET" and action == "config":
        gid = await _authorised_guild(sess, q("guild_id"))
        module = S.BY_ID.get(q("module") or "")
        if not module:
            _fail(404, "Unknown module.")
        premium = bool(await db.is_guild_premium_active(gid, CLONE_ID))
        cfg = await _get_cfg(module, gid)
        raise _Reply(200, {"ok": True, "values": S.export_values(module, cfg), "premium": _eff_premium(module, premium, cfg)})

    if method == "POST" and action == "save":
        gid = await _authorised_guild(sess, body.get("guild_id"))
        module = S.BY_ID.get(str(body.get("module") or ""))
        if not module:
            _fail(404, "Unknown module.")
        meta = await _meta(gid)
        chans, roles = _id_sets(meta)
        premium = bool(await db.is_guild_premium_active(gid, CLONE_ID))
        before_cfg = await _get_cfg(module, gid)
        clean, errors = S.validate_values(module, body.get("values"), chans, roles, _eff_premium(module, premium, before_cfg))
        if errors:
            raise _Reply(422, {"ok": False, "message": errors[0], "errors": errors})
        await getattr(db, module["set"])(gid, CLONE_ID, **clean)
        logger.info("dashboard save guild=%s user=%s module=%s keys=%s", gid, sess["user"]["id"], module["id"], sorted(clean))
        after = S.export_values(module, await _get_cfg(module, gid))
        try:
            changes = S.diff_values(module, S.export_values(module, before_cfg), after)
            if changes:
                await db.dash_audit_add(gid, str(sess["user"]["id"]), sess["user"].get("username") or "Unknown",
                                        module["id"], changes, S.AUDIT_RETENTION_DAYS)
        except Exception:
            logger.exception("dashboard: audit write failed (save itself succeeded)")
        raise _Reply(200, {"ok": True, "values": after})

    if method == "POST" and action == "bot_action":
        gid = await _authorised_guild(sess, body.get("guild_id"))
        raise _Reply(200, await _bot_action(sess, gid, body.get("id")))

    if method == "GET" and action == "billing":
        gid = await _authorised_guild(sess, q("guild_id"))
        raise _Reply(200, {"ok": True, **await _billing(gid)})

    if method == "POST" and action == "checkout":
        gid = await _authorised_guild(sess, body.get("guild_id"))
        raise _Reply(200, await _checkout(sess, gid, body.get("plan")))

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

    if method == "GET" and action == "welcome_preview":
        gid = await _authorised_guild(sess, q("guild_id"))
        raise _Reply(200, await _welcome_preview(sess, gid, q))

    if method == "GET" and action == "dropbox":
        msgs = await db.dropbox_list(str(sess["user"]["id"]))
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
                if n > MAX_BODY:
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
