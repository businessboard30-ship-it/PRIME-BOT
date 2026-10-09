# path: api/dash_dev.py
"""Developer mode (#/dev). Visible to every signed-in user, usable only with an active dev entitlement.

Enforcement is SERVER-SIDE: `dev_status` works for everyone (it drives the locked screen); every other
dev_* handler must start with `gate = await require_dev(uid, db)` and return it when set (402,
code "subscription_required"). The locked screen in the browser is cosmetic and grants nothing.
Nothing here writes entitlements: only the payment webhook does. No route takes a user id from the client.
"""
import json
import logging

from modules import ai_usage, dev_chat, dev_export, dev_github, dev_keys
from modules import entitlements as ent
from modules import user_subs
from api.dash_member import pay_view

logger = logging.getLogger(__name__)
SOURCE = "dev"                 # counter source in user_ai_usage; the card plan uses "card_plan"
WEEKLY_BOT_CHATS = ai_usage.LIMITS[SOURCE]     # bot-provided AI messages per week for Developer subscribers
FEATURES = (
    {"key": "chat", "label": "AI chat", "ready": True},
    {"key": "export", "label": "Export to your DMs", "ready": True},
    {"key": "keys", "label": "Bring your own AI key (Claude, Groq, OpenAI)", "ready": True},
    {"key": "github", "label": "Connect GitHub (read-only)", "ready": True},
)


def _gate_denied() -> dict:
    return {"_status": 402, "code": "subscription_required", "message": "Developer mode needs an active subscription."}


async def require_dev(uid, db):
    """None when the SESSION user has active Developer access, else the 402 reply dict."""
    rows = await db.entitlements_list(uid)
    return None if ent.has_access(rows, ent.DEV_PRODUCTS) else _gate_denied()


def _dev_plans(rows) -> list:
    by_product = {r.get("product"): r for r in rows or []}
    out = []
    for product in ent.DEV_PRODUCTS:
        plan = user_subs.PLANS[product]
        state = ent.effective(by_product[product])["state"] if product in by_product else "none"
        out.append({"product": product, "label": plan["label"], "price_usd": plan["price_usd"],
                    "period_days": plan["period_days"], "state": state})
    return out


async def dev_status(uid, q, db):
    """Works for everyone. Only whitelisted fields; the browser uses `unlocked` to pick the screen."""
    rows = await db.entitlements_list(uid)
    unlocked = ent.has_access(rows, ent.DEV_PRODUCTS)
    exp = ent.latest_expiry(rows, ent.DEV_PRODUCTS)
    return {"unlocked": unlocked, "expires_at": exp.isoformat() if (exp and unlocked) else None,
            "export_available": ent.export_allowed(rows), "plans": _dev_plans(rows),
            "pay": await pay_view(db), "features": [dict(f) for f in FEATURES]}


async def dev_overview(uid, q, db):
    gate = await require_dev(uid, db)
    if gate:
        return gate
    return {"features": [dict(f) for f in FEATURES], "weekly_bot_chats": WEEKLY_BOT_CHATS}


def _usage_view(st: dict) -> dict:
    limit = WEEKLY_BOT_CHATS
    return {"used": st["used"], "limit": limit, "remaining": max(limit - st["used"], 0), "resets_at": st["resets_at"]}


async def dev_usage(uid, q, db):
    gate = await require_dev(uid, db)
    if gate:
        return gate
    conns = await db.dev_connection_list(uid)
    models = [{"id": "default", "label": "Bot AI (default)"}] + [
        {"id": c["provider"], "label": dev_keys.PROVIDERS[c["provider"]]["label"] + " (your key)"}
        for c in conns if c.get("provider") in dev_keys.PROVIDERS]
    return {**_usage_view(await ai_usage.status(db, uid, SOURCE)), "models": models}


