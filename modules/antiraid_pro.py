# path: modules/antiraid_pro.py

"""
Anti-raid Pro (PREMIUM, per server) — the logic behind three extras on top of the free anti-raid:

  1. Join-profile filter   flag / quarantine / kick joiners whose account is young, has the default
                           avatar, or has a spammy name.
  2. Raid report           a summary posted when a raid ends: counts, timeline, account-age mix and
                           the patterns the raiders share (name stems, default avatars).
  3. Quarantine & review   raiders are parked in the quarantine role instead of being banned straight
                           away; staff then get Ban all / Release all / Review one-by-one.

Free servers keep the whole free anti-raid (spike detection, lockdown, timeout/kick, alerts). Premium
choices are SAVED even when premium lapses but are not USED (effective_cfg), exactly like the
honeypot's extras, and come back the moment premium returns.

This module has no slash command and no persistent state beyond the four filter_* columns on
discord_antiraid_config and the existing quarantine tables; the raid log is in memory (bounded) and
is only used to write the report. `database` is imported lazily so this file is cheap to import and
easy to test.
"""

from __future__ import annotations

import logging
import re
import time
from collections import Counter, deque
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import discord

logger = logging.getLogger(__name__)

# ── settings ──────────────────────────────────────────────────────────────

# key -> (label, emoji, description)
FILTER_ACTIONS = {
    "flag":       ("Flag only", "🚩", "Post it in the log channel, change nothing"),
    "quarantine": ("Quarantine for review", "🔒", "Hold them in the no-access role until staff decide"),
    "kick":       ("Kick", "👢", "Remove them; real people can rejoin later"),
}
DEFAULT_FILTER_ACTION = "flag"
FILTER_AGE_CHOICES = (0, 1, 3, 7, 14, 30)       # days; 0 = age check off

QUARANTINE_REASON_PREFIX = "[anti-raid]"         # marks quarantine rows this feature created
REVIEW_LIMIT = 200                               # most raid-quarantined people one tap will touch
BAN_DELETE_SECONDS = 3600                        # message history removed when banning a raider

_MAX_ENTRIES = 500                               # join records kept per raid (bounds memory)
_PREMIUM_TTL = 30.0
_FLAG_POSTS_PER_MIN = 5                          # flag-only log lines per server per minute

OUTCOMES = {
    "quarantined": "🔒 Quarantined",
    "kicked": "👢 Kicked",
    "timed_out": "⏳ Timed out",
    "flagged": "🚩 Flagged",
    "failed": "⚠️ Couldn't act on",
    "left_alone": "🤝 Left alone",
}
ACTIONED = ("quarantined", "kicked", "timed_out")

_premium_cache: Dict[tuple, Tuple[float, bool]] = {}
_logs: Dict[tuple, "RaidLog"] = {}
_flag_posts: Dict[tuple, deque] = {}


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── premium ───────────────────────────────────────────────────────────────

async def is_premium(guild_id: int, clone_id) -> bool:
    """True when the server has active premium. Fails safe (False) and caches for 30s so a
    join burst doesn't hit the database once per joiner. Errors are never cached."""
    key = (guild_id, clone_id)
    hit = _premium_cache.get(key)
    now = time.monotonic()
    if hit and hit[0] > now:
        return hit[1]
    try:
        from database import db
        value = bool(await db.is_guild_premium_active(guild_id, clone_id))
    except Exception:
        return False
    _premium_cache[key] = (now + _PREMIUM_TTL, value)
    return value


def effective_cfg(cfg: dict, premium: bool) -> dict:
    """The settings anti-raid actually uses. Premium servers get what they saved; free servers
    fall back to the free behaviour (saved premium choices are kept, just paused)."""
    if premium:
        return cfg
    out = dict(cfg)
    if out.get("joiner_action") == "quarantine":
        out["joiner_action"] = "none"
    return out


def paused_extras(cfg: dict) -> bool:
    """True when a lapsed server still has premium choices saved that are not being used."""
    return cfg.get("joiner_action") == "quarantine" or filter_enabled(cfg)


