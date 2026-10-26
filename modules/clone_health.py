# path: modules/clone_health.py
"""Clone health board: classification, change detection and the Components V2 pages (pure logic, no I/O).

The main bot reads every clone's heartbeat (discord_cloned_bots.last_heartbeat, written every 5 minutes by each clone's
own process) and keeps ONE self-updating board in the support server (cogs/clone_health.py does the Discord side).

Noise rules that shape this file:
  * The board is only edited when something MEANINGFUL changes: a clone's state or its server count. "Seen 3m ago" uses
    Discord's live <t:ts:R> timestamps, so time passing never needs an edit.
  * A missed heartbeat is only "delayed" (yellow, never logged); a clone is "offline" (red, logged once) after FOUR
    missed beats, so a redeploy or a short blip never alerts.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import discord

HEARTBEAT_SECONDS = 300                       # discord_bot/bot.py _heartbeat_loop
ONLINE_MAX_AGE = 8 * 60                       # one beat + slack
DELAYED_MAX_AGE = 20 * 60                     # up to four beats missed: yellow, not alarming yet
STARTING_GRACE = 10 * 60                      # a brand-new clone that has not beaten yet

PER_PAGE = 14                                 # clone lines per message (well inside V2's 40 components / 4000 chars)
MAX_PAGES = 5
NAME_MAX = 32

ONLINE, DELAYED, STARTING, OFFLINE, STOPPED = "online", "delayed", "starting", "offline", "stopped"
EMOJI = {ONLINE: "🟢", DELAYED: "🟡", STARTING: "🔵", OFFLINE: "🔴", STOPPED: "⚫"}
_ORDER = {OFFLINE: 0, DELAYED: 1, STARTING: 2, ONLINE: 3, STOPPED: 4}


@dataclass(frozen=True)
class Entry:
    clone_id: int
    name: str
    state: str
    servers: int
    members: int
    seen_ts: Optional[int]                    # unix seconds of the last heartbeat, None = never


@dataclass(frozen=True)
class MainInfo:
    servers: int
    members: int
    up_since_ts: int


def _ts(value) -> Optional[int]:
    try:
        return int(value.timestamp()) if value is not None else None
    except Exception:
        return None


def classify(status: str, last_heartbeat_ts: Optional[int], created_ts: Optional[int], now: float) -> str:
    if (status or "").lower() != "active":
        return STOPPED
    if last_heartbeat_ts is None:
        return STARTING if created_ts is not None and now - created_ts <= STARTING_GRACE else OFFLINE
    age = now - last_heartbeat_ts
    if age <= ONLINE_MAX_AGE:
        return ONLINE
    return DELAYED if age <= DELAYED_MAX_AGE else OFFLINE


def build_entries(rows: Iterable[dict], now: float) -> List[Entry]:
    out = []
    for r in rows:
        seen = _ts(r.get("last_heartbeat"))
        name = (r.get("bot_username") or f"clone {r['clone_id']}").lstrip("@")[:NAME_MAX]
        out.append(Entry(int(r["clone_id"]), name,
                         classify(r.get("status"), seen, _ts(r.get("created_at")), now),
                         int(r.get("servers") or 0), int(r.get("members") or 0), seen))
    out.sort(key=lambda e: (_ORDER[e.state], e.clone_id))
    return out


def fingerprint(entries: Sequence[Entry], main_servers: int) -> str:
    """Only what deserves an edit: each clone's state and server count (NOT member counts, ages or latency)."""
    raw = f"main:{main_servers}|" + "|".join(f"{e.clone_id}:{e.state}:{e.servers}"
                                             for e in sorted(entries, key=lambda e: e.clone_id))
    return hashlib.sha1(raw.encode()).hexdigest()


def diff_states(prev: Optional[Dict[int, str]], entries: Sequence[Entry]) -> Tuple[List[Entry], List[Entry]]:
    """(went_offline, recovered). With no baseline (first tick after a restart) nothing is reported, so a deploy
    never produces a burst of alerts. Only the offline boundary matters; yellow/blue flips are not events."""
    if prev is None:
        return [], []
    down, up = [], []
    for e in entries:
        before = prev.get(e.clone_id)
        if before is None or before == e.state:
            continue
        if e.state == OFFLINE:
            down.append(e)
        elif before == OFFLINE and e.state in (ONLINE, DELAYED, STARTING):
            up.append(e)
    return down, up


def _line(e: Entry) -> str:
    base = f"{EMOJI[e.state]} **#{e.clone_id}** {e.name} · {e.servers:,} server{'' if e.servers == 1 else 's'}"
    if e.members:
        base += f" · {e.members:,} members"
    if e.state == STARTING:
        return base + " · starting up"
    if e.seen_ts is None:
        return base + " · never connected"
    return base + f" · seen <t:{e.seen_ts}:R>"


def _accent(entries: Sequence[Entry]) -> discord.Color:
    states = {e.state for e in entries}
    if OFFLINE in states:
        return discord.Color.red()
    if DELAYED in states or STARTING in states:
        return discord.Color.gold()
    return discord.Color.green()


def build_pages(entries: Sequence[Entry], main: MainInfo, events: Sequence[Tuple[int, str]],
                per_page: int = PER_PAGE, max_pages: int = MAX_PAGES) -> List[discord.ui.LayoutView]:
    """The board as 1..max_pages Components V2 messages. Problems are listed first, so if the list is ever too long
    it is only healthy clones that get summarised."""
    listed = [e for e in entries if e.state != STOPPED]
    stopped = len(entries) - len(listed)
    counts = {s: sum(1 for e in entries if e.state == s) for s in _ORDER}
    capacity = per_page * max_pages
    shown, hidden = listed[:capacity], max(0, len(listed) - capacity)
    chunks = [shown[i:i + per_page] for i in range(0, len(shown), per_page)] or [[]]
    accent = _accent(entries)

    summary = " · ".join(f"{EMOJI[s]} {counts[s]} {s}" for s in (ONLINE, DELAYED, STARTING, OFFLINE) if counts[s])
    header = [f"## 🩺 Clone health",
              f"**{len(entries)} clone{'' if len(entries) == 1 else 's'}**" + (f" · {summary}" if summary else ""),
              f"🟢 **Main bot** · {main.servers:,} server{'' if main.servers == 1 else 's'} · up since <t:{main.up_since_ts}:R>"]
    if events:
        header.append("**Recent events**\n" + "\n".join(f"<t:{ts}:t> {text}" for ts, text in events))

    pages = []
    for idx, chunk in enumerate(chunks):
        view = discord.ui.LayoutView(timeout=None)
        box = discord.ui.Container(accent_colour=accent)
        if idx == 0:
            box.add_item(discord.ui.TextDisplay("\n".join(header)))
            box.add_item(discord.ui.Separator())
        if chunk:
            box.add_item(discord.ui.TextDisplay("\n".join(_line(e) for e in chunk)))
        elif idx == 0:
            box.add_item(discord.ui.TextDisplay("No clones are registered yet."))
        foot = ["Updates by itself when something changes · seen-times are live"]
        if idx == len(chunks) - 1:
            if stopped:
                foot.append(f"⚫ {stopped} stopped clone{'' if stopped == 1 else 's'} not listed")
            if hidden:
                foot.append(f"+{hidden} more not listed (problems are always listed first)")
        if len(chunks) > 1:
            foot.append(f"page {idx + 1}/{len(chunks)}")
        box.add_item(discord.ui.Separator())
        box.add_item(discord.ui.TextDisplay("-# " + " · ".join(foot)))
        view.add_item(box)
        pages.append(view)
    return pages