async def dev_chat_send(uid, body, db):
    """POST {messages: [{role, content}]}. The conversation is kept by the browser, never stored here.
    Order matters: gate, kill switch, validate, spend one chat atomically, call the model, refund on failure."""
    gate = await require_dev(uid, db)
    if gate:
        return gate
    model = str((body or {}).get("model") or "default").strip().lower()
    if model != "default":
        return await _chat_with_own_key(uid, body, db, model)
    from modules import admin_controls
    if "ai" in await admin_controls.current_switches():
        return {"_status": 503, "message": "AI chat is switched off right now."}
    messages, err = dev_chat.clean_messages((body or {}).get("messages"))
    if err:
        return {"_status": 422, "message": err}
    ws = ai_usage.week_start()
    ok, st = await ai_usage.consume(db, uid, SOURCE)
    if not ok:
        reset = ai_usage.resets_at().strftime("%a %d %b, %H:%M UTC")
        return {"_status": 429, "code": "weekly_limit", "message": f"You've used all {WEEKLY_BOT_CHATS} chats this week. They reset {reset}."}
    try:
        text = await dev_chat.ask(messages)
    except Exception as e:
        await db.ai_usage_refund(uid, ws, SOURCE)
        if not isinstance(e, RuntimeError):
            logger.exception("dev chat failed")
        return {"_status": 502, "message": str(e) if isinstance(e, RuntimeError) else "The AI service is busy. Try again in a moment."}
    return {"reply": text, **_usage_view(st)}


async def _chat_with_own_key(uid, body, db, provider):
    """The person's own key: server to provider only, NOT counted against the weekly 50, never returned."""
    provider = dev_keys.clean_provider(provider)
    if not provider:
        return {"_status": 422, "message": "Pick a model from the list."}
    messages, err = dev_chat.clean_messages((body or {}).get("messages"))
    if err:
        return {"_status": 422, "message": err}
    enc = await db.dev_connection_secret(uid, provider)
    key = _decrypt(enc) if enc else None
    if not key:
        return {"_status": 409, "code": "no_key", "message": "Add that key in Keys first."}
    try:
        text = await dev_keys.chat(provider, key, dev_chat.SYSTEM_PROMPT, messages)
    except RuntimeError as e:
        return {"_status": 502, "message": str(e)}
    except Exception:
        logger.error("dev own-key chat failed (provider=%s)", provider)       # no exception text: it could echo request data
        return {"_status": 502, "message": "The AI service is busy. Try again in a moment."}
    return {"reply": text, "own_key": True, "provider": provider}


def _decrypt(enc):
    from utils.crypto import secret_manager
    return secret_manager.decrypt(enc)


async def dev_keys_list(uid, q, db):
    gate = await require_dev(uid, db)
    if gate:
        return gate
    rows = await db.dev_connection_list(uid)
    return {"connections": [dev_keys.public_view(r) for r in rows if r.get("provider") in dev_keys.PROVIDERS],
            "providers": [{"id": k, "label": v["label"]} for k, v in dev_keys.PROVIDERS.items()],
            "kept_days_after_expiry": dev_keys.GRACE_DAYS}


async def dev_key_save(uid, body, db):
    """Add or replace one provider key. Step-up (fresh Discord sign-in) is enforced by the router before this runs.
    Validates with one free call, then stores ciphertext. The key is never returned, logged or put in an error."""
    gate = await require_dev(uid, db)
    if gate:
        return gate
    provider = dev_keys.clean_provider((body or {}).get("provider"))
    if not provider:
        return {"_status": 422, "message": "Pick a provider."}
    key, err = dev_keys.clean_key((body or {}).get("key"))
    if err:
        return {"_status": 422, "message": err}
    existing = {c["provider"] for c in await db.dev_connection_list(uid) if c.get("provider") in dev_keys.PROVIDERS}
    if provider not in existing and len(existing) >= dev_keys.MAX_CONNECTIONS:
        return {"_status": 422, "message": "You've reached the limit of saved keys."}
    ok, verr = await dev_keys.validate(provider, key)
    if not ok:
        return {"_status": 422, "message": verr}
    from utils.crypto import secret_manager
    await db.dev_connection_upsert(uid, provider, secret_manager.encrypt(key), dev_keys.last4(key))
    rows = await db.dev_connection_list(uid)
    return {"connections": [dev_keys.public_view(r) for r in rows if r.get("provider") in dev_keys.PROVIDERS]}


async def dev_key_remove(uid, body, db):
    gate = await require_dev(uid, db)
    if gate:
        return gate
    provider = dev_keys.clean_provider((body or {}).get("provider"))
    if not provider:
        return {"_status": 422, "message": "Pick a provider."}
    await db.dev_connection_delete(uid, provider)
    rows = await db.dev_connection_list(uid)
    return {"connections": [dev_keys.public_view(r) for r in rows if r.get("provider") in dev_keys.PROVIDERS]}