# ── profile filter ────────────────────────────────────────────────────────

_NAME_LINK = re.compile(r"(discord\.gg|discord(?:app)?\.com/invite|https?://|www\.|\.gg/)", re.I)
_NAME_PING = re.compile(r"@(everyone|here)", re.I)
_NAME_BAIT = re.compile(r"(free\s*nitro|nitro\s*gift|airdrop|giveaway|onlyfans|steam\s*gift|crypto\s*drop)", re.I)
_NAME_DIGIT_TAIL = re.compile(r"\d{6,}$")
_NAME_RUN = re.compile(r"(.)\1{5,}")


def name_flags(name: str) -> List[str]:
    """Short reasons a name looks like a raid/spam account. Deliberately conservative."""
    name = name or ""
    out = []
    if _NAME_LINK.search(name):
        out.append("invite/link in name")
    if _NAME_PING.search(name):
        out.append("mass-ping bait in name")
    if _NAME_BAIT.search(name):
        out.append("scam wording in name")
    if _NAME_DIGIT_TAIL.search(name):
        out.append("long number at the end")
    if _NAME_RUN.search(name):
        out.append("repeated characters")
    combining = sum(1 for ch in name if 0x0300 <= ord(ch) <= 0x036F)
    if combining >= 4:
        out.append("stacked accents")
    return out


def account_age_days(member, now: Optional[datetime] = None) -> float:
    created = getattr(member, "created_at", None)
    if created is None:
        return 9999.0
    return max(0.0, ((now or _now()) - created).total_seconds() / 86400.0)


def has_default_avatar(member) -> bool:
    """True when the account never set an avatar (a guild-only avatar doesn't count)."""
    return getattr(member, "avatar", None) is None


def fmt_age(days: float) -> str:
    if days < 1 / 24:
        return f"{max(1, int(days * 1440))} min"
    if days < 1:
        return f"{int(days * 24)} h"
    return f"{int(days)} d"


def filter_enabled(cfg: dict) -> bool:
    return (int(cfg.get("filter_age_days") or 0) > 0
            or bool(cfg.get("filter_default_avatar"))
            or bool(cfg.get("filter_suspicious_name")))


def evaluate_profile(member, cfg: dict, now: Optional[datetime] = None) -> List[str]:
    """Reasons this joiner trips the filter (empty = looks fine)."""
    reasons = []
    limit = int(cfg.get("filter_age_days") or 0)
    if limit > 0:
        age = account_age_days(member, now)
        if age < limit:
            reasons.append(f"account is {fmt_age(age)} old (under {limit} d)")
    if cfg.get("filter_default_avatar") and has_default_avatar(member):
        reasons.append("default avatar")
    if cfg.get("filter_suspicious_name"):
        seen = []
        for name in (getattr(member, "name", ""), getattr(member, "global_name", None)):
            for flag in name_flags(name or ""):
                if flag not in seen:
                    seen.append(flag)
        reasons.extend(seen)
    return reasons


def filter_summary(cfg: dict) -> str:
    if not filter_enabled(cfg):
        return "Off"
    parts = []
    age = int(cfg.get("filter_age_days") or 0)
    if age:
        parts.append(f"account under {age} d")
    if cfg.get("filter_default_avatar"):
        parts.append("default avatar")
    if cfg.get("filter_suspicious_name"):
        parts.append("suspicious name")
    action = FILTER_ACTIONS.get(cfg.get("filter_action") or DEFAULT_FILTER_ACTION, FILTER_ACTIONS["flag"])
    return " · ".join(parts) + f" → {action[0]}"


def flag_post_allowed(key: tuple, now: Optional[float] = None) -> bool:
    """Rate limit for flag-only log lines so a 200-account raid can't flood the log channel."""
    now = time.monotonic() if now is None else now
    q = _flag_posts.setdefault(key, deque())
    while q and q[0] < now - 60:
        q.popleft()
    if len(q) >= _FLAG_POSTS_PER_MIN:
        return False
    q.append(now)
    return True


