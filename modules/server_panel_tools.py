"""
Server Owners Panel — Phase 8 data helpers (Tools hub).

Invites, scheduled messages and link buttons. Same rules as modules/server_panel.py:
reads and writes go through the same db.* functions the slash commands use (no second
copy of the rules), every change writes one audit row, everything is keyed by
(guild_id, clone_id). Link buttons are the one exception to clone scoping: the table
is keyed by chat_id only (shared with Telegram), exactly like /linkbutton.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from modules.server_panel import record_change

logger = logging.getLogger(__name__)

MAX_LINK_BUTTONS = 5           # Discord: 5 buttons per action row (same cap as /linkbutton)
MAX_SCHEDULES = 25             # one select menu holds 25 options
MIN_INTERVAL_SECONDS = 60
MAX_TEXT = 2000
VOICE_XP_RATE_CAP = 25         # voice_xp.VOICE_XP_RATE_CAP

DURATION_RE = re.compile(r"(\d+)\s*([smhdw])", re.IGNORECASE)
UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})$")

SCHEDULE_KINDS = ("once", "recurring", "daily")


# ── parsing (mirrors cogs/schedule.py so the panel and /schedule agree) ──────

def parse_duration(text: str) -> Optional[int]:
    matches = DURATION_RE.findall((text or "").strip())
    if not matches:
        return None
    total = sum(int(a) * UNIT_SECONDS[u.lower()] for a, u in matches)
    return total or None


def parse_time_of_day(text: str) -> Optional[Tuple[int, int]]:
    m = TIME_RE.match((text or "").strip())
    if not m:
        return None
    hour, minute = int(m.group(1)), int(m.group(2))
    return (hour, minute) if 0 <= hour < 24 and 0 <= minute < 60 else None


def plan_schedule(kind: str, when: str, now: Optional[datetime] = None) -> Tuple[Optional[datetime], Optional[int], Optional[str]]:
    """(run_at, interval_seconds, error). Exactly one of run_at / error is set."""
    now = now or datetime.now(timezone.utc)
    if kind == "once":
        secs = parse_duration(when)
        if secs is None:
            return None, None, "Couldn't parse that delay — try `2h`, `30m` or `1d`."
        return now + timedelta(seconds=secs), None, None
    if kind == "recurring":
        secs = parse_duration(when)
        if secs is None or secs < MIN_INTERVAL_SECONDS:
            return None, None, "Couldn't parse that interval — try `1d`, `12h` or `30m` (minimum 1m)."
        return now + timedelta(seconds=secs), secs, None
    if kind == "daily":
        parsed = parse_time_of_day(when)
        if parsed is None:
            return None, None, "Couldn't parse that time — use 24h UTC like `09:00` or `21:30`."
        run_at = now.replace(hour=parsed[0], minute=parsed[1], second=0, microsecond=0)
        if run_at <= now:
            run_at += timedelta(days=1)
        return run_at, 86400, None
    return None, None, "Unknown schedule type."


def describe_interval(seconds: Optional[int]) -> str:
    if not seconds:
        return "one-off"
    if seconds % 86400 == 0:
        d = seconds // 86400
        return "daily" if d == 1 else f"every {d}d"
    if seconds % 3600 == 0:
        return f"every {seconds // 3600}h"
    if seconds % 60 == 0:
        return f"every {seconds // 60}m"
    return f"every {seconds}s"


def validate_link(label: str, url: str) -> Optional[str]:
    """None when fine, else the reason (same rules as /linkbutton add)."""
    label, url = (label or "").strip(), (url or "").strip()
    if not label:
        return "The button needs a label."
    if len(label) > 80:
        return "Label is too long (Discord button labels max out at 80 characters)."
    if not url.startswith(("http://", "https://")):
        return "That doesn't look like a link — it must start with http:// or https://."
    if len(url) > 512:
        return "That link is too long (512 characters max)."
    return None


def validate_voice_rate(text: str) -> Tuple[Optional[int], Optional[str]]:
    try:
        rate = int(str(text).strip())
    except (TypeError, ValueError):
        return None, f"XP per minute must be a whole number from 1 to {VOICE_XP_RATE_CAP}."
    if not 1 <= rate <= VOICE_XP_RATE_CAP:
        return None, f"XP per minute must be a whole number from 1 to {VOICE_XP_RATE_CAP}."
    return rate, None


# ── invites ──────────────────────────────────────────────────────────────

async def _audited(guild_id, clone_id, actor_id, prefix: str, old: dict, fields: dict) -> None:
    for k, v in fields.items():
        await record_change(guild_id, clone_id, actor_id, f"{prefix}.{k}", (old or {}).get(k), v)


async def invites_state(guild_id: int, clone_id: Optional[int]) -> Dict[str, Any]:
    """Config plus the top inviters. Each read is isolated so one failure
    shows an empty section instead of breaking the screen."""
    from database import db
    try:
        cfg = await db.get_invite_tracker_config(guild_id, clone_id=clone_id)
    except Exception:
        logger.debug("[server-panel] invite config read failed", exc_info=True)
        cfg = {}
    try:
        top = await db.get_invite_leaderboard(guild_id, clone_id, 5)
    except Exception:
        logger.debug("[server-panel] invite leaderboard read failed", exc_info=True)
        top = []
    return {"cfg": cfg, "top": top}


async def set_invites(guild_id, clone_id, actor_id, **fields) -> None:
    from database import db
    old = await db.get_invite_tracker_config(guild_id, clone_id=clone_id)
    await db.set_invite_tracker_config(guild_id, clone_id=clone_id, **fields)
    await _audited(guild_id, clone_id, actor_id, "invites", old, fields)


async def set_invite_leaderboard_channel(guild_id, clone_id, actor_id, channel_id: Optional[int]) -> None:
    """None = default (announce channel), -1 = off, else a channel id."""
    from database import db
    old = await db.get_invite_tracker_config(guild_id, clone_id=clone_id)
    await db.set_invite_leaderboard_autopost_channel(guild_id, clone_id, channel_id)
    await _audited(guild_id, clone_id, actor_id, "invites", old, {"leaderboard_autopost_channel_id": channel_id})


# ── scheduled messages ───────────────────────────────────────────────────

async def list_schedules(guild_id: int, clone_id: Optional[int]) -> List[dict]:
    from database import db
    try:
        return await db.list_scheduled_messages(guild_id, clone_id=clone_id)
    except Exception:
        logger.debug("[server-panel] schedule list failed", exc_info=True)
        return []


async def add_schedule(guild_id, clone_id, actor_id, channel_id: int, kind: str, when: str,
                       text: str) -> Tuple[Optional[dict], Optional[str]]:
    """(job, error). Validation, the per-server cap, then the same create call /schedule uses."""
    from database import db
    text = (text or "").strip()
    if not text:
        return None, "The message can't be empty."
    if len(text) > MAX_TEXT:
        return None, f"The message is too long ({MAX_TEXT} characters max)."
    run_at, interval, err = plan_schedule(kind, when)
    if err:
        return None, err
    if len(await list_schedules(guild_id, clone_id)) >= MAX_SCHEDULES:
        return None, f"This server already has {MAX_SCHEDULES} scheduled messages — cancel one first."
    job = await db.create_scheduled_message(guild_id, channel_id, text, run_at, interval, actor_id, clone_id=clone_id)
    await record_change(guild_id, clone_id, actor_id, "schedule.add", None,
                        f"#{job.get('id')} {kind} in {channel_id}")
    return job, None


async def cancel_schedule(guild_id, clone_id, actor_id, schedule_id: int) -> bool:
    from database import db
    ok = await db.delete_scheduled_message(guild_id, schedule_id, clone_id=clone_id)
    if ok:
        await record_change(guild_id, clone_id, actor_id, "schedule.cancel", f"#{schedule_id}", None)
    return bool(ok)


# ── link buttons ─────────────────────────────────────────────────────────

async def list_links(guild_id: int) -> List[dict]:
    from database import db
    try:
        return await db.list_link_buttons(guild_id)
    except Exception:
        logger.debug("[server-panel] link button list failed", exc_info=True)
        return []


async def add_link(guild_id, clone_id, actor_id, label: str, url: str) -> Optional[str]:
    """None on success, else a reason."""
    from database import db
    label, url = label.strip(), url.strip()
    err = validate_link(label, url)
    if err:
        return err
    existing = await list_links(guild_id)
    old_url = next((b["url"] for b in existing if b["label"] == label), None)
    if old_url is None and len(existing) >= MAX_LINK_BUTTONS:
        return (f"This server already has {MAX_LINK_BUTTONS} link buttons — Discord only allows "
                f"{MAX_LINK_BUTTONS} per panel. Remove one first.")
    if not await db.add_link_button(guild_id, label, url, actor_id):
        return "Couldn't save that. Try again."
    await record_change(guild_id, clone_id, actor_id, f"link_button.{label}"[:100], old_url, url)
    return None


async def remove_link(guild_id, clone_id, actor_id, label: str) -> bool:
    from database import db
    existing = await list_links(guild_id)
    old_url = next((b["url"] for b in existing if b["label"] == label), None)
    ok = await db.remove_link_button(guild_id, label)
    if ok:
        await record_change(guild_id, clone_id, actor_id, f"link_button.{label}"[:100], old_url, None)
    return bool(ok)
