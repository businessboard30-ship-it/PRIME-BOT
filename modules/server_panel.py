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
    import asyncio
    gid = guild.id

    async def _scan():
        try:
            from discord_bot.cogs.setup_channels import scan_missing_channels
            return await scan_missing_channels(guild, clone_id)
        except Exception:
            logger.debug("[server-panel] missing-channel scan failed", exc_info=True)
            return None

    # All reads are independent, so they run at the same time: total wait is
    # the slowest single read, not the sum of all of them.
    welcome, verif, honey, automod, leveling, ticket, premium, missing = await asyncio.gather(
        _safe(db.get_welcome_config(gid, clone_id), {}),
        _safe(db.get_verification_config(gid, clone_id), {}),
        _safe(db.get_honeypot_config(gid, clone_id), {}),
        _safe(db.get_automod_config(gid, clone_id), {}),
        _safe(db.get_leveling_config(gid, clone_id), {}),
        _safe(db.get_ticket_config(gid, clone_id), {}),
        _safe(db.is_guild_premium_active(gid, clone_id), False),
        _scan(),
    )

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
    import asyncio
    active, row = await asyncio.gather(
        _safe(db.is_guild_premium_active(guild_id, clone_id), False),
        _safe(db.get_guild_premium(guild_id, clone_id), None),
    )
    return {"active": bool(active), "expires_at": (row or {}).get("expires_at")}


# ── writes (thin wrappers that also audit) ───────────────────────────────

async def set_welcome(guild_id: int, clone_id: Optional[int], actor_id: int, **fields) -> None:
    from database import db
    old = await db.get_welcome_config(guild_id, clone_id)
    await db.set_welcome_config(guild_id, clone_id=clone_id, **fields)
    for k, v in fields.items():
        await record_change(guild_id, clone_id, actor_id, f"welcome.{k}", old.get(k), v)


async def set_welcome_extras(guild_id: int, clone_id: Optional[int], actor_id: int, **fields) -> None:
    """Goodbye message and auto-roles (discord_welcome_extras), audited like every panel write."""
    from database import db
    old = await db.get_welcome_extras(guild_id, clone_id)
    await db.set_welcome_extras(guild_id, clone_id=clone_id, **fields)
    for k, v in fields.items():
        await record_change(guild_id, clone_id, actor_id, f"welcome_extras.{k}", old.get(k), v)


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


def _drop_honeypot_cache(guild_id: int, clone_id: Optional[int]) -> None:
    """The honeypot listener caches each server's settings for 60s. Any write made
    here must clear that entry, or an on/off toggle (or reset) keeps being ignored
    until the cache expires. Best effort: never blocks the write."""
    try:
        from discord_bot.cogs.honeypot import _invalidate
        _invalidate(guild_id, clone_id)
    except Exception:
        logger.debug("[server-panel] couldn't clear honeypot cache", exc_info=True)


async def set_honeypot(guild_id: int, clone_id: Optional[int], actor_id: int, **fields) -> None:
    from database import db
    old = await db.get_honeypot_config(guild_id, clone_id)
    await db.set_honeypot_config(guild_id, clone_id=clone_id, **fields)
    _drop_honeypot_cache(guild_id, clone_id)
    for k, v in fields.items():
        await record_change(guild_id, clone_id, actor_id, f"honeypot.{k}", old.get(k), v)


async def set_antiraid(guild_id: int, clone_id: Optional[int], actor_id: int, **fields) -> None:
    from database import db
    old = await db.get_antiraid_config(guild_id, clone_id)
    await db.set_antiraid_config(guild_id, clone_id=clone_id, **fields)
    for k, v in fields.items():
        await record_change(guild_id, clone_id, actor_id, f"antiraid.{k}", old.get(k), v)


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


# ── Phase 3: stats, history, reset, copy settings, reports ───────────────

