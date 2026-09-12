# path: api/selar_submit.py

"""
Backs the web /unlock page's "I've Paid" button (app/unlock/unlock-status.tsx)
— the web-side replacement for the old Discord "I've Paid" DM button (see
payments_manual.py's module docstring for the full flow).

Re-verifies the same HMAC signature api/selar_redirect.py already checked
(defense in depth — this endpoint is a public POST, so it must not trust
that a request only ever arrives via that redirect), then atomically claims
the payment for review via db.claim_manual_payment_for_review. That claim
is the actual duplicate-tap guard: a second POST for the same reference
(double-tap, page reload + resubmit, or a replayed request) finds the row
already flipped out of 'pending' and gets a no-op response back — the
frontend dims/disables its own button immediately on tap as a first line
of defense, but this is what actually prevents a second approval DM.

No live discord.py Client here (this runs in the Railway API-server
process, not the gateway-connected bot — see discord_bot/dm_send.py's
docstring), so approver DMs go over plain REST via dm_user_with_buttons,
carrying Approve/Reject buttons whose custom_id matches the
discord.ui.DynamicItem templates registered in discord_bot/bot.py
(payments_manual.MANUAL_PAYMENT_DYNAMIC_ITEMS) — whichever bot process is
actually connected to the gateway when a button is clicked reconstructs
the item from that custom_id alone.

POST body (JSON): {reference, payment_type, buyer_id, sig, guild_id?, clone_id?}
— the same fields app/unlock forwarded from api/selar_redirect.py's query
string, round-tripped back to us so this endpoint never has to trust a
bare reference on its own.
"""
import json
import logging
import asyncio
import re
from http.server import BaseHTTPRequestHandler

from database import db
from config import (
    SELAR_PRODUCT_LINKS, DISCORD_CLONE_ADMIN_IDS, DISCORD_BOT_TOKEN,
    WELCOME_CARD_PACK_FEE_USD, ULTRA_PACK_FEE_USD, CLONE_MONETIZATION_FEE_GHS, CLONE_BOT_FEE_GHS,
)
from utils.selar_signing import verify_selar_target
from discord_bot.dm_send import dm_user_with_buttons

logger = logging.getLogger(__name__)

_REFERENCE_RE = re.compile(r"^[A-Za-z0-9._:-]{6,160}$")
_PAYMENT_TYPE_RE = re.compile(r"^[a-z_]{1,50}$")

# Mirrors discord_bot/cogs/_views_direct_paid.py's _AMOUNT_DISPLAY — this
# endpoint has no live interaction to read a caller-supplied amount_display
# string from, so the approver DM looks the price up by payment_type instead.
_AMOUNT_DISPLAY = {
    "welcome_card_pack": f"${WELCOME_CARD_PACK_FEE_USD:g} USD",
    "ultra_welcome_pack": f"${ULTRA_PACK_FEE_USD:g} USD",
    "discord_clone_monetization": f"₵{CLONE_MONETIZATION_FEE_GHS:g} GHS",
    "discord_clone": f"₵{CLONE_BOT_FEE_GHS:g} GHS",
}


class handler(BaseHTTPRequestHandler):

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _json(self, status: int, payload: dict):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self._cors()
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode())

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b"{}"
            body = json.loads(raw or b"{}")
        except Exception:
            self._json(400, {"status": "error", "message": "Invalid JSON body"})
            return

        reference = str(body.get("reference") or "").strip()
        payment_type = str(body.get("payment_type") or "").strip()
        sig = str(body.get("sig") or "").strip()
        buyer_id_raw = body.get("buyer_id")
        ts_raw = body.get("ts")
        guild_id_raw = body.get("guild_id")
        clone_id_raw = body.get("clone_id")

        try:
            buyer_id = int(buyer_id_raw)
            ts = int(ts_raw)
            guild_id = int(guild_id_raw) if guild_id_raw not in (None, "") else None
            clone_id = int(clone_id_raw) if clone_id_raw not in (None, "") else None
        except (TypeError, ValueError):
            self._json(400, {"status": "error", "message": "Invalid payment target"})
            return

        if not _REFERENCE_RE.match(reference) or not _PAYMENT_TYPE_RE.match(payment_type):
            self._json(400, {"status": "error", "message": "Invalid payment target"})
            return

        if not verify_selar_target(reference, payment_type, buyer_id, guild_id, clone_id, ts, sig):
            logger.warning(f"[selar-submit] rejected submission for reference={reference!r} (bad/expired signature)")
            self._json(403, {"status": "error", "message": "This confirmation link is invalid or has expired — reopen the payment from Discord"})
            return

        async def _run():
            claimed = await db.claim_manual_payment_for_review(reference)
            if not claimed:
                # Either already submitted once, or already resolved by an
                # admin — either way, not our job to notify again.
                existing = await db.get_payment_by_reference(reference)
                return None, existing
            return claimed, claimed

        try:
            claimed, current = asyncio.run(_run())
        except Exception as e:
            logger.error(f"[selar-submit] DB error claiming reference {reference}: {e}")
            self._json(500, {"status": "error", "message": "Internal error"})
            return

        if not claimed:
            status = (current or {}).get("status", "pending")
            self._json(200, {"status": status, "submitted": True, "notified": False})
            return

        amount_display = _AMOUNT_DISPLAY.get(payment_type, payment_type.replace("_", " ").title())
        try:
            asyncio.run(_notify_approvers(claimed, guild_id, clone_id, amount_display))
        except Exception as e:
            # The claim itself already succeeded and is durably recorded —
            # a failed DM is not fatal, an admin can still find this row
            # via its 'awaiting_review' status and approve manually.
            logger.error(f"[selar-submit] failed to notify approvers for reference {reference}: {e}")

        self._json(200, {"status": "awaiting_review", "submitted": True, "notified": True})

    def log_message(self, format, *args):
        logger.debug(f"[selar-submit] {format % args}")


