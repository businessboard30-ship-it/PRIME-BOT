# path: api/selar_submit.py

"""
Backs the web /unlock page's "I've Paid" button (app/unlock/unlock-status.tsx)
— see payments_manual.py's module docstring for the full flow.

Selar's product-level "redirect after purchase" is a single static URL per
product with nothing appended (confirmed) — there is no per-buyer reference/
signature round-tripped back to us the way the old design assumed. So this
endpoint identifies the buyer the only way it can: via the Discord OAuth
session api/discord_login_oauth.py already minted for them when they signed
in on /unlock (session_id is opaque and server-verified — the buyer's real
Discord user_id is read back out of the stored session payload, never
trusted from anything the browser sends directly).

Once identified, db.get_latest_pending_selar_payment(user_id, payment_type)
finds the 'pending' payment_logs row start_manual_payment() wrote the moment
the buyer tapped "Pay on Selar" in Discord, and db.claim_manual_payment_
for_review atomically flips it to 'awaiting_review' — that claim is the
actual duplicate-tap guard: a second POST for the same row (double-tap, page
reload + resubmit) finds it already out of 'pending' and gets a no-op back.

This does NOT prove the buyer actually paid — it only proves which Discord
account is claiming to have. The real check is still a human: the approver
DM tells the admin to cross-reference the Selar dashboard's buyer-typed
Discord username/server name (Selar Custom Checkout Form fields) before
tapping Approve.

POST body (JSON): {session_id, payment_type}
"""
import json
import logging
import asyncio
import re
from http.server import BaseHTTPRequestHandler

from database import db
from config import (
    DISCORD_CLONE_ADMIN_IDS, DISCORD_BOT_TOKEN,
    WELCOME_CARD_PACK_FEE_USD, ULTRA_PACK_FEE_USD, CLONE_MONETIZATION_FEE_GHS, CLONE_BOT_FEE_GHS,
)
from discord_bot.dm_send import dm_user_with_buttons

logger = logging.getLogger(__name__)

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

        session_id = str(body.get("session_id") or "").strip()
        payment_type = str(body.get("payment_type") or "").strip()

        if not session_id or not _PAYMENT_TYPE_RE.match(payment_type):
            self._json(400, {"status": "error", "message": "Missing or invalid payment target"})
            return

        async def _run():
            session = await db.get_login_session(session_id)
            if not session or not session.get("user", {}).get("id"):
                return "no_session", None, None

            try:
                user_id = int(session["user"]["id"])
            except (TypeError, ValueError):
                return "no_session", None, None

            pending = await db.get_latest_pending_selar_payment(user_id, payment_type)
            if not pending:
                return "no_pending", None, None

            claimed = await db.claim_manual_payment_for_review(pending["paystack_reference"])
            if not claimed:
                # Someone already submitted this exact row (double-tap,
                # reload+resubmit) — not our job to notify again, just
                # report its current status back.
                existing = await db.get_payment_by_reference(pending["paystack_reference"])
                return "already_claimed", session, existing

            return "claimed", session, claimed

        try:
            outcome, session, row = asyncio.run(_run())
        except Exception as e:
            logger.error(f"[selar-submit] DB error for session={session_id!r} payment_type={payment_type!r}: {e}")
            self._json(500, {"status": "error", "message": "Internal error"})
            return

        if outcome == "no_session":
            self._json(401, {"status": "error", "message": "Your sign-in expired — sign in with Discord again."})
            return

        if outcome == "no_pending":
            self._json(404, {
                "status": "error",
                "message": "No pending payment found for your Discord account — start the payment again from Discord, then come back here.",
            })
            return

        if outcome == "already_claimed":
            status = (row or {}).get("status", "pending")
            reference = (row or {}).get("paystack_reference", "")
            self._json(200, {"status": status, "reference": reference, "submitted": True, "notified": False})
            return

        # outcome == "claimed"
        reference = row["paystack_reference"]
        amount_display = _AMOUNT_DISPLAY.get(payment_type, payment_type.replace("_", " ").title())
        try:
            asyncio.run(_notify_approvers(row, amount_display))
        except Exception as e:
            # The claim itself already succeeded and is durably recorded —
            # a failed DM is not fatal, an admin can still find this row
            # via its 'awaiting_review' status and approve manually.
            logger.error(f"[selar-submit] failed to notify approvers for reference {reference}: {e}")

        self._json(200, {"status": "awaiting_review", "reference": reference, "submitted": True, "notified": True})

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


async def _notify_approvers(payment_row: dict, amount_display: str) -> None:
    payment_id = payment_row["payment_id"]
    reference = payment_row["paystack_reference"]
    payment_type = payment_row["payment_type"]
    buyer_id = payment_row["user_id"]
    guild_id = payment_row.get("chat_id")
    clone_id = payment_row.get("clone_id")

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
    # that user.
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
        f"💰 **Manual payment — buyer confirmed on the web (Discord sign-in)**\n"
        f"Buyer: **{buyer_name}** (`{buyer_id}`)\n"
        f"Type: `{payment_type}` — {amount_display}\n"
        f"Reference: `{reference}`\n"
        f"{guild_line}\n"
        f"Cross-check the Selar dashboard's buyer-typed Discord username/server name "
        f"(buyer email `user_{buyer_id}@animebot.com`) against the above before approving, "
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