# ── raid log (memory only, bounded) ───────────────────────────────────────

class RaidLog:
    __slots__ = ("started", "entries", "total", "outcomes", "filter_hits", "truncated")

    def __init__(self, started: Optional[datetime] = None):
        self.started = started or _now()
        self.entries: List[dict] = []
        self.total = 0
        self.outcomes: Counter = Counter()
        self.filter_hits = 0
        self.truncated = False


def start_log(key: tuple, now: Optional[datetime] = None) -> RaidLog:
    log = RaidLog(now)
    _logs[key] = log
    return log


def get_log(key: tuple) -> Optional[RaidLog]:
    return _logs.get(key)


def pop_log(key: tuple) -> Optional[RaidLog]:
    return _logs.pop(key, None)


def outcome_for(action: str, result) -> str:
    """Maps apply_joiner_action's True/False/None to a report outcome."""
    if result is None:
        return "left_alone"
    if result is False:
        return "failed"
    return {"timeout": "timed_out", "kick": "kicked", "quarantine": "quarantined"}.get(action, "left_alone")


def record_join(key: tuple, member, outcome: str, reasons: Optional[List[str]] = None,
                now: Optional[datetime] = None) -> None:
    """Adds one joiner to the active raid log (creating it if the bot restarted mid-raid)."""
    log = _logs.get(key) or start_log(key, now)
    log.total += 1
    log.outcomes[outcome] += 1
    if reasons:
        log.filter_hits += 1
    if len(log.entries) >= _MAX_ENTRIES:
        log.truncated = True
        return
    log.entries.append({
        "t": now or _now(),
        "id": getattr(member, "id", 0),
        "name": getattr(member, "name", "") or "",
        "age_days": account_age_days(member, now),
        "default_avatar": has_default_avatar(member),
        "outcome": outcome,
        "reasons": list(reasons or []),
    })


# ── report analysis (pure) ────────────────────────────────────────────────

AGE_BUCKETS = (("under 1 day", 1), ("1–7 days", 7), ("7–30 days", 30), ("30+ days", None))


def age_buckets(entries: List[dict]) -> List[Tuple[str, int]]:
    counts = [0] * len(AGE_BUCKETS)
    for e in entries:
        for i, (_label, limit) in enumerate(AGE_BUCKETS):
            if limit is None or e["age_days"] < limit:
                counts[i] += 1
                break
    return [(AGE_BUCKETS[i][0], counts[i]) for i in range(len(AGE_BUCKETS)) if counts[i]]


def median_age(entries: List[dict]) -> Optional[float]:
    ages = sorted(e["age_days"] for e in entries)
    if not ages:
        return None
    mid = len(ages) // 2
    return ages[mid] if len(ages) % 2 else (ages[mid - 1] + ages[mid]) / 2


def name_stems(entries: List[dict], min_count: int = 3, top: int = 3) -> List[Tuple[str, int]]:
    """Names that only differ by digits/separators (user123, user124, …) share a stem."""
    stems = Counter()
    for e in entries:
        stem = re.sub(r"[\d_\-\.\s]+", "", (e["name"] or "").lower())
        if len(stem) >= 3:
            stems[stem] += 1
    return [(s, n) for s, n in stems.most_common(top) if n >= min_count]


def peak_window(entries: List[dict], seconds: int = 10) -> Optional[Tuple[int, datetime]]:
    """(most joins inside any `seconds` window, when that window started)."""
    times = sorted(e["t"] for e in entries)
    if not times:
        return None
    best, best_start, lo = 0, times[0], 0
    for hi, t in enumerate(times):
        while (t - times[lo]).total_seconds() > seconds:
            lo += 1
        if hi - lo + 1 > best:
            best, best_start = hi - lo + 1, times[lo]
    return best, best_start


def _ts(dt: datetime, style: str = "T") -> str:
    return f"<t:{int(dt.timestamp())}:{style}>"


