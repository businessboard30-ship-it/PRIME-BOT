# path: utils/dash_review.py

"""
Pure helpers for the dashboard's anti-raid review screen (no I/O, unit-tested).

The in-Discord review uses live discord.py objects; the dashboard runs in the web process and only has
Discord's REST API, so the same rules are re-expressed over plain ids and dicts here.
"""

from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Tuple

DISCORD_EPOCH_MS = 1420070400000
QUARANTINE_REASON_PREFIX = "[anti-raid]"      # same marker modules/antiraid_pro.py writes
ADMINISTRATOR = 0x8
KICK_MEMBERS = 0x2
BAN_MEMBERS = 0x4
DECISIONS = {"release": None, "kick": KICK_MEMBERS, "ban": BAN_MEMBERS}   # decision -> extra permission needed
BAN_DELETE_SECONDS = 3600


def snowflake_created(user_id) -> datetime:
    return datetime.fromtimestamp(((int(user_id) >> 22) + DISCORD_EPOCH_MS) / 1000, tz=timezone.utc)


def is_raid_row(row: dict) -> bool:
    return str(row.get("reason") or "").startswith(QUARANTINE_REASON_PREFIX)


def clean_reason(reason: Optional[str]) -> str:
    r = str(reason or "")
    if r.startswith(QUARANTINE_REASON_PREFIX):
        r = r[len(QUARANTINE_REASON_PREFIX):]
    return r.strip()[:200]


def has_permission(owner_id: Optional[int], user_id: int, member_role_ids: Iterable[int],
                   roles_by_id: Dict[int, int], guild_id: int, bit: int) -> bool:
    """Owner, Administrator, or the given permission bit, computed from role permissions."""
    if owner_id is not None and int(owner_id) == int(user_id):
        return True
    perms = int(roles_by_id.get(int(guild_id), 0))
    for rid in member_role_ids:
        perms |= int(roles_by_id.get(int(rid), 0))
    return bool(perms & (ADMINISTRATOR | bit))


def plan_release(member_role_ids: Iterable[int], saved_role_ids: Iterable[int], quarantine_role_id: Optional[int],
                 guild_roles: Dict[int, dict], bot_top_position: int, guild_id: int) -> Tuple[List[int], int, int]:
    """New full role list for a released member, plus (restored, lost) counts.

    Mirrors modules/server_panel_quarantine.release_member: keep what they hold now (minus the
    quarantine role), add back saved roles that still exist and that the bot can still hand out
    (not @everyone, not managed, below the bot's top role)."""
    q = int(quarantine_role_id) if quarantine_role_id else None
    keep = [int(r) for r in member_role_ids if int(r) != int(guild_id) and (q is None or int(r) != q)]
    restore, saved = [], list(saved_role_ids or [])
    for rid in saved:
        rid = int(rid)
        role = guild_roles.get(rid)
        if role is None or rid == int(guild_id) or role.get("managed"):
            continue
        if int(role.get("position", 0)) >= int(bot_top_position):
            continue
        restore.append(rid)
    new_roles = keep + [r for r in restore if r not in keep]
    return new_roles, len(restore), len(saved) - len(restore)


def bot_top_position(bot_role_ids: Iterable[int], guild_roles: Dict[int, dict]) -> int:
    return max([int(guild_roles[int(r)].get("position", 0)) for r in bot_role_ids if int(r) in guild_roles] or [0])


def review_row(row: dict, member: Optional[dict], now: Optional[datetime] = None) -> dict:
    """What the browser sees about one quarantined account. Ids as strings."""
    now = now or datetime.now(timezone.utc)
    uid = int(row["user_id"])
    created = snowflake_created(uid)
    user = (member or {}).get("user") or {}
    avatar = (f"https://cdn.discordapp.com/avatars/{uid}/{user['avatar']}.png?size=64" if user.get("avatar")
              else f"https://cdn.discordapp.com/embed/avatars/{(uid >> 22) % 6}.png")
    since = row.get("created_at")
    return {
        "user_id": str(uid),
        "name": str(user.get("global_name") or user.get("username") or f"User {uid}")[:40],
        "avatar_url": avatar,
        "in_server": member is not None,
        "account_age_days": round(max((now - created).total_seconds(), 0) / 86400, 1),
        "quarantined_at": since.isoformat() if hasattr(since, "isoformat") else None,
        "reason": clean_reason(row.get("reason")),
        "saved_roles": len(row.get("saved_role_ids") or []),
    }