async def server_stats(guild, clone_id: Optional[int]) -> dict:
    """Same numbers as /serveranalytics (reuses its age formatter and growth
    tips), plus the setup score. Each read is isolated."""
    from database import db
    import asyncio
    active, items = await asyncio.gather(
        _safe(db.count_active_members(guild.id, days=7, clone_id=clone_id), None),
        setup_items(guild, clone_id),
    )
    done, total = setup_score(items)
    age, tips = "N/A", []
    try:
        from discord_bot.cogs.analytics import AnalyticsCog, GROWTH_TIPS
        age = AnalyticsCog._age_str(guild.created_at)
        tips = [f"{emoji} {name}" + (f" — `{cmd}`" if cmd else "") for emoji, name, cmd, _ in GROWTH_TIPS[:3]]
    except Exception:
        logger.debug("[server-panel] analytics helpers unavailable", exc_info=True)
    channels = list(getattr(guild, "channels", []) or [])
    return {
        "members": getattr(guild, "member_count", None), "active_7d": active, "age": age,
        "text": sum(1 for c in channels if getattr(c, "type", None) == _CT_TEXT),
        "voice": sum(1 for c in channels if getattr(c, "type", None) == _CT_VOICE),
        "roles": max(len(getattr(guild, "roles", []) or []) - 1, 0),
        "setup_done": done, "setup_total": total, "tips": tips,
    }


def _channel_types():
    import discord
    return discord.ChannelType.text, discord.ChannelType.voice


_CT_TEXT, _CT_VOICE = _channel_types()


def _clip(v: Any, n: int = 30) -> str:
    s = "∅" if v is None else str(v).replace("`", "'").replace("\n", " ")
    return s if len(s) <= n else s[: n - 1] + "…"


def format_history(rows: List[dict]) -> List[str]:
    """One short line per audit row, newest first."""
    out = []
    for r in rows:
        ts = r.get("created_at")
        when = f"<t:{int(ts.timestamp())}:R>" if ts else "?"
        out.append(f"{when} <@{r.get('actor_id')}> `{_clip(r.get('setting_key'), 40)}`: "
                   f"{_clip(r.get('old_value'))} → {_clip(r.get('new_value'))}")
    return out


# -- reset to defaults ----------------------------------------------------

RESET_GROUPS = {
    "welcome": "Welcome", "verification": "Verification", "automod": "Auto-mod filters",
    "modlog": "Mod-log categories", "leveling": "Leveling and voice XP", "starboard": "Starboard",
    "suggestions": "Suggestions", "tickets": "Tickets", "honeypot": "Honeypot",
    "antiraid": "Anti-raid",
}
RESET_NOTES = {
    "leveling": "Level-role rewards are kept (remove them in Community).",
    "tickets": "A ticket panel already posted stays in its channel.",
    "verification": "A gate message already posted stays in its channel.",
    "honeypot": "The honeypot configuration is deleted; a posted warning message stays.",
    "antiraid": "Anti-raid is switched off and its settings deleted; an active lockdown is ended first.",
}
_WELCOME_DEFAULTS = {
    "enabled": False, "channel_id": None, "card_style": "gif", "avatar_shape": "circle",
    "delivery_mode": "channel", "message_template": "Welcome {member} to {guild}! You are member #{count}.",
}
_WELCOME_EXTRAS_DEFAULTS = {
    "goodbye_enabled": False, "goodbye_channel_id": None,
    "goodbye_message": "{name} has left {guild}. We are now {count} members.",
    "member_role_id": None, "bot_role_id": None,
}
_VERIFICATION_DEFAULTS = {
    "enabled": False, "mode": "button", "channel_id": None, "unverified_role_id": None,
    "verified_role_id": None, "timeout_seconds": 300, "max_attempts": 3,
}
_AUTOMOD_RESET = {
    "action": "delete", "timeout_minutes": 10, "log_channel_id": None, "word_filter_enabled": False,
    "banned_words": [], "anti_invite_enabled": False, "anti_mention_enabled": False,
    "anti_mention_threshold": 5, "spam_enabled": False, "spam_flood_threshold": 10,
    "spam_flood_window_seconds": 10, "min_account_age_hours": 0,
}
_LEVELING_DEFAULTS = {
    "announce_channel_id": None, "xp_rate": "default", "card_style": "card",
    "leaderboard_autopost_channel_id": None,
}
_VOICE_XP_DEFAULTS = {"enabled": True, "xp_per_minute": 10, "afk_channel_excluded": True}
_STARBOARD_DEFAULTS = {"channel_id": None, "threshold": 5, "emoji": "⭐"}
_TICKET_DEFAULTS = {"support_role_id": None, "category_id": None, "panel_channel_id": None}


