"""
Paystack Webhook Handler for Payment Verification
Receives server-to-server payment confirmation from Paystack and activates features
"""

import json
import asyncio
from http.server import BaseHTTPRequestHandler
from payments import paystack
from database import db
import logging

logger = logging.getLogger(__name__)


class handler(BaseHTTPRequestHandler):
    """Handle Paystack webhook events"""
    
    def do_POST(self):
        """Handle incoming Paystack webhook POST request"""
        
        # Read raw request body
        content_length = int(self.headers.get('Content-Length', 0))
        request_body = self.rfile.read(content_length).decode('utf-8')
        
        # Get signature header
        signature = self.headers.get('x-paystack-signature', '')
        
        # Verify signature using constant-time comparison
        if not paystack.verify_webhook(request_body, signature):
            logger.warning("[v0] Webhook signature verification failed")
            self.send_response(401)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({"status": "error", "message": "Unauthorized"}).encode())
            return
        
        # Parse payload
        try:
            payload = json.loads(request_body)
        except json.JSONDecodeError:
            logger.error("[v0] Failed to parse webhook payload JSON")
            self.send_response(400)
            self.end_headers()
            return
        
        # Process based on event type
        event_type = payload.get('event')
        data = payload.get('data', {})
        
        try:
            if event_type == 'charge.success':
                asyncio.run(self._handle_charge_success(data))
            
            # Always return 200 quickly to acknowledge receipt
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({"status": "received"}).encode())
            
        except Exception as e:
            logger.error(f"[v0] Webhook processing error: {e}")
            self.send_response(200)  # Still return 200 so Paystack doesn't retry
            self.end_headers()
    
    async def _handle_charge_success(self, data: dict):
        """Handle successful charge event (Task 1)"""
        
        reference = data.get('reference')
        status = data.get('status')
        metadata = data.get('metadata', {})
        
        if status != 'success' or not reference:
            logger.warning(f"[v0] Invalid charge success event: {data}")
            return
        
        payment_type = metadata.get('type')
        user_id = metadata.get('user_id')
        
        logger.info(f"[v0] Processing payment: {payment_type} for user {user_id}")
        
        try:
            if payment_type == 'premium_group_join':
                # Server-to-server confirmation for the Premium Group paywall
                # (handlers/premium_group_handler.py). This is what makes
                # handlers/moderation.py's handle_join_request payment gate
                # work automatically — without this case, a payment only got
                # marked 'completed' if the user manually tapped "I've Paid —
                # Verify" in chat, so anyone who paid but closed the app
                # before tapping Verify would be stuck unable to join.
                await db.mark_payment_paid(reference)
                logger.info(f"[v0] premium_group_join payment {reference} marked as paid")

                # Discord equivalent: metadata.provider == 'discord' means
                # this payment came from discord_bot/views.py's PremiumPayView,
                # which stashes guild_id AND group_id in extra_metadata (a
                # guild can now have several independently-priced premium
                # groups, so group_id is what tells us which role to grant —
                # guild_id alone is no longer enough). Grant the role
                # directly via Discord's REST API (see discord_bot/role_grant.py)
                # — this process has no live gateway connection, so it can't
                # call member.add_roles() the way the bot process does; this
                # is the closing half of the same gap Telegram's join-request
                # gate closes, just for users who already joined the guild
                # before paying and are just waiting on the role.
                if metadata.get('provider') == 'discord' and user_id:
                    guild_id = metadata.get('guild_id')
                    group_id = metadata.get('group_id')
                    if guild_id and group_id:
                        from discord_bot.role_grant import grant_role
                        group = await db.get_premium_group(int(group_id))
                        if group and group.get('role_id'):
                            # A group created by a clone (clone_id not None)
                            # needs that clone's own bot token — the main
                            # bot's token can't grant roles in a guild it's
                            # not a member of.
                            bot_token = None
                            if group.get('clone_id') is not None:
                                clone = await db.get_discord_clone(int(group['clone_id']))
                                if clone:
                                    from utils.crypto import secret_manager
                                    bot_token = secret_manager.decrypt(clone['bot_token_encrypted'])
                            granted = await grant_role(
                                int(guild_id), int(user_id), int(group['role_id']),
                                reason=f"premium_group_join payment verified (webhook): {group['name']}",
                                bot_token=bot_token,
                            )
                            if not granted:
                                # Not fatal — on_member_join and the in-app
                                # Verify button are both independent, later
                                # chances to grant the same role once
                                # has_paid() is true.
                                logger.warning(
                                    f"[v0] Webhook could not grant Discord role for user {user_id} "
                                    f"in guild {guild_id} group {group_id} — will retry via on_member_join or /verify"
                                )
                        else:
                            logger.warning(
                                f"[v0] Discord premium group {group_id} not found (guild {guild_id}) — "
                                f"payment marked paid but no role to grant"
                            )
                    else:
                        logger.warning(
                            f"[v0] Discord premium payment missing guild_id/group_id in metadata — "
                            f"payment marked paid but no role to grant"
                        )

            elif payment_type == 'group_pay_now':
                # Generic welcome-message "Pay Now" button (any group, any
                # admin-chosen purpose) — just mark it paid. The bot-side
                # "Verify" tap (handlers/welcome_pay.py) is what confirms to
                # the user and posts to the group; this is a backstop so the
                # payment_logs row is correct even if the user never taps
                # Verify.
                await db.mark_payment_paid(reference)
                logger.info(f"[v0] group_pay_now payment {reference} marked as paid")

            elif payment_type == 'bot_clone':
                # Mark clone payment as paid in database (Task 2)
                await db.mark_clone_payment_paid(reference)
                logger.info(f"[v0] Clone payment {reference} marked as paid")
                
            elif payment_type == 'discord_clone':
                # Discord equivalent of 'bot_clone' above, but the token was
                # already collected and validated up front (Discord's
                # /registerclone, unlike Telegram's flow, takes the token in
                # the same command that starts payment) — see
                # discord_bot/cogs/clone_admin.py. All the webhook needs to
                # do is turn the pending row into a real, running clone.
                clone_id = await db.complete_discord_clone_pending_payment(reference)
                if clone_id:
                    logger.info(f"[v0] Discord clone payment {reference} confirmed — clone_id={clone_id} now active")
                else:
                    logger.warning(f"[v0] Discord clone payment {reference} confirmed but no pending row found")

            elif payment_type == 'ai_subscription':
                # Activate AI subscription for user (Task 3) — scoped to
                # whichever bot initiated this payment.
                if user_id:
                    clone_id = int(metadata.get('clone_id', 0) or 0)
                    await db.activate_utility_subscription(int(user_id), days=30, clone_id=clone_id)
                    logger.info(f"[v0] Subscription activated for user {user_id} on clone_id={clone_id}")

            elif payment_type == 'archive_boost':
                # Bots Archive listing boost (discord_bot/cogs/archive.py's
                # /archive boost) — feature the listing at the top of
                # /archive trending for the paid tier's duration.
                from datetime import datetime, timedelta
                from modules import archive_adapter as arc
                listing_id = metadata.get('listing_id')
                tier = metadata.get('tier')
                if listing_id and tier in arc.BOOST_TIERS:
                    hours = arc.BOOST_TIERS[tier]["hours"]
                    expires_at = datetime.utcnow() + timedelta(hours=hours)
                    await arc.record_boost(int(listing_id), int(user_id), tier, expires_at, reference)
                    logger.info(f"[v0] archive_boost activated for listing {listing_id}, tier {tier}, ref {reference}")

            elif payment_type == 'botstore_premium':                # Activate BotStore premium tier (unlimited listings) — scoped
                # to whichever bot (main = 0, or a specific clone) initiated
                # this payment, so it can't grant premium on a different bot.
                if user_id:
                    clone_id = int(metadata.get('clone_id', 0) or 0)
                    await db.set_premium_tier(int(user_id), clone_id=clone_id)
                    logger.info(f"[v0] BotStore premium activated for user {user_id} on clone_id={clone_id}")

            elif payment_type == 'clone_monetization':
                # Activate (or renew) a clone owner's monetization
                # subscription — unlocks connecting their own Paystack/Stripe
                # key and setting their own prices (handlers/clone_bot.py).
                clone_id = int(metadata.get('clone_id', 0) or 0)
                if clone_id:
                    from config import CLONE_MONETIZATION_DAYS
                    await db.activate_monetization_subscription(clone_id, days=CLONE_MONETIZATION_DAYS)
                    logger.info(f"[v0] Monetization activated for clone_id={clone_id}")

            elif payment_type == 'superbot_tier':
                # Activate a SuperBot premium tier (pro/elite)
                tier = metadata.get('tier')
                if user_id and tier:
                    from modules import superbot_adapter
                    await superbot_adapter.set_user_tier(int(user_id), tier)
                    logger.info(f"[v0] SuperBot tier '{tier}' activated for user {user_id}")

            elif payment_type == 'discover_category_upgrade':
                # Discover Players category member-cap upgrade — see
                # discord_bot/cogs/discover_players.py's /discover upgrade
                # command (which creates the pending discover_category_payments
                # row) and DISCOVER_CAP_TIERS in config.py for the tier ladder.
                # This process has no live gateway connection (same
                # constraint noted above for role granting), so there's no
                # DM confirmation from here — the cap is live in the DB
                # immediately, and /discover status reflects it on next use.
                paid = await db.mark_discover_category_payment_paid(reference)
                if paid:
                    logger.info(
                        f"[v0] Discover category {paid['category_id']} cap raised to "
                        f"{paid['cap_to']} (payment {reference})"
                    )
                else:
                    logger.warning(f"[v0] discover_category_upgrade payment {reference} had no matching pending row")

            elif payment_type == 'media_connect_subscription':
                # Activate the Jellyfin/Plex/Google Drive movie-search
                # feature (discord_bot/cogs/media_connect.py) — per-user,
                # works across any server/DM, $2/month (config.MEDIA_CONNECT_FEE_GHS).
                if user_id:
                    await db.activate_media_connect_subscription(int(user_id), reference, days=30)
                    logger.info(f"[v0] Media Connect subscription activated for user {user_id}")
                    
            elif payment_type == 'discord_clone_monetization':
                # Discord equivalent of 'clone_monetization' above — backstop
                # for discord_bot/cogs/clone_admin.py's /clonemonetize activate,
                # which otherwise only activates when the user taps "Verify
                # Payment" in-app (and its pending state was in-memory only
                # in older builds, so it couldn't even survive a restart).
                # start_discord_monetization_payment already wrote a 'pending'
                # discord_clone_monetization_subscriptions row keyed by
                # payment_reference at checkout time; just flip it active.
                from config import CLONE_MONETIZATION_DAYS
                clone_id = await db.activate_discord_monetization_subscription_by_reference(
                    reference, days=CLONE_MONETIZATION_DAYS
                )
                if clone_id:
                    logger.info(f"[v0] discord_clone_monetization payment {reference} confirmed — clone_id={clone_id} activated")
                else:
                    logger.warning(f"[v0] discord_clone_monetization payment {reference} confirmed but no matching pending row found")

            elif payment_type == 'image_search_yandex':
                # Backstop for discord_bot/cogs/image_search.py's Yandex
                # direct-search subscription — start_image_search_yandex_payment
                # already wrote a 'pending' row keyed by payment_reference at
                # checkout time; activate it here too so a user who never
                # taps "Verify Payment" still gets what they paid for.
                from config import IMAGE_SEARCH_YANDEX_DAYS
                activated = await db.activate_image_search_yandex_subscription_by_reference(
                    reference, days=IMAGE_SEARCH_YANDEX_DAYS
                )
                if activated:
                    # Same reusable-card token Paystack returns on any
                    # successful charge — save it if present so the
                    # auto-renew cron can reuse it, same as the in-app
                    # Verify path does.
                    auth_code = (data.get('authorization') or {}).get('authorization_code')
                    if auth_code:
                        await db.save_image_search_yandex_authorization(
                            activated['user_id'], activated['clone_id'], auth_code
                        )
                    logger.info(
                        f"[v0] image_search_yandex payment {reference} confirmed — "
                        f"user_id={activated['user_id']} clone_id={activated['clone_id']} activated"
                    )
                else:
                    logger.warning(f"[v0] image_search_yandex payment {reference} confirmed but no matching pending row found")

            elif payment_type == 'image_search_unlock':
                # Backstop for discord_bot/cogs/image_search.py's one-off
                # "unlock source links" charge. Unlike the two cases above,
                # there's no in-app view left to deliver the result to once
                # the webhook (rather than the user's own Verify tap) is
                # what completes the payment — so best-effort DM the paid-
                # for links directly via REST (this process has no live
                # gateway connection, same constraint as role_grant.py).
                completed = await db.complete_image_search_unlock_payment(reference)
                if completed:
                    logger.info(
                        f"[v0] image_search_unlock payment {reference} confirmed — "
                        f"user_id={completed['user_id']} clone_id={completed['clone_id']}"
                    )
                    from discord_bot.dm_send import dm_user
                    from config import DISCORD_BOT_TOKEN
                    bot_token = DISCORD_BOT_TOKEN
                    if completed['clone_id']:
                        clone = await db.get_discord_clone(int(completed['clone_id']))
                        if clone:
                            from utils.crypto import secret_manager
                            bot_token = secret_manager.decrypt(clone['bot_token_encrypted'])
                    links = completed.get('results') or []
                    lines = [f"{i}. {r.get('title') or 'Result'}: {r.get('url', '')}" for i, r in enumerate(links, 1)]
                    message = "✅ Payment confirmed — here are your source links:\n" + "\n".join(lines) if lines else \
                        "✅ Payment confirmed for your source-link unlock."
                    if bot_token:
                        sent = await dm_user(int(completed['user_id']), message, bot_token)
                        if not sent:
                            # Not fatal — the payment is durably marked
                            # 'completed' in the DB regardless, so tapping
                            # Verify again (handle_verify_unlock_button's
                            # DB fallback) still delivers the links.
                            logger.warning(
                                f"[v0] Could not DM image_search_unlock links to user {completed['user_id']} "
                                f"— payment is marked completed, they can still tap Verify again"
                            )
                else:
                    logger.warning(f"[v0] image_search_unlock payment {reference} confirmed but no matching pending row found")

        except Exception as e:
            logger.error(f"[v0] Error processing payment {reference}: {e}")
    
    def log_message(self, format, *args):
        """Suppress default HTTP server logging"""
        logger.debug(f"[v0] Webhook: {format % args}")


if __name__ == '__main__':
    from http.server import HTTPServer
    
    server = HTTPServer(('localhost', 3001), handler)
    print("[v0] Paystack webhook listening on http://localhost:3001")
    server.serve_forever()
