"""
Cron-triggered endpoint that auto-renews the per-user Yandex direct-search
subscription (config.IMAGE_SEARCH_YANDEX_FEE_GHS/month — see
handlers/image_search_handler.py) by charging the saved Paystack
authorization_code from each subscriber's first payment, no redirect or
manual "Verify Payment" tap required.

Run this once a day (same cadence as api/cron_expire_monetization.py). Any
row within 24h of expires_at gets charged:
  - success -> extends expires_at by IMAGE_SEARCH_YANDEX_DAYS from now
  - failure (declined/expired card, etc.) -> subscription drops to
    'expired' and the dead authorization_code is cleared so we don't keep
    retrying a card that won't work; the user has to resubscribe manually.

Auth: same pattern as the other api/cron_*.py endpoints — either header
"Authorization: Bearer <CRON_SECRET>" (what Vercel Cron sends automatically
when CRON_SECRET is set) or query param ?secret=<CRON_SECRET>.
"""
import json
import asyncio
import logging
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

from config import CRON_SECRET, IMAGE_SEARCH_YANDEX_DAYS, IMAGE_SEARCH_YANDEX_FEE_GHS
from database import db
from payments import paystack

logger = logging.getLogger(__name__)


async def renew_due_subscriptions() -> dict:
    due = await db.get_image_search_yandex_due_for_renewal()
    renewed, failed = [], []

    for row in due:
        user_id = row["user_id"]
        clone_id = row["clone_id"]
        authorization_code = row["authorization_code"]
        email = f"user_{user_id}@animebot.com"

        result = paystack.charge_authorization(
            authorization_code, email, IMAGE_SEARCH_YANDEX_FEE_GHS * 100,
            reference=f"YandexSearchRenew_{user_id}_{clone_id}"
        )

        if result and result.get("status") == "success":
            await db.activate_image_search_yandex_subscription(user_id, clone_id, days=IMAGE_SEARCH_YANDEX_DAYS)
            renewed.append({"user_id": user_id, "clone_id": clone_id})
        else:
            await db.mark_image_search_yandex_renewal_failed(user_id, clone_id)
            failed.append({"user_id": user_id, "clone_id": clone_id})

    return {"renewed": renewed, "failed": failed}



class handler(BaseHTTPRequestHandler):

    def _authorized(self) -> bool:
        if not CRON_SECRET:
            return False
        auth_header = self.headers.get("Authorization", "")
        if auth_header == f"Bearer {CRON_SECRET}":
            return True
        query = parse_qs(urlparse(self.path).query)
        return query.get("secret", [""])[0] == CRON_SECRET

    def do_GET(self):
        self._handle()

    def do_POST(self):
        self._handle()

    def _handle(self):
        if not self._authorized():
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "error", "message": "Unauthorized"}).encode())
            return

        try:
            result = asyncio.run(renew_due_subscriptions())
            # Rows whose renewal window passed with no saved card (one-time
            # subscribers who declined auto-renew) still need to lapse.
            asyncio.run(db.expire_image_search_yandex_subscriptions())
            logger.info(f"[v0] cron_renew_yandex_search renewed={len(result['renewed'])} failed={len(result['failed'])}")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "ok", **result}).encode())
        except Exception as e:
            logger.error(f"[v0] cron_renew_yandex_search error: {e}")
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "error", "message": str(e)}).encode())

    def log_message(self, format, *args):
        logger.debug(f"[v0] cron_renew_yandex_search: {format % args}")


if __name__ == '__main__':
    print(asyncio.run(renew_due_subscriptions()))