async def reset_feature(guild_id: int, clone_id: Optional[int], actor_id: int, key: str, bot=None) -> None:
    """Put one feature's panel-visible settings back to defaults. Goes through
    the audited setters, so every field change is logged, plus one summary line."""
    if key not in RESET_GROUPS:
        raise ValueError(f"unknown feature {key!r}")
    g, c, a = guild_id, clone_id, actor_id
    if key == "welcome":
        await set_welcome(g, c, a, **_WELCOME_DEFAULTS)
        await set_welcome_extras(g, c, a, **_WELCOME_EXTRAS_DEFAULTS)
    elif key == "verification":
        await set_verification(g, c, a, **_VERIFICATION_DEFAULTS)
    elif key == "automod":
        await set_automod(g, c, a, **_AUTOMOD_RESET)
    elif key == "modlog":
        await set_modlog_categories(g, c, a, set())
    elif key == "leveling":
        await set_leveling(g, c, a, **_LEVELING_DEFAULTS)
        await set_voice_xp(g, c, a, **_VOICE_XP_DEFAULTS)
    elif key == "starboard":
        await set_starboard(g, c, a, **_STARBOARD_DEFAULTS)
    elif key == "suggestions":
        await set_suggestions(g, c, a, None)
    elif key == "tickets":
        await set_tickets(g, c, a, **_TICKET_DEFAULTS)
    elif key == "honeypot":
        from database import db
        old = await db.get_honeypot_config(g, c)
        await db.delete_honeypot_config(g, clone_id=c)
        _drop_honeypot_cache(g, c)
        await _audited(g, c, a, "honeypot", old, {k: None for k in ("channel_id", "enabled")})
    elif key == "antiraid":
        from database import db
        old = await db.get_antiraid_config(g, c)
        if old.get("active_until"):   # never leave a server locked down with no settings left to end it
            live = bot.get_guild(g) if bot is not None else None
            if live is not None:
                from discord_bot.cogs import antiraid
                await antiraid.end_raid(bot, live)
        await db.delete_antiraid_config(g, clone_id=c)
        await _audited(g, c, a, "antiraid", old, {k: None for k in ("enabled", "log_channel_id")})
    await record_change(g, c, a, f"reset.{key}", None, "defaults")


# -- copy settings between servers ---------------------------------------

COPY_FEATURES = ("automod", "welcome", "leveling", "starboard")
COPY_EXCLUDED = ("verification, tickets, honeypot, suggestions and reaction roles "
                 "(they depend on messages already posted in the other server)")
_AUTOMOD_COPY = ("action", "timeout_minutes", "word_filter_enabled", "banned_words", "anti_invite_enabled",
                 "anti_mention_enabled", "anti_mention_threshold", "spam_enabled", "spam_flood_threshold",
                 "spam_flood_window_seconds", "min_account_age_hours")


def map_channel(src_guild, dst_guild, channel_id) -> Optional[int]:
    """Channel in dst with the same name and type as src's, or None. IDs are
    never copied across servers."""
    ch = src_guild.get_channel(channel_id) if channel_id else None
    if ch is None:
        return None
    for c in getattr(dst_guild, "channels", []) or []:
        if c.name == ch.name and getattr(c, "type", None) == getattr(ch, "type", None):
            return c.id
    return None


