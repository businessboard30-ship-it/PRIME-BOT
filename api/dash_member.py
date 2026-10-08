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


ROUTES = {"member_status": member_status}