def build_report_embed(log: Optional[RaidLog], ended: datetime, summary_line: str,
                       ended_by=None, waiting: int = 0) -> discord.Embed:
    """The post-raid summary. `log` may be None (bot restarted mid-raid): a short report is still posted."""
    embed = discord.Embed(title="📊 Raid report — raid mode ended", color=discord.Color.green())
    who = f"Ended by {ended_by.mention}. " if ended_by is not None else "No suspicious joins for a while. "
    embed.description = who + summary_line
    if log is None or log.total == 0:
        embed.add_field(
            name="Details",
            value="No joiner details were recorded for this raid (the bot may have restarted while it was running).",
            inline=False)
    else:
        entries = log.entries
        actioned = sum(log.outcomes.get(o, 0) for o in ACTIONED)
        lines = [f"**{log.total}** joined during the raid · **{actioned}** actioned"]
        for key in ("quarantined", "kicked", "timed_out", "flagged", "failed", "left_alone"):
            n = log.outcomes.get(key, 0)
            if n:
                lines.append(f"{OUTCOMES[key]}: **{n}**")
        embed.add_field(name="Joins", value="\n".join(lines), inline=True)

        tl = [f"Started {_ts(log.started)}"]
        peak = peak_window(entries)
        if peak and peak[0] > 1:
            tl.append(f"Peak: **{peak[0]}** joins in 10s at {_ts(peak[1])}")
        if entries:
            tl.append(f"Last join {_ts(entries[-1]['t'])}")
        mins = max(1, round((ended - log.started).total_seconds() / 60))
        tl.append(f"Lasted about **{mins} min**")
        embed.add_field(name="Timeline", value="\n".join(tl), inline=True)

        if entries:
            ages = ", ".join(f"{label}: **{n}**" for label, n in age_buckets(entries))
            med = median_age(entries)
            pat = [f"Account age — {ages}" + (f" (median {fmt_age(med)})" if med is not None else "")]
            default_av = sum(1 for e in entries if e["default_avatar"])
            if default_av:
                pat.append(f"Default avatar: **{default_av}** of {len(entries)} "
                           f"({round(100 * default_av / len(entries))}%)")
            stems = name_stems(entries)
            if stems:
                pat.append("Similar names — " + ", ".join(f"`{s}…` ×{n}" for s, n in stems))
            if log.filter_hits:
                pat.append(f"Tripped the profile filter: **{log.filter_hits}**")
            embed.add_field(name="Patterns", value="\n".join(pat), inline=False)
        if log.truncated:
            embed.set_footer(text=f"Details cover the first {_MAX_ENTRIES} joiners; counts above include everyone.")
    if waiting:
        embed.add_field(
            name="Waiting for your decision",
            value=f"**{waiting}** account(s) are held in quarantine. Use the buttons below.",
            inline=False)
    return embed


# ── quarantine & review ───────────────────────────────────────────────────

def is_raid_row(row: dict) -> bool:
    return str(row.get("reason") or "").startswith(QUARANTINE_REASON_PREFIX)


async def raid_quarantined(guild, clone_id) -> List[dict]:
    """People quarantined BY anti-raid (not ones staff quarantined by hand), oldest first."""
    from database import db
    try:
        rows = await db.list_quarantined(guild.id, clone_id, REVIEW_LIMIT)
    except Exception:
        logger.exception("[antiraid-pro] couldn't list quarantined for guild %s", guild.id)
        return []
    rows = [r for r in rows if is_raid_row(r)]
    rows.sort(key=lambda r: r.get("created_at") or datetime.min.replace(tzinfo=timezone.utc))
    return rows


async def quarantine_suspect(guild, clone_id, member, reason: str) -> Tuple[bool, str]:
    """Quarantines a joiner for staff review. Already-quarantined counts as success so the
    filter and the raid handler can both see the same person without one reporting a failure."""
    from database import db
    from modules import server_panel_quarantine as spq
    try:
        if await db.get_quarantined(guild.id, clone_id, member.id) is not None:
            return True, "already quarantined"
    except Exception:
        logger.debug("[antiraid-pro] quarantine lookup failed", exc_info=True)
    return await spq.quarantine_member(guild, clone_id, None, member, reason=f"{QUARANTINE_REASON_PREFIX} {reason}")


