"""Keeps Gumroad's sale-ping subscription pointed at the current API host.

Uses Gumroad's resource_subscriptions API (needs the token's view_sales scope). The Ping URL typed in Gumroad's
Settings page can't be changed by API; this subscription is a second, API-managed delivery of the same sale pings, and
duplicate pings are harmless (the second answers "already processed"). Never logs the secret or the token.
"""
import logging
from typing import Optional, Tuple

import aiohttp

import config
from modules.payment_watch import GUMROAD_API

logger = logging.getLogger(__name__)
PATH = "/api/gumroad_webhook"


def desired_url() -> Optional[str]:
    base = (getattr(config, "PUBLIC_BASE_URL", "") or "").rstrip("/")
    secret = getattr(config, "GUMROAD_WEBHOOK_SECRET", "") or ""
    if not base or not secret:
        return None
    return f"{base}{PATH}?secret={secret}"


async def sync_ping_subscription() -> Tuple[str, str]:
    """(status, text). status: ok | changed | skipped | failed. Text never contains the secret."""
    token = getattr(config, "GUMROAD_ACCESS_TOKEN", "") or ""
    want = desired_url()
    if not token:
        return "skipped", "GUMROAD_ACCESS_TOKEN isn't set."
    if not want:
        return "skipped", "PUBLIC_BASE_URL or GUMROAD_WEBHOOK_SECRET isn't set."
    auth = {"access_token": token}
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
            async with s.get(f"{GUMROAD_API}/resource_subscriptions", params=dict(auth, resource_name="sale")) as r:
                if r.status != 200:
                    return "failed", f"Gumroad refused the subscription list (HTTP {r.status}); the token needs the view_sales scope."
                data = await r.json(content_type=None)
            subs = [x for x in (data or {}).get("resource_subscriptions", []) if isinstance(x, dict)]
            if any(x.get("post_url") == want for x in subs):
                return "ok", "Ping subscription already points at the current host."
            for x in subs:  # stale ones from an older host
                if PATH in str(x.get("post_url", "")) and x.get("id"):
                    async with s.delete(f"{GUMROAD_API}/resource_subscriptions/{x['id']}", params=auth):
                        pass
            async with s.put(f"{GUMROAD_API}/resource_subscriptions", data=dict(auth, resource_name="sale", post_url=want)) as r:
                if r.status != 200:
                    return "failed", f"Gumroad refused the new subscription (HTTP {r.status})."
        return "changed", "Ping subscription now points at the current host."
    except Exception as e:
        logger.warning("[ping-sync] failed (%s)", type(e).__name__)
        return "failed", f"Couldn't reach Gumroad ({type(e).__name__})."