def map_role(src_guild, dst_guild, role_id) -> Optional[int]:
    """Assignable role in dst with the same name as src's, or None."""
    role = src_guild.get_role(role_id) if role_id else None
    if role is None:
        return None
    for r in getattr(dst_guild, "roles", []) or []:
        if r.name == role.name and role_blocked_reason(dst_guild, r) is None:
            return r.id
    return None


async def manages_guild(guild, user_id: int, permissions=None) -> bool:
    """Live owner/Manage Server check (same rule as the panel itself). When the
    member isn't cached, fetches them rather than guessing."""
    if guild is None:
        return False
    if access_denied_reason(guild, user_id, permissions) is None:
        return True
    if permissions is None and guild.get_member(user_id) is None:
        try:
            member = await guild.fetch_member(user_id)
        except Exception:
            return False
        return access_denied_reason(guild, user_id, member.guild_permissions) is None
    return False


def copyable_guilds(bot, current_guild, user_id: int, limit: int = 25) -> List[tuple]:
    """(id, name) of other servers the bot is in where the user is owner or
    Manage Server, from cached data only. Re-verified live on confirm."""
    out = []
    for g in getattr(bot, "guilds", []) or []:
        if g.id != current_guild.id and access_denied_reason(g, user_id) is None:
            out.append((g.id, g.name))
    return sorted(out, key=lambda t: t[1].lower())[:limit]


@dataclass
class CopyReport:
    copied: List[str]
    repick: List[str]


async def copy_settings(src, dst, clone_id: Optional[int], actor_id: int,
                        features=COPY_FEATURES) -> CopyReport:
    """Copy plain settings from src to dst (both guild objects). Channels and
    roles are matched by name; anything with no match is left as it is in dst
    and listed in `repick` for the owner to set. Each feature is isolated."""
    from database import db
    rep = CopyReport([], [])
    g, c, a = dst.id, clone_id, actor_id

    async def chan(label, cid):
        m = map_channel(src, dst, cid)
        if cid and m is None:
            rep.repick.append(f"{label}: no channel with the same name")
        return m

    for feat in features:
        try:
            if feat == "automod":
                cfg = await db.get_automod_config(src.id, clone_id)
                await set_automod(g, c, a, **{k: cfg.get(k) for k in _AUTOMOD_COPY if k in cfg})
                await set_modlog_categories(g, c, a, {cat for cat, col in MODLOG_CATEGORY_FIELDS.items() if cfg.get(col)})
                m = await chan("Mod-log channel", cfg.get("log_channel_id"))
                if m:
                    await set_automod(g, c, a, log_channel_id=m)
            elif feat == "welcome":
                cfg = await db.get_welcome_config(src.id, clone_id)
                await set_welcome(g, c, a, **{k: cfg.get(k) for k in
                                              ("message_template", "card_style", "avatar_shape", "delivery_mode") if cfg.get(k)})
                m = await chan("Welcome channel", cfg.get("channel_id"))
                if m:
                    await set_welcome(g, c, a, channel_id=m, enabled=bool(cfg.get("enabled")))
                elif cfg.get("enabled"):
                    rep.repick.append("Welcome: left as it was (pick a channel, then turn it on)")
            elif feat == "leveling":
                cfg = await db.get_leveling_config(src.id, clone_id)
                await set_leveling(g, c, a, xp_rate=cfg.get("xp_rate", "default"), card_style=cfg.get("card_style", "card"))
                vx = await db.get_voice_xp_config(src.id, clone_id)
                await set_voice_xp(g, c, a, enabled=vx.get("enabled"), xp_per_minute=vx.get("xp_per_minute"),
                                   afk_channel_excluded=vx.get("afk_channel_excluded"))
                for label, field in (("Level-up channel", "announce_channel_id"),
                                     ("Leaderboard channel", "leaderboard_autopost_channel_id")):
                    m = await chan(label, cfg.get(field))
                    if m:
                        await set_leveling(g, c, a, **{field: m})
                for lr in await db.get_level_roles(src.id, clone_id):
                    rid = map_role(src, dst, lr["role_id"])
                    if rid is None:
                        rep.repick.append(f"Level {lr['level']} reward: no matching assignable role")
                    else:
                        await add_level_role(g, c, a, lr["level"], rid)
            elif feat == "starboard":
                cfg = await db.get_starboard_config(src.id, clone_id)
                await set_starboard(g, c, a, threshold=cfg.get("threshold"), emoji=cfg.get("emoji"))
                m = await chan("Starboard channel", cfg.get("channel_id"))
                if m:
                    await set_starboard(g, c, a, channel_id=m)
            rep.copied.append(RESET_GROUPS.get(feat, feat))
        except Exception:
            logger.exception("[server-panel] copy of %s failed", feat)
            rep.repick.append(f"{feat}: copy failed, nothing more was changed for it")
    await record_change(g, c, a, "copy_settings", src.id, ",".join(rep.copied))
    return rep