# ---------- GitHub connect (C4, read-only). The token is encrypted in dev_connections (provider "github"), never returned ----------
async def _gh_secret(uid, db):
    """(token, login) for the SESSION user, or (None, None). Server-side only: the token never goes into a response."""
    enc = await db.dev_connection_secret(uid, dev_github.PROVIDER)
    if not enc:
        return None, None
    try:
        return dev_github.unpack_secret(_decrypt(enc))
    except Exception:
        return None, None


async def _gh_status(uid, db) -> dict:
    token, login = await _gh_secret(uid, db)
    row = next((r for r in await db.dev_connection_list(uid) if r.get("provider") == dev_github.PROVIDER), None)
    return {"configured": dev_github.configured(), "connected": bool(token), "login": login if token else None,
            "connected_at": dev_keys._iso(row.get("created_at")) if (row and token) else None,
            "access": "Read-only. Nothing is ever written to GitHub from here."}


async def dev_github_status(uid, q, db):
    gate = await require_dev(uid, db)
    return gate or await _gh_status(uid, db)


async def dev_github_connect(uid, body, db):
    """Start the GitHub OAuth round trip. Step-up is enforced by the router. State + PKCE; the state is bound to this user."""
    gate = await require_dev(uid, db)
    if gate:
        return gate
    if not dev_github.configured():
        return {"_status": 503, "message": "GitHub connect isn't set up yet."}
    state, verifier = dev_github.new_state(), dev_github.new_verifier()
    await db.create_login_oauth_state(state, return_to=dev_github.pack_return(uid, verifier))
    return {"url": dev_github.authorize_url(state, verifier)}


async def dev_github_finish(uid, body, db):
    """The browser returns here with the code and state GitHub sent back. The state must have been issued to THIS session's
    user, so a link someone else started can't connect the wrong account. Exchanges the code with the PKCE verifier."""
    gate = await require_dev(uid, db)
    if gate:
        return gate
    if not dev_github.configured():
        return {"_status": 503, "message": "GitHub connect isn't set up yet."}
    code, state = dev_github.clean_code((body or {}).get("code")), dev_github.clean_state((body or {}).get("state"))
    if not code or not state:
        return {"_status": 422, "message": "That GitHub link isn't valid. Start again."}
    popped = await db.pop_login_oauth_state(state)
    parsed = dev_github.unpack_return((popped or {}).get("return_to"))
    if not parsed or parsed[0] != str(uid):
        return {"_status": 422, "message": "That GitHub link expired or isn't yours. Start again."}
    token, err = await dev_github.exchange_code(code, parsed[1])
    if err:
        return {"_status": 502, "message": err}
    try:
        login = await dev_github.whoami(token)
    except RuntimeError as e:
        return {"_status": 502, "message": str(e)}
    from utils.crypto import secret_manager
    await db.dev_connection_upsert(uid, dev_github.PROVIDER, secret_manager.encrypt(dev_github.pack_secret(token, login)), dev_github.last4(token))
    return await _gh_status(uid, db)


async def dev_github_disconnect(uid, body, db):
    gate = await require_dev(uid, db)
    if gate:
        return gate
    token, _ = await _gh_secret(uid, db)
    await db.dev_connection_delete(uid, dev_github.PROVIDER)
    if token:
        await dev_github.revoke(token)            # best effort; the local copy is already gone
    return await _gh_status(uid, db)


async def _gh_read(uid, db, fn):
    """Callers have already passed require_dev (every handler starts with it)."""
    token, _ = await _gh_secret(uid, db)
    if not token:
        return {"_status": 422, "code": "github_not_connected", "message": "Connect GitHub first."}
    try:
        return await fn(token)
    except RuntimeError as e:
        return {"_status": 502, "message": str(e)}


async def dev_github_repos(uid, q, db):
    gate = await require_dev(uid, db)
    if gate:
        return gate

    async def go(token):
        return {"repos": await dev_github.list_repos(token)}
    return await _gh_read(uid, db, go)


