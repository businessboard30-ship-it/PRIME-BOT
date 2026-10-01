"""
Server Owners Panel — data helpers (Phase 1). See SERVER_PANEL_PLAN.md.

Everything here is keyed by (guild_id, clone_id) so a clone's panel never reads
or writes the main bot's settings. The UI lives in
discord_bot/cogs/_views_server_panel.py and stays a thin shell: reads and
writes go through the same `db.get_*/set_*_config` functions the slash commands
use, so there is no second copy of the rules.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

HISTORY_LIMIT = 15


# ── access ───────────────────────────────────────────────────────────────

def access_denied_reason(guild, user_id: int, permissions=None) -> Optional[str]:
    """None when the user may use this server's panel, else a short reason.

    Allowed: the server owner, or anyone with Manage Server (decision 2).
    `permissions` is interaction.permissions (always populated inside a guild
    channel, even for user-installed apps); falls back to the member object.
    Re-run on every click and every modal submit. The global
    too-many-privileged-members lockout from _perm_guard still applies.
    """
    if guild is None:
        return "The panel only works inside a server."
    is_owner = getattr(guild, "owner_id", None) == user_id
    can_manage = bool(getattr(permissions, "manage_guild", False)) or bool(
        getattr(permissions, "administrator", False))
    if not can_manage and not is_owner:
        member = guild.get_member(user_id) if hasattr(guild, "get_member") else None
        gp = getattr(member, "guild_permissions", None)
        can_manage = bool(getattr(gp, "manage_guild", False)) or bool(getattr(gp, "administrator", False))
    if not (is_owner or can_manage):
        return "You need the **Manage Server** permission to use the panel."
    try:
        from discord_bot.cogs import _perm_guard
        count = _perm_guard.get_privileged_count(guild)
        if count > _perm_guard.MAX_PRIVILEGED_MEMBERS:
            return _perm_guard.GUARD_MESSAGE.format(limit=_perm_guard.MAX_PRIVILEGED_MEMBERS, count=count)
    except Exception:
        logger.debug("[server-panel] perm guard check skipped", exc_info=True)
    return None


# ── audit ────────────────────────────────────────────────────────────────

def _short(v: Any) -> Optional[str]:
    return None if v is None else str(v)[:200]


async def record_change(guild_id: int, clone_id: Optional[int], actor_id: int,
                        key: str, old: Any = None, new: Any = None) -> None:
    """One log line plus one row in server_panel_audit. Best effort: a failed
    DB write never hides the real result of the action."""
    logger.info("[server-panel-audit] guild=%s clone=%s actor=%s key=%s old=%r new=%r",
                guild_id, clone_id, actor_id, key, _short(old), _short(new))
    try:
        from database import get_pool
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO server_panel_audit (guild_id, clone_id, actor_id, setting_key, old_value, new_value) "
                "VALUES ($1, $2, $3, $4, $5, $6)",
                guild_id, clone_id, actor_id, key[:100], _short(old), _short(new))
    except Exception:
        logger.debug("[server-panel] couldn't persist audit row", exc_info=True)


async def recent_changes(guild_id: int, clone_id: Optional[int], limit: int = HISTORY_LIMIT) -> List[dict]:
    from database import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT actor_id, setting_key, old_value, new_value, created_at FROM server_panel_audit "
            "WHERE guild_id = $1 AND clone_id IS NOT DISTINCT FROM $2 "
            "ORDER BY created_at DESC, id DESC LIMIT $3", guild_id, clone_id, limit)
    return [dict(r) for r in rows]


# ── setup checklist ──────────────────────────────────────────────────────

@dataclass
class SetupItem:
    key: str
    label: str
    done: bool
    fix_label: str = "Set up"


SETUP_KEYS = ("welcome", "verification", "automod", "modlog", "leveling", "tickets", "channels", "premium")


async def _safe(coro, default):
    try:
        return await coro
    except Exception:
        logger.debug("[server-panel] read failed", exc_info=True)
        return default


async def setup_items(guild, clone_id: Optional[int]) -> List[SetupItem]:
    """The eight-item Setup checklist (plan section 5). Each read is isolated,
    so one failing table marks that item To-do instead of breaking the screen."""
    from database import db
    gid = guild.id
    welcome = await _safe(db.get_welcome_config(gid, clone_id), {})
    verif = await _safe(db.get_verification_config(gid, clone_id), {})
    honey = await _safe(db.get_honeypot_config(gid, clone_id), {})
    automod = await _safe(db.get_automod_config(gid, clone_id), {})
    leveling = await _safe(db.get_leveling_config(gid, clone_id), {})
    ticket = await _safe(db.get_ticket_config(gid, clone_id), {})
    premium = await _safe(db.is_guild_premium_active(gid, clone_id), False)
    missing = None
    try:
        from discord_bot.cogs.setup_channels import scan_missing_channels
        missing = await scan_missing_channels(guild, clone_id)
    except Exception:
        logger.debug("[server-panel] missing-channel scan failed", exc_info=True)

    automod_on = any(automod.get(k) for k in ("word_filter_enabled", "anti_invite_enabled",
                                               "anti_mention_enabled", "spam_enabled"))
    return [
        SetupItem("welcome", "Welcome message and card",
                  bool(welcome.get("enabled") and welcome.get("channel_id"))),
        SetupItem("verification", "Verification or honeypot",
                  bool(verif.get("enabled")) or bool(honey.get("channel_id") and honey.get("enabled"))),
        SetupItem("automod", "Auto-moderation", automod_on),
        SetupItem("modlog", "Mod-log channel", bool(automod.get("log_channel_id"))),
        # Leveling has no on/off flag (always listening); "done" means the
        # owner picked a level-up announcement channel.
        SetupItem("leveling", "Leveling", bool(leveling.get("announce_channel_id"))),
        SetupItem("tickets", "Ticket or support channel",
                  bool(ticket.get("panel_channel_id") or ticket.get("category_id"))),
        SetupItem("channels", "Missing channels", missing is not None and not missing, "Create"),
        SetupItem("premium", "Premium decision", bool(premium), "See Premium"),
    ]


def setup_score(items: List[SetupItem]) -> tuple[int, int]:
    return sum(1 for i in items if i.done), len(items)


# ── status ───────────────────────────────────────────────────────────────

async def premium_status(guild_id: int, clone_id: Optional[int]) -> dict:
    """{'active': bool, 'expires_at': datetime|None} from the existing premium check."""
    from database import db
    active = await _safe(db.is_guild_premium_active(guild_id, clone_id), False)
    row = await _safe(db.get_guild_premium(guild_id, clone_id), None)
    return {"active": bool(active), "expires_at": (row or {}).get("expires_at")}


# ── writes (thin wrappers that also audit) ───────────────────────────────

async def set_welcome(guild_id: int, clone_id: Optional[int], actor_id: int, **fields) -> None:
    from database import db
    old = await db.get_welcome_config(guild_id, clone_id)
    await db.set_welcome_config(guild_id, clone_id=clone_id, **fields)
    for k, v in fields.items():
        await record_change(guild_id, clone_id, actor_id, f"welcome.{k}", old.get(k), v)


async def set_automod(guild_id: int, clone_id: Optional[int], actor_id: int, **fields) -> None:
    from database import db
    old = await db.get_automod_config(guild_id, clone_id)
    await db.set_automod_config(guild_id, clone_id=clone_id, **fields)
    for k, v in fields.items():
        await record_change(guild_id, clone_id, actor_id, f"automod.{k}", old.get(k), v)


async def set_verification(guild_id: int, clone_id: Optional[int], actor_id: int, **fields) -> None:
    from database import db
    old = await db.get_verification_config(guild_id, clone_id)
    await db.set_verification_config(guild_id, clone_id=clone_id, **fields)
    for k, v in fields.items():
        await record_change(guild_id, clone_id, actor_id, f"verification.{k}", old.get(k), v)


async def set_honeypot(guild_id: int, clone_id: Optional[int], actor_id: int, **fields) -> None:
    from database import db
    old = await db.get_honeypot_config(guild_id, clone_id)
    await db.set_honeypot_config(guild_id, clone_id=clone_id, **fields)
    for k, v in fields.items():
        await record_change(guild_id, clone_id, actor_id, f"honeypot.{k}", old.get(k), v)


# ── Phase 2 writes: community, tickets, logs ─────────────────────────────

MODLOG_CATEGORY_FIELDS = {
    "server": "log_server_enabled", "channels": "log_channels_enabled",
    "roles": "log_roles_enabled", "members": "log_members_enabled",
    "moderation": "log_moderation_enabled", "voice": "log_voice_enabled",
    "invites": "log_invites_enabled",
}
XP_RATES = ("slow", "default", "fast")
STAR_THRESHOLDS = (1, 2, 3, 5, 7, 10, 15, 20, 25)


async def _audited(guild_id, clone_id, actor_id, prefix, old: dict, fields: dict) -> None:
    for k, v in fields.items():
        await record_change(guild_id, clone_id, actor_id, f"{prefix}.{k}", (old or {}).get(k), v)


async def set_leveling(guild_id, clone_id, actor_id, **fields) -> None:
    from database import db
    old = await db.get_leveling_config(guild_id, clone_id)
    await db.set_leveling_config(guild_id, clone_id=clone_id, **fields)
    await _audited(guild_id, clone_id, actor_id, "leveling", old, fields)


async def set_voice_xp(guild_id, clone_id, actor_id, **fields) -> None:
    from database import db
    old = await db.get_voice_xp_config(guild_id, clone_id)
    await db.set_voice_xp_config(guild_id, clone_id=clone_id, **fields)
    await _audited(guild_id, clone_id, actor_id, "voice_xp", old, fields)


async def set_starboard(guild_id, clone_id, actor_id, **fields) -> None:
    from database import db
    old = await db.get_starboard_config(guild_id, clone_id)
    await db.set_starboard_config(guild_id, clone_id=clone_id, **fields)
    await _audited(guild_id, clone_id, actor_id, "starboard", old, fields)


async def set_suggestions(guild_id, clone_id, actor_id, approved_log_channel_id) -> None:
    from database import db
    old = await db.get_suggestion_config(guild_id, clone_id)
    await db.set_suggestion_config(guild_id, clone_id=clone_id, approved_log_channel_id=approved_log_channel_id)
    await _audited(guild_id, clone_id, actor_id, "suggestions", old,
                   {"approved_log_channel_id": approved_log_channel_id})


async def set_tickets(guild_id, clone_id, actor_id, **fields) -> None:
    from database import db
    old = await db.get_ticket_config(guild_id, clone_id)
    await db.set_ticket_config(guild_id, clone_id=clone_id, **fields)
    await _audited(guild_id, clone_id, actor_id, "tickets", old, fields)


def role_blocked_reason(guild, role) -> Optional[str]:
    """Why a role can't be handed out as a reward, or None. Mirrors /levelrole add."""
    if role is None:
        return "Pick a role first."
    if getattr(role, "is_default", lambda: False)() or getattr(role, "managed", False):
        return "That role can't be assigned (everyone/bot-managed roles are not allowed)."
    me = getattr(guild, "me", None)
    if me is not None and role >= me.top_role:
        return "That role is above (or equal to) my own top role — move my role above it first."
    return None


async def add_level_role(guild_id, clone_id, actor_id, level: int, role_id: int) -> bool:
    from database import db
    ok = await db.add_level_role(guild_id, level, role_id, clone_id=clone_id)
    if ok:
        await record_change(guild_id, clone_id, actor_id, f"level_role.{level}", None, role_id)
    return bool(ok)


async def remove_level_role(guild_id, clone_id, actor_id, level: int) -> bool:
    from database import db
    roles = await db.get_level_roles(guild_id, clone_id)
    old = next((r["role_id"] for r in roles if r["level"] == level), None)
    ok = await db.remove_level_role(guild_id, level, clone_id=clone_id)
    if ok:
        await record_change(guild_id, clone_id, actor_id, f"level_role.{level}", old, None)
    return bool(ok)


async def set_modlog_categories(guild_id, clone_id, actor_id, selected: set) -> None:
    """Turn exactly `selected` categories on and the rest off, in one write."""
    fields = {col: (cat in selected) for cat, col in MODLOG_CATEGORY_FIELDS.items()}
    await set_automod(guild_id, clone_id, actor_id, **fields)
