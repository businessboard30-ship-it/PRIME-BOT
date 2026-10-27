# path: modules/verification_pro.py

"""
Premium extras for join verification (discord_bot/cogs/verification.py).

Free for everyone: captcha / button check, verify by DM, verify channel, Unverified + Verified roles, channel lockdown.
Premium servers also get:
  - a custom verify panel (title, text, colour, banner)
  - a verification log channel
  - auto-kick of members who never verify
  - a minimum account age
  - a welcome message sent after a member verifies

Same rule as anti-raid (modules/antiraid_pro.py): a server that lapses keeps what it saved, but none of it is
USED until premium is active again. effective_cfg() is the single place that decides that.
"""

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

import discord

logger = logging.getLogger(__name__)

PRO_FIELDS = (
    "panel_title", "panel_text", "panel_color", "panel_image_url",
    "log_channel_id", "kick_after_minutes", "min_account_age_days", "welcome_text",
)

# (stored value, label). None = off.
KICK_AFTER_CHOICES = (
    (None, "Off"), (10, "10 minutes"), (30, "30 minutes"), (60, "1 hour"), (360, "6 hours"), (1440, "24 hours"),
)
MIN_AGE_CHOICES = ((None, "Off"), (1, "1 day"), (3, "3 days"), (7, "7 days"), (30, "30 days"))

TITLE_MAX = 100
TEXT_MAX = 600
WELCOME_MAX = 500
IMAGE_URL_MAX = 300

_HEX_RE = re.compile(r"^#?([0-9a-fA-F]{6})$")


# ── premium ───────────────────────────────────────────────────────────────

async def is_premium(guild_id: int, clone_id) -> bool:
    """Shares anti-raid's cached, fail-safe (False) premium lookup."""
    from modules.antiraid_pro import is_premium as _is_premium
    return await _is_premium(guild_id, clone_id)


def effective_cfg(cfg: dict, premium: bool) -> dict:
    """The settings verification actually uses. Free servers get the free behaviour; saved extras are kept, paused."""
    if premium:
        return cfg
    out = dict(cfg)
    for key in PRO_FIELDS:
        out[key] = None
    return out


async def effective_for(guild_id: int, clone_id, cfg: dict) -> dict:
    return effective_cfg(cfg, await is_premium(guild_id, clone_id))


def paused_extras(cfg: dict) -> bool:
    """True when a server has premium extras saved (so a lapsed server can be told they're paused)."""
    return any(cfg.get(k) not in (None, "") for k in PRO_FIELDS)


def label_for(choices, value) -> str:
    for stored, label in choices:
        if stored == value:
            return label
    return "Off"


# ── input parsing ─────────────────────────────────────────────────────────

def parse_color(text: Optional[str]) -> Optional[int]:
    """'#5865F2' / '5865f2' -> int. Blank -> None. Anything else raises ValueError."""
    text = (text or "").strip()
    if not text:
        return None
    m = _HEX_RE.match(text)
    if not m:
        raise ValueError("colour must be 6 hex digits, like #5865F2")
    return int(m.group(1), 16)


def clean_image_url(text: Optional[str]) -> Optional[str]:
    """Blank -> None. Must be an https link (Discord won't show anything else in an embed)."""
    text = (text or "").strip()
    if not text:
        return None
    if len(text) > IMAGE_URL_MAX or not text.lower().startswith("https://") or " " in text:
        raise ValueError("banner must be a direct https:// image link")
    return text


def clean_text(text: Optional[str], limit: int) -> Optional[str]:
    text = (text or "").strip()
    return text[:limit] if text else None


def render_placeholders(text: str, member: discord.abc.User, guild: discord.Guild) -> str:
    return (text.replace("{user}", member.mention if hasattr(member, "mention") else str(member))
                .replace("{server}", guild.name))


# ── rules (pure, so they can be tested without Discord) ───────────────────

def account_too_young(created_at: datetime, min_days: Optional[int], now: Optional[datetime] = None) -> bool:
    if not min_days:
        return False
    now = now or datetime.now(timezone.utc)
    return (now - created_at) < timedelta(days=int(min_days))


def should_kick(joined_at: Optional[datetime], kick_after_minutes: Optional[int], now: Optional[datetime] = None) -> bool:
    if not kick_after_minutes or joined_at is None:
        return False
    now = now or datetime.now(timezone.utc)
    return (now - joined_at) >= timedelta(minutes=int(kick_after_minutes))


def panel_look(cfg: dict) -> dict:
    """The custom-look fields of an (already effective) config, only those that are set."""
    return {k: cfg.get(k) for k in ("panel_title", "panel_text", "panel_color", "panel_image_url") if cfg.get(k) not in (None, "")}


# ── Discord side effects (all best-effort: never raise into a join / verify) ──

async def log_event(guild: discord.Guild, cfg: dict, text: str) -> None:
    channel_id = cfg.get("log_channel_id")
    if not channel_id:
        return
    channel = guild.get_channel(channel_id)
    if channel is None:
        return
    try:
        await channel.send(text, allowed_mentions=discord.AllowedMentions.none())
    except (discord.Forbidden, discord.HTTPException):
        logger.info("verification log: couldn't post in channel %s of guild %s", channel_id, guild.id)


async def send_welcome(member: discord.Member, cfg: dict) -> bool:
    """DMs the custom welcome message. True if one was sent. Closed DMs just return False."""
    text = cfg.get("welcome_text")
    if not text:
        return False
    try:
        await member.send(render_placeholders(text, member, member.guild))
        return True
    except (discord.Forbidden, discord.HTTPException):
        return False


async def on_verified(member: discord.Member, cfg: dict) -> bool:
    """Called after a member passes verification with an EFFECTIVE config. Returns True if the custom welcome went out."""
    await log_event(member.guild, cfg, f"✅ **Verified:** {member.mention} (`{member.id}`)")
    return await send_welcome(member, cfg)


async def kick_for(member: discord.Member, cfg: dict, reason: str, log_text: str, dm: Optional[str] = None) -> bool:
    """Kicks `member` (best-effort DM first) and logs it. Returns True if the kick went through."""
    if dm:
        try:
            await member.send(dm)
        except (discord.Forbidden, discord.HTTPException):
            pass
    try:
        await member.kick(reason=reason)
    except (discord.Forbidden, discord.HTTPException):
        logger.warning("verification: couldn't kick %s in guild %s (%s)", member.id, member.guild.id, reason)
        await log_event(member.guild, cfg, f"⚠️ Couldn't kick {member.mention} (`{member.id}`): check my permissions.")
        return False
    await log_event(member.guild, cfg, log_text)
    return True