# -- help / report --------------------------------------------------------

REPORT_KINDS = {"problem": "Problem report", "idea": "Feature idea"}
REPORT_MAX = 900  # feedback limit is 1000 and the tag below stays under 40


def format_report(kind: str, text: str) -> str:
    return f"[Server panel · {REPORT_KINDS.get(kind, 'Feedback')}] {text.strip()}"


# ── Phase 4: kill switches, usage tracking ───────────────────────────────

async def engaged_features(user_id: int) -> set:
    """Owner kill-switch keys that are engaged right now, for this person.
    Bot owners are never locked out (same rule as the slash-command check).
    Fails open: any problem reading them means nothing is locked."""
    try:
        from config import DISCORD_CLONE_ADMIN_IDS
        if user_id in DISCORD_CLONE_ADMIN_IDS:
            return set()
    except Exception:
        logger.debug("[server-panel] owner list unavailable", exc_info=True)
    try:
        from modules import admin_controls as ac
        return set(await ac.current_switches())
    except Exception:
        logger.debug("[server-panel] kill switches unreadable; nothing locked", exc_info=True)
        return set()


async def record_open(guild_id: int, clone_id: Optional[int]) -> None:
    """Count one panel open for this server. Best effort, never raises."""
    try:
        from database import get_pool
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO server_panel_usage (guild_id, clone_id) VALUES ($1, $2) "
                "ON CONFLICT (guild_id, (COALESCE(clone_id, -1))) DO UPDATE "
                "SET last_opened_at = NOW(), opens = server_panel_usage.opens + 1",
                guild_id, clone_id)
    except Exception:
        logger.debug("[server-panel] couldn't record panel open", exc_info=True)


async def usage_stats() -> Optional[dict]:
    """{'opened': servers that ever opened the panel, 'week': ... in the last 7
    days, 'changed': servers that changed a setting from it}, or None when it
    can't be read. Report/feedback rows don't count as changes."""
    import asyncio

    async def query() -> dict:
        from database import get_pool
        pool = await get_pool()
        async with pool.acquire() as conn:
            u = await conn.fetchrow(
                "SELECT COUNT(*) AS opened, "
                "COUNT(*) FILTER (WHERE last_opened_at > NOW() - INTERVAL '7 days') AS week "
                "FROM server_panel_usage")
            c = await conn.fetchval(
                "SELECT COUNT(*) FROM (SELECT DISTINCT guild_id, clone_id FROM server_panel_audit "
                "WHERE setting_key NOT LIKE 'report.%') t")
        return {"opened": int(u["opened"]), "week": int(u["week"]), "changed": int(c or 0)}

    try:
        return await asyncio.wait_for(query(), timeout=5)
    except Exception:
        logger.debug("[server-panel] usage stats unavailable", exc_info=True)
        return None
