# path: modules/ai_server_ops.py

"""
Pure (no network, no Discord calls) helpers for the AI-only server-admin commands in
discord_bot/cogs/ai_server_ops.py: permission-name parsing, the role/member hierarchy rules, time / message-link /
image-URL parsing. Kept separate so the safety rules can be unit tested without a Discord connection.

The rules here are deliberately *stricter* than what Discord itself enforces, because the person behind the request
is an AI turning a sentence into an action:
  * `administrator` is never granted by the AI, to a role or in a channel override.
  * Dangerous permissions (manage roles/channels/server, kick/ban, @everyone pings...) can only be granted by someone
    who is the server owner or an Administrator.
  * Nobody can grant a permission they do not hold themselves.
  * Roles / members at or above the asker's top role (or the bot's) are never touched.
"""

import re
from datetime import datetime, timedelta, timezone
from typing import Iterable, List, Optional, Tuple
from urllib.parse import urlsplit

import discord

# ── permissions ──────────────────────────────────────────────────────────

# Never granted by the AI, whoever asks. (Revoking it is fine.)
NEVER_GRANT = frozenset({"administrator"})

# Only the server owner / an Administrator may grant these through the AI.
DANGEROUS = frozenset({
    "administrator", "manage_guild", "manage_roles", "manage_channels", "manage_webhooks", "manage_messages",
    "manage_threads", "manage_expressions", "manage_events", "kick_members", "ban_members", "moderate_members",
    "mention_everyone", "view_audit_log", "manage_nicknames", "move_members", "mute_members", "deafen_members",
})

_ALIASES = {
    "send": "send_messages", "post": "send_messages", "talk": "send_messages", "speak_in_chat": "send_messages",
    "view": "view_channel", "see": "view_channel", "read_messages": "view_channel", "read": "read_message_history",
    "history": "read_message_history", "react": "add_reactions", "reactions": "add_reactions",
    "attach": "attach_files", "files": "attach_files", "embed": "embed_links", "links": "embed_links",
    "join": "connect", "join_voice": "connect", "voice": "connect", "talk_in_voice": "speak",
    "manage_permissions": "manage_roles", "manage_emojis": "manage_expressions",
    "manage_emojis_and_stickers": "manage_expressions", "manage_server": "manage_guild",
    "ping_everyone": "mention_everyone", "everyone": "mention_everyone", "timeout": "moderate_members",
    "create_threads": "create_public_threads", "threads": "create_public_threads",
    "send_in_threads": "send_messages_in_threads", "use_commands": "use_application_commands",
    "slash_commands": "use_application_commands", "stream": "stream", "video": "stream",
}

_VALID = frozenset(discord.Permissions.VALID_FLAGS)
CHANNEL_PERMS = frozenset(n for n, v in discord.Permissions.all_channel() if v)


def normalize_perm(raw: str) -> Optional[str]:
    """'Send Messages' / 'send-messages' / 'send' -> 'send_messages'. None when it is not a Discord permission."""
    key = re.sub(r"[\s\-]+", "_", (raw or "").strip().lower()).strip("_")
    key = _ALIASES.get(key, key)
    return key if key in _VALID else None


def parse_perms(raw, *, only: Optional[Iterable[str]] = None) -> Tuple[List[str], List[str]]:
    """(valid permission names, unknown words) from 'send messages, add reactions and embed links'. Order kept,
    duplicates dropped. `only` limits what is valid (e.g. CHANNEL_PERMS for a channel override)."""
    allowed = set(only) if only is not None else None
    good: List[str] = []
    bad: List[str] = []
    for part in re.split(r"[,;\n]+|\band\b", str(raw or ""), flags=re.I):
        part = part.strip()
        if not part:
            continue
        name = normalize_perm(part)
        if name is None or (allowed is not None and name not in allowed):
            bad.append(part)
        elif name not in good:
            good.append(name)
    return good, bad


def nice_perm(name: str) -> str:
    return name.replace("_", " ").title()


def grant_problem(actor_has, names: Iterable[str], *, privileged: bool) -> Optional[str]:
    """Why these permissions may not be granted by this asker, or None.
    `actor_has(name) -> bool` says whether the asker holds the permission; `privileged` = owner or Administrator."""
    names = list(names)
    never = [n for n in names if n in NEVER_GRANT]
    if never:
        return ("I never grant **Administrator** — give it yourself in Server Settings if you really mean it.")
    if not privileged:
        risky = [nice_perm(n) for n in names if n in DANGEROUS]
        if risky:
            return ("Only the server owner or an Administrator can hand out " + ", ".join(f"**{r}**" for r in risky) + ".")
        lacking = [nice_perm(n) for n in names if not actor_has(n)]
        if lacking:
            return "You can't grant permissions you don't have yourself: " + ", ".join(f"**{r}**" for r in lacking) + "."
    return None