def can_ban(member) -> bool:
    perms = getattr(member, "guild_permissions", None)
    return bool(perms and (perms.administrator or perms.ban_members))


def can_review(member) -> bool:
    perms = getattr(member, "guild_permissions", None)
    return bool(perms and (perms.administrator or perms.ban_members or perms.manage_guild))


async def ban_suspect(guild, clone_id, actor, user_id: int) -> Tuple[bool, str]:
    from database import db
    from modules.server_panel import record_change
    me = getattr(guild, "me", None)
    if me is None or not me.guild_permissions.ban_members:
        return False, "I need the **Ban Members** permission to do that."
    try:
        await guild.ban(discord.Object(id=user_id),
                        reason=f"[anti-raid review] banned by {actor}"[:400],
                        delete_message_seconds=BAN_DELETE_SECONDS)
    except (discord.Forbidden, discord.HTTPException):
        return False, "Discord refused that ban — check my role is above theirs. They stay quarantined."
    await db.remove_quarantined(guild.id, clone_id, user_id)
    await record_change(guild.id, clone_id, getattr(actor, "id", 0), "antiraid.review_ban", user_id, None)
    return True, f"🔨 <@{user_id}> was banned."


async def ban_all(guild, clone_id, actor) -> str:
    done = failed = 0
    for row in await raid_quarantined(guild, clone_id):
        ok, _ = await ban_suspect(guild, clone_id, actor, int(row["user_id"]))
        done += ok
        failed += (not ok)
    return f"🔨 Banned {done} account(s)" + (f"; {failed} could not be banned and stay quarantined." if failed else ".")


async def release_suspect(guild, clone_id, actor_id: int, user_id: int) -> Tuple[bool, str]:
    from modules import server_panel_quarantine as spq
    return await spq.release_member(guild, clone_id, actor_id, user_id)


async def release_all(guild, clone_id, actor_id: int) -> str:
    """Releases ONLY people anti-raid quarantined; manual quarantines are left alone."""
    done = failed = 0
    for row in await raid_quarantined(guild, clone_id):
        ok, _ = await release_suspect(guild, clone_id, actor_id, int(row["user_id"]))
        done += ok
        failed += (not ok)
    return f"🔓 Released {done} account(s)" + (f"; {failed} could not be released and stay listed." if failed else ".")


def next_pending(rows: List[dict], after_user_id: Optional[int] = None) -> Optional[dict]:
    """The next person to review: the one after `after_user_id` in list order, else the first."""
    if not rows:
        return None
    if after_user_id is not None:
        ids = [int(r["user_id"]) for r in rows]
        if after_user_id in ids:
            rest = rows[ids.index(after_user_id) + 1:]
            return rest[0] if rest else None
    return rows[0]


def review_embed(guild, row: dict, position: int, total: int) -> discord.Embed:
    uid = int(row["user_id"])
    member = guild.get_member(uid)
    embed = discord.Embed(title=f"🔎 Review {position} of {total}", color=discord.Color.orange())
    if member is None:
        embed.description = f"<@{uid}> (`{uid}`) has left the server."
    else:
        embed.description = f"{member.mention} (`{member.name}`)"
        embed.set_thumbnail(url=member.display_avatar.url)
        age = account_age_days(member)
        embed.add_field(name="Account age", value=fmt_age(age), inline=True)
        embed.add_field(name="Avatar", value="default" if has_default_avatar(member) else "custom", inline=True)
        joined = getattr(member, "joined_at", None)
        if joined:
            embed.add_field(name="Joined", value=_ts(joined, "R"), inline=True)
    why = str(row.get("reason") or "").replace(QUARANTINE_REASON_PREFIX, "").strip()
    if why:
        embed.add_field(name="Why", value=why[:200], inline=False)
    embed.set_footer(text="Ban removes them for good · Release gives their roles back · Skip decides later")
    return embed