async def dev_github_browse(uid, q, db):
    gate = await require_dev(uid, db)
    if gate:
        return gate
    repo, path, ref = dev_github.clean_repo(q("repo")), dev_github.clean_path(q("path")), dev_github.clean_ref(q("ref"))
    if not repo or path is False or ref is False:
        return {"_status": 422, "message": "That repository, path or branch isn't valid."}

    async def go(token):
        return await dev_github.browse(token, repo, path, ref)
    return await _gh_read(uid, db, go)


async def dev_github_diff(uid, q, db):
    gate = await require_dev(uid, db)
    if gate:
        return gate
    repo = dev_github.clean_repo(q("repo"))
    suffix, err = dev_github.clean_diff_spec(q("kind"), q("a"), q("b"))
    if not repo or err:
        return {"_status": 422, "message": err or "That repository isn't valid."}

    async def go(token):
        return await dev_github.get_diff(token, repo, suffix)
    return await _gh_read(uid, db, go)


# ---------- Export (C2). Gated by ent.export_allowed(rows), NOT require_dev: it keeps working 7 days after the plan ends ----------
async def _export_gate(uid, db):
    """None when the SESSION user may export (active plan or inside the 7-day grace), else the 402 reply dict."""
    rows = await db.entitlements_list(uid)
    return None if ent.export_allowed(rows) else _gate_denied()


def _storage_channel() -> int:
    import config
    return int(getattr(config, "DEV_STORAGE_CHANNEL_ID", 0) or 0)


def _upload_form(payload: dict, filename: str, data: bytes, mime: str):
    import aiohttp
    form = aiohttp.FormData()
    form.add_field("payload_json", json.dumps(payload), content_type="application/json")
    form.add_field("files[0]", data, filename=filename, content_type=mime)
    return form


async def _fetch_bytes(url: str, limit: int):
    """GET one attachment from Discord's CDN with a hard size cap. Returns bytes or None."""
    import aiohttp
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as s:
            async with s.get(url) as r:
                if r.status != 200 or (r.content_length or 0) > limit:
                    return None
                data = await r.content.read(limit + 1)
                return data if len(data) <= limit else None
    except Exception:
        return None


