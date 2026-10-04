"""
Server Owners Panel — Phase 10 data helpers (Features hub + permission health check).

Bump status, Custom role perk, Autopost (bot self-promo), plus a read-only permission
health check and the "Fix next" jump on the Setup checklist. Same rules as
modules/server_panel.py: reads and writes go through the same db.* functions the slash
commands use (no second copy of the rules), every change writes one audit row, everything
is keyed by (guild_id, clone_id).

Not here on purpose: a server-wide default language (language is stored per user, and Bump
has its own language filter) and AI chat (per-user sessions and caps only, no per-server
settings). Both would need new schema, so they are not faked as panel screens.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from modules.server_panel import record_change

logger = logging.getLogger(__name__)

# Same bounds as /autopost setup (discord_bot/cogs/autopost.py).
AUTOPOST_MIN_HOURS = 1
AUTOPOST_MAX_HOURS = 720
AUTOPOST_PERMS = ("view_channel", "send_messages", "embed_links")

# Channels the panel can point at, as (setup label, permission set the bot needs there).
POST_PERMS = ("view_channel", "send_messages", "embed_links")

# Server-wide permissions, by the feature that needs them.
GUILD_NEEDS: List[Tuple[str, Tuple[str, ...]]] = [
    ("Moderation actions (kick, ban, timeout)", ("kick_members", "ban_members", "moderate_members")),
    ("Auto-roles, level roles, self-roles, custom roles", ("manage_roles",)),
    ("Invite tracker", ("manage_guild",)),
    ("Deleting messages (auto-mod, honeypot, Scam Shield)", ("manage_messages",)),
    ("Creating channels (tickets, setup)", ("manage_channels",)),
]


async def _audited(guild_id, clone_id, actor_id, prefix: str, old: dict, fields: dict) -> None:
    for k, v in fields.items():
        await record_change(guild_id, clone_id, actor_id, f"{prefix}.{k}", (old or {}).get(k), v)


async def _safe(coro, default):
    try:
        return await coro
    except Exception:
        logger.debug("[server-panel] read failed", exc_info=True)
        return default


# ── bump ─────────────────────────────────────────────────────────────────

async def bump_state(guild_id: int, clone_id: Optional[int]) -> Dict[str, Any]:
    """The bump network settings for this server ({} when never set up)."""
    from database import db
    cfg = await _safe(db.bump_get_guild_config(guild_id, clone_id=clone_id), None)
    return dict(cfg or {})


def bump_is_set_up(cfg: dict) -> bool:
    return bool(cfg) and bool(cfg.get("bump_channel_id"))


async def set_bump_receiving(guild_id: int, clone_id: Optional[int], actor_id: int, receives: bool) -> bool:
    """Pause or resume incoming bumps. False (nothing written) if bump was never set up,
    because writing would create a half-empty config row."""
    from database import db
    old = await db.bump_get_guild_config(guild_id, clone_id=clone_id)
    if not old:
        return False
    await db.bump_set_guild_config(guild_id, clone_id, actor_id, receives_bumps=bool(receives))
    await _audited(guild_id, clone_id, actor_id, "bump", old, {"receives_bumps": bool(receives)})
    return True


# ── custom role ──────────────────────────────────────────────────────────

async def custom_role_state(guild_id: int, clone_id: Optional[int]) -> Dict[str, Any]:
    from database import db
    disabled = await _safe(db.is_custom_role_feature_disabled(guild_id, clone_id=clone_id), False)
    panel = await _safe(db.get_custom_role_panel(guild_id, clone_id=clone_id), None)
    return {"disabled": bool(disabled), "panel": panel or {}}


async def set_custom_role_enabled(guild_id: int, clone_id: Optional[int], actor_id: int, enabled: bool) -> None:
    """Same switch as /customrole disable_feature (stored as 'disabled')."""
    from database import db
    old_disabled = await db.is_custom_role_feature_disabled(guild_id, clone_id=clone_id)
    await db.set_custom_role_feature_disabled(guild_id, not enabled, clone_id=clone_id)
    await _audited(guild_id, clone_id, actor_id, "custom_role",
                   {"disabled": bool(old_disabled)}, {"disabled": not enabled})


# ── autopost ─────────────────────────────────────────────────────────────

def validate_interval(text: Any) -> Tuple[Optional[int], Optional[str]]:
    msg = f"Hours must be a whole number from {AUTOPOST_MIN_HOURS} to {AUTOPOST_MAX_HOURS}."
    try:
        hours = int(str(text).strip())
    except (TypeError, ValueError):
        return None, msg
    if not AUTOPOST_MIN_HOURS <= hours <= AUTOPOST_MAX_HOURS:
        return None, msg
    return hours, None


async def autopost_state(guild_id: int, clone_id: Optional[int]) -> Dict[str, Any]:
    from database import db
    cfg = await _safe(db.get_discord_autopost(guild_id, clone_id), None)
    content = await _safe(db.list_discord_autopost_content(), [])
    return {"cfg": dict(cfg or {}), "content_count": len(content or [])}


async def set_autopost(guild_id: int, clone_id: Optional[int], actor_id: int,
                       channel_id: int, interval_hours: int) -> Optional[str]:
    """Turn autopost on (same rules as /autopost setup). Returns an error sentence, or None on success."""
    from database import db
    hours, err = validate_interval(interval_hours)
    if err:
        return err
    if not (await _safe(db.list_discord_autopost_content(), [])):
        return "There's no autopost content configured yet — nothing to rotate through."
    old = await _safe(db.get_discord_autopost(guild_id, clone_id), None) or {}
    await db.set_discord_autopost(guild_id, clone_id, channel_id, hours, actor_id)
    await _audited(guild_id, clone_id, actor_id, "autopost", old,
                   {"enabled": True, "channel_id": channel_id, "interval_hours": hours})
    return None


async def disable_autopost(guild_id: int, clone_id: Optional[int], actor_id: int) -> bool:
    from database import db
    old = await _safe(db.get_discord_autopost(guild_id, clone_id), None) or {}
    was_on = await db.disable_discord_autopost(guild_id, clone_id)
    if was_on:
        await _audited(guild_id, clone_id, actor_id, "autopost", old, {"enabled": False})
    return bool(was_on)


# ── permission health check (read-only) ──────────────────────────────────

def guild_gaps(me_perms) -> List[Tuple[str, List[str]]]:
    """[(feature, [missing permission labels])] for server-wide permissions the bot lacks."""
    from discord_bot import perm_check
    out = []
    for feature, perms in GUILD_NEEDS:
        miss = [perm_check.LABELS.get(p, p.replace("_", " ").title())
                for p in perms if not getattr(me_perms, p, False)]
        if miss:
            out.append((feature, miss))
    return out


async def channel_targets(guild_id: int, clone_id: Optional[int]) -> List[Tuple[str, int]]:
    """The channels the panel's features post in: [(label, channel_id)], de-duplicated per feature."""
    from database import db
    import asyncio
    welcome, automod, leveling, ticket, inv, auto, bump = await asyncio.gather(
        _safe(db.get_welcome_config(guild_id, clone_id), {}),
        _safe(db.get_automod_config(guild_id, clone_id), {}),
        _safe(db.get_leveling_config(guild_id, clone_id), {}),
        _safe(db.get_ticket_config(guild_id, clone_id), {}),
        _safe(db.get_invite_tracker_config(guild_id, clone_id=clone_id), {}),
        _safe(db.get_discord_autopost(guild_id, clone_id), {}),
        _safe(db.bump_get_guild_config(guild_id, clone_id=clone_id), {}),
    )
    pairs = [
        ("Welcome channel", (welcome or {}).get("channel_id")),
        ("Mod-log channel", (automod or {}).get("log_channel_id")),
        ("Level-up announcements", (leveling or {}).get("announce_channel_id")),
        ("Ticket panel", (ticket or {}).get("panel_channel_id")),
        ("Invite announcements", (inv or {}).get("channel_id")),
        ("Autopost", (auto or {}).get("channel_id") if (auto or {}).get("enabled") else None),
        ("Bump channel", (bump or {}).get("bump_channel_id")),
    ]
    return [(label, int(cid)) for label, cid in pairs if cid and int(cid) > 0]


async def health_report(guild, clone_id: Optional[int]) -> Dict[str, Any]:
    """{'guild': [(feature, [missing])], 'channels': [(label, channel_id, sentence_or_None)],
    'unreachable': [label]}. Read-only: nothing is changed."""
    from discord_bot import perm_check
    me = getattr(guild, "me", None)
    guild_part = guild_gaps(me.guild_permissions) if me is not None else []
    chans, unreachable = [], []
    for label, cid in await channel_targets(guild.id, clone_id):
        ch = guild.get_channel(cid)
        if ch is None:
            unreachable.append(label)
            continue
        miss = perm_check.missing_in_channel(ch, me, POST_PERMS)
        chans.append((label, cid, ", ".join(miss) if miss else None))
    return {"guild": guild_part, "channels": chans, "unreachable": unreachable}


def health_ok(report: dict) -> bool:
    return (not report.get("guild") and not report.get("unreachable")
            and not any(miss for _l, _c, miss in report.get("channels", [])))


# ── fix next (Setup checklist) ───────────────────────────────────────────

def next_incomplete(items) -> Optional[str]:
    """Key of the first checklist item that isn't done, in checklist order. None when all done."""
    for item in items or []:
        if not item.done:
            return item.key
    return None