# ── hierarchy ────────────────────────────────────────────────────────────

def is_owner(guild, member) -> bool:
    return getattr(guild, "owner_id", None) == getattr(member, "id", object())


def is_privileged(guild, member) -> bool:
    """Server owner or Administrator."""
    return is_owner(guild, member) or bool(getattr(member.guild_permissions, "administrator", False))


def role_problem(guild, actor, role, *, assigning: bool = False, allow_everyone: bool = False) -> Optional[str]:
    """Why `actor` (and the bot) may not change/assign `role`, or None."""
    if role.is_default():
        return None if allow_everyone else "I don't change **@everyone** that way."
    if assigning and getattr(role, "managed", False):
        return f"**{role.name}** is managed by an integration or bot, so Discord doesn't let anyone hand it out."
    me = guild.me
    if role >= me.top_role:
        return f"**{role.name}** is at or above my highest role, so I can't touch it. Move my role higher first."
    if not is_owner(guild, actor) and role >= actor.top_role:
        return f"**{role.name}** is at or above your highest role, so you can't change it."
    return None


def member_problem(guild, actor, target, *, allow_self: bool = False) -> Optional[str]:
    """Why `actor` (and the bot) may not act on `target`, or None."""
    if is_owner(guild, target):
        return "Nobody can do that to the server owner."
    if target.id == guild.me.id:
        return "I won't do that to myself."
    if target.id == actor.id:
        return None if allow_self else "You can't do that to yourself."
    if target.top_role >= guild.me.top_role:
        return f"**{target.display_name}** has a role at or above mine, so I can't change them."
    if not is_owner(guild, actor) and target.top_role >= actor.top_role:
        return f"**{target.display_name}** has a role at or above yours."
    return None


# ── parsing ──────────────────────────────────────────────────────────────

def snowflake(raw) -> Optional[int]:
    """'123', '<@123>', '<@!123>', '<@&123>', '<#123>' -> 123."""
    m = re.fullmatch(r"<[@#][!&]?(\d{15,25})>|(\d{15,25})", str(raw or "").strip())
    return int(m.group(1) or m.group(2)) if m else None


_LINK = re.compile(r"discord(?:app)?\.com/channels/(\d+|@me)/(\d+)/(\d+)")


def message_ref(raw) -> Tuple[Optional[int], Optional[int]]:
    """(channel_id or None, message_id or None) from a message link or a bare id."""
    text = str(raw or "").strip()
    m = _LINK.search(text)
    if m:
        return int(m.group(2)), int(m.group(3))
    sf = snowflake(text)
    return None, sf


_REL = re.compile(r"^in\s+(\d+)\s*(m|min|mins|minutes?|h|hr|hrs|hours?|d|days?)$", re.I)


def parse_when(raw, now: Optional[datetime] = None) -> Optional[datetime]:
    """UTC datetime from 'in 30 minutes' / 'in 2h' / 'in 3 days' / '2026-10-20 18:00' / '2026-10-20T18:00Z'."""
    text = re.sub(r"\s*utc\s*$", "", str(raw or "").strip(), flags=re.I)
    if not text:
        return None
    now = now or datetime.now(timezone.utc)
    m = _REL.match(text)
    if m:
        n, unit = int(m.group(1)), m.group(2).lower()
        delta = (timedelta(minutes=n) if unit.startswith("m") else timedelta(hours=n) if unit.startswith("h")
                 else timedelta(days=n))
        return now + delta
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00").replace(" ", "T", 1))
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


CDN_HOSTS = frozenset({"cdn.discordapp.com", "media.discordapp.net"})
_IMG_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp")
MAX_IMAGE_BYTES = 8 * 1024 * 1024


def cdn_image_url_ok(url: str) -> bool:
    """Only an https link on Discord's own CDN to a png/jpg/gif/webp. The bot downloads this, so it is an exact-host
    allowlist (no other site, no redirects, no credentials, no odd ports) — that is what rules out SSRF."""
    try:
        parts = urlsplit((url or "").strip())
    except ValueError:
        return False
    return (parts.scheme == "https" and parts.hostname in CDN_HOSTS and parts.port in (None, 443)
            and parts.username is None and parts.path.lower().endswith(_IMG_EXT))


def clip(text, n: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def safe_text(text, n: int = 80) -> str:
    """Message/name text for a prompt or log line: one line, clipped, with @ and backticks defused so it can neither
    ping nor break the formatting."""
    return clip(str(text or "").replace("@", "@\u200b").replace("`", "'"), n)


def reason_for(actor) -> str:
    """The audit-log reason attached to every Discord call the AI makes."""
    return f"AI action requested by {actor} ({actor.id})"[:512]