async def dev_export_list(uid, q, db):
    gate = await _export_gate(uid, db)
    if gate:
        return gate
    rows = await db.dev_export_list(uid)
    return {"exports": [dev_export.public_view(r) for r in rows], "max_per_day": dev_export.MAX_PER_DAY,
            "max_kb": dev_export.MAX_BYTES // 1024, "kinds": sorted(dev_export.KINDS)}


async def dev_export_create(uid, body, db):
    """POST {kind, name?, content}. Order: gate, kill switch, validate, caps, encrypt, upload ciphertext to the storage
    channel, store the receipt, audit, then a best-effort DM of the plain file to the person."""
    gate = await _export_gate(uid, db)
    if gate:
        return gate
    from modules import admin_controls
    if "dev_export" in await admin_controls.current_switches():
        return {"_status": 503, "message": "Export is switched off right now."}
    spec, err = dev_export.clean_request(body)
    if err:
        return {"_status": 422, "message": err}
    channel = _storage_channel()
    if not channel:
        return {"_status": 503, "message": "Export isn't set up yet."}
    if await db.dev_export_count_since(uid, 24) >= dev_export.MAX_PER_DAY:
        return {"_status": 429, "code": "daily_limit", "message": f"You can export {dev_export.MAX_PER_DAY} files a day. Try again tomorrow."}
    if await db.dev_export_count(uid) >= dev_export.MAX_KEPT:
        return {"_status": 422, "message": "You're keeping the maximum number of exports. Delete some first."}
    from api import dash
    export_id = dev_export.new_export_id()
    form = _upload_form(dev_export.storage_message(export_id), dev_export.opaque_filename(export_id),
                        dev_export.encrypt(spec["data"]), "application/octet-stream")
    try:
        sent = await dash._bot_post(f"/channels/{channel}/messages", form=form)
        message_id = str(sent["id"])
    except Exception as e:
        logger.warning("dev export upload failed: %s", type(e).__name__)
        return {"_status": 502, "message": "Couldn't save the export right now. Try again in a moment."}
    try:
        await db.dev_export_add(export_id, uid, message_id, spec["name"], len(spec["data"]))
    except Exception:
        logger.exception("dev export receipt failed")
        try:
            await dash._bot_request("DELETE", f"/channels/{channel}/messages/{message_id}")
        except Exception:
            pass
        return {"_status": 502, "message": "Couldn't save the export right now. Try again in a moment."}
    try:                                           # best-effort extra audit; the receipt row is the durable record
        await db.owner_audit_add(uid, "", "dev_export", "export", export_id, detail={"size": len(spec["data"]), "kind": spec["kind"]})
    except Exception:
        logger.warning("dev export audit write failed")
    dm_sent = await _send_dm(uid, spec)
    rows = await db.dev_export_list(uid)
    return {"export": next((dev_export.public_view(r) for r in rows if r.get("id") == export_id), {"id": export_id, "name": spec["name"]}),
            "dm_sent": dm_sent}


async def _send_dm(uid, spec) -> bool:
    import config
    from api import dash
    try:
        ch = await dash._bot_post("/users/@me/channels", json_body={"recipient_id": str(uid)})
        form = _upload_form(dev_export.dm_message(spec["name"], f"{config.DASH_PAGES_URL}/#/dev"), spec["name"], spec["data"], spec["mime"])
        await dash._bot_post(f"/channels/{ch['id']}/messages", form=form)
        return True
    except Exception:
        return False                               # DMs closed is normal; the Export page still has the file


async def dev_export_download(uid, q, db):
    gate = await _export_gate(uid, db)
    if gate:
        return gate
    export_id = q("id")
    if not dev_export.valid_export_id(export_id):
        return {"_status": 422, "message": "That export doesn't exist."}
    row = await db.dev_export_get(uid, export_id)             # keyed by the SESSION user: another user's id finds nothing
    if not row:
        return {"_status": 404, "message": "That export doesn't exist."}
    from api import dash
    channel = _storage_channel()
    gone = {"_status": 410, "message": "That export is no longer available."}
    try:
        msg = await dash._bot_get(f"/channels/{channel}/messages/{row['message_id']}")
        att = (msg.get("attachments") or [None])[0]
    except Exception:
        return gone
    if not att or att.get("filename") != dev_export.opaque_filename(export_id):
        return gone
    blob = await _fetch_bytes(att.get("url") or "", dev_export.MAX_BYTES * 2)
    plain = dev_export.decrypt(blob) if blob else None
    if plain is None:
        return gone
    return {"name": row["name"], "content": plain.decode("utf-8", "replace")}


async def dev_export_delete(uid, body, db):
    gate = await _export_gate(uid, db)
    if gate:
        return gate
    export_id = (body or {}).get("id")
    if not dev_export.valid_export_id(export_id):
        return {"_status": 422, "message": "That export doesn't exist."}
    row = await db.dev_export_get(uid, export_id)
    if not row:
        return {"_status": 404, "message": "That export doesn't exist."}
    from api import dash
    try:
        status = await dash._bot_request("DELETE", f"/channels/{_storage_channel()}/messages/{row['message_id']}")
    except Exception:
        status = None
    if status not in (200, 204, 404):
        return {"_status": 502, "message": "Couldn't delete it right now. Try again in a moment."}
    await db.dev_export_delete(uid, export_id)
    return {"deleted": True}


ROUTES = {"dev_status": dev_status, "dev_overview": dev_overview, "dev_usage": dev_usage, "dev_keys": dev_keys_list,
          "dev_export_list": dev_export_list, "dev_export_download": dev_export_download,
          "dev_github": dev_github_status, "dev_github_repos": dev_github_repos, "dev_github_browse": dev_github_browse,
          "dev_github_diff": dev_github_diff}
WRITES = {"dev_chat": dev_chat_send, "dev_key_save": dev_key_save, "dev_key_remove": dev_key_remove,
          "dev_export_create": dev_export_create, "dev_export_delete": dev_export_delete,
          "dev_github_connect": dev_github_connect, "dev_github_finish": dev_github_finish,
          "dev_github_disconnect": dev_github_disconnect}
# Writes that also need a fresh Discord sign-in (step-up). The router checks this set; handlers never see the session.
FRESH_WRITES = frozenset({"dev_key_save", "dev_key_remove", "dev_github_connect", "dev_github_disconnect"})