async def _resolve_discord_name(user_id: int, bot_token: str) -> str:
    """GET /users/{id} works off the bot token alone — unlike a mutual-
    server member lookup, this resolves ANY Discord user by id, which is
    exactly why <@user_id> mentions were showing as "@unknown-user" in
    admin DMs (Discord's client can only render a mention it has cached
    locally; it doesn't fetch on your behalf just because the DM contains
    one). Falls back to the raw id string on any failure so the DM never
    ends up with a blank buyer field."""
    import aiohttp
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
            async with session.get(
                f"https://discord.com/api/v10/users/{user_id}",
                headers={"Authorization": f"Bot {bot_token}"},
            ) as resp:
                if resp.status != 200:
                    return str(user_id)
                data = await resp.json()
                username = data.get("username")
                discriminator = data.get("discriminator")
                if not username:
                    return str(user_id)
                # Post-2023 Discord usernames have discriminator "0" (migrated
                # off the old tag system) — only show #1234 for legacy accounts
                # that still have a real one.
                if discriminator and discriminator != "0":
                    return f"{username}#{discriminator}"
                return username
    except Exception as e:
        logger.warning(f"[selar-submit] couldn't resolve username for {user_id}: {e}")
        return str(user_id)


async def _resolve_guild_name(guild_id: int, bot_token: str) -> str:
    """GET /guilds/{id} only succeeds if this bot token's bot is actually a
    member of that guild — which it always is here, since a manual payment
    can only have been started from within that guild in the first place."""
    import aiohttp
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
            async with session.get(
                f"https://discord.com/api/v10/guilds/{guild_id}",
                headers={"Authorization": f"Bot {bot_token}"},
            ) as resp:
                if resp.status != 200:
                    return str(guild_id)
                data = await resp.json()
                return data.get("name") or str(guild_id)
    except Exception as e:
        logger.warning(f"[selar-submit] couldn't resolve guild name for {guild_id}: {e}")
        return str(guild_id)


async def _notify_approvers(payment_row: dict, guild_id, clone_id, amount_display: str) -> None:
    payment_id = payment_row["payment_id"]
    reference = payment_row["paystack_reference"]
    payment_type = payment_row["payment_type"]
    buyer_id = payment_row["user_id"]

    approver_ids = set(DISCORD_CLONE_ADMIN_IDS)
    bot_token = DISCORD_BOT_TOKEN
    if clone_id:
        clone = await db.get_discord_clone(clone_id)
        if clone:
            if clone.get("owner_id"):
                approver_ids.add(clone["owner_id"])
            if clone.get("bot_token_encrypted"):
                from utils.crypto import secret_manager
                decrypted = secret_manager.decrypt(clone["bot_token_encrypted"])
                if decrypted:
                    bot_token = decrypted

    if not bot_token:
        logger.error(f"[selar-submit] no bot token available to notify approvers for reference {reference}")
        return

    # Resolve real names up front — a raw snowflake id in the DM gives you
    # nothing to act on, and <@id> mentions silently render as
    # "@unknown-user" whenever Discord's client has no cached data for
    # that user (exactly what was happening before this fix).
    buyer_name = await _resolve_discord_name(buyer_id, bot_token)
    guild_line = ""
    if guild_id is not None:
        guild_name = await _resolve_guild_name(guild_id, bot_token)
        guild_line = f"Server: **{guild_name}** (`{guild_id}`)\n"
    elif clone_id:
        guild_line = f"Clone: `#{clone_id}`\n"
    else:
        guild_line = "Scope: account-level\n"

    msg = (
        f"💰 **Manual payment — buyer confirmed on the web**\n"
        f"Buyer: **{buyer_name}** (`{buyer_id}`)\n"
        f"Type: `{payment_type}` — {amount_display}\n"
        f"Reference: `{reference}`\n"
        f"{guild_line}\n"
        f"Check Selar for a matching sale (buyer email `user_{buyer_id}@animebot.com`), "
        f"then Approve or Reject below."
    )
    buttons = [
        {"label": "Approve", "style": 3, "custom_id": f"manualpay_approve:{payment_id}"},
        {"label": "Reject", "style": 4, "custom_id": f"manualpay_reject:{payment_id}"},
    ]
    for admin_id in approver_ids:
        sent = await dm_user_with_buttons(admin_id, msg, buttons, bot_token)
        if not sent:
            logger.warning(f"[selar-submit] couldn't DM approver {admin_id} for reference {reference}")
