# path: modules/admin_clones.py
"""Owner clone management that needs no live bot process.

Used by the web owner area (api/dash_owner_ops.py). Everything here is database writes plus Discord REST
calls made with the CLONE'S OWN token, so the web service can do it. The running clones are started and
stopped by discord_bot/clone_manager.py, which polls discord_cloned_bots.status every
POLL_INTERVAL_SECONDS, so no owner_jobs queue is needed for this.

Secrets: the bot token is only ever passed in, validated, encrypted and stored. It is never returned,
logged or put in an audit row, and list_clones() does not select the token column at all.
"""
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

STOP_NOTE = "The clone supervisor shuts the process down within about a minute."
START_NOTE = "The clone supervisor brings it online within about a minute."


def _db():
    from database import db
    return db


async def list_clones(limit: int = 100) -> List[Dict[str, Any]]:
    """Every clone, newest first. Never selects bot_token_encrypted or custom_data."""
    from database import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT clone_id, owner_id, bot_user_id, bot_username, application_id, status, "
            "last_heartbeat, created_at, parent_clone_id FROM discord_cloned_bots "
            "ORDER BY clone_id DESC LIMIT $1", limit)
    return [dict(r) for r in rows]


async def register_for_owner(token: str, owner_id: int) -> Dict[str, Any]:
    """Register a brand new clone for `owner_id`, free (owners are exempt from the activation fee).

    Mirrors Discord's rule that one owner has one clone row: if the owner already has one, this refuses
    and the caller should relink instead (keeps economy/leveling/premium history)."""
    from discord_clone_service import validate_bot_token, build_invite_url, set_default_install_params
    from utils.crypto import secret_manager
    db = _db()
    if await db.get_discord_clones_by_owner(owner_id):
        return {"ok": False, "error": "You already have a clone. Relink it to a new token instead, "
                                      "so its data carries over."}
    result = await validate_bot_token(token)
    if not result.get("ok"):
        return {"ok": False, "error": f"Couldn't validate that token: {result.get('error')}"}
    install = await set_default_install_params(token)
    if not install.get("ok"):
        logger.warning("owner clone register: couldn't set default install params for application %s",
                       result.get("application_id"))
    clone_id = await db.create_discord_clone(
        owner_id=owner_id, bot_token_encrypted=secret_manager.encrypt(token),
        bot_user_id=result["bot_user_id"], bot_username=result["bot_username"],
        application_id=result["application_id"], parent_clone_id=None)
    return {"ok": True, "clone_id": clone_id, "bot_username": result["bot_username"],
            "invite_url": build_invite_url(result["application_id"]), "note": START_NOTE}


async def relink(clone_id: int, owner_id: int, token: str) -> Dict[str, Any]:
    """Point an existing clone the caller owns at a new bot token; all its data stays."""
    from discord_clone_service import validate_bot_token, build_invite_url, set_default_install_params
    from utils.crypto import secret_manager
    db = _db()
    row = await db.get_discord_clone(clone_id)
    if not row or row.get("owner_id") != owner_id:
        return {"ok": False, "error": "You don't own a clone with that id."}
    result = await validate_bot_token(token)
    if not result.get("ok"):
        return {"ok": False, "error": f"Couldn't validate that token: {result.get('error')}"}
    install = await set_default_install_params(token)
    if not install.get("ok"):
        logger.warning("owner clone relink: couldn't set default install params for application %s",
                       result.get("application_id"))
    ok = await db.relink_discord_clone(
        clone_id=clone_id, owner_id=owner_id, bot_token_encrypted=secret_manager.encrypt(token),
        bot_user_id=result["bot_user_id"], bot_username=result["bot_username"],
        application_id=result["application_id"])
    if not ok:
        return {"ok": False, "error": "You don't own a clone with that id."}
    return {"ok": True, "clone_id": clone_id, "bot_username": result["bot_username"],
            "invite_url": build_invite_url(result["application_id"]), "note": START_NOTE}


async def stop(clone_id: int) -> Optional[str]:
    """Mark a clone inactive. Returns None on success or a short reason it was not done."""
    db = _db()
    row = await db.get_discord_clone(clone_id)
    if not row:
        return "No clone with that id."
    if row.get("status") != "active":
        return "That clone is already stopped."
    await db.set_discord_clone_status(clone_id, "inactive")
    return None
