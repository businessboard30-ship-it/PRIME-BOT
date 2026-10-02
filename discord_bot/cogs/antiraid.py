# path: discord_bot/cogs/antiraid.py

"""
Anti-raid — join-spike detection with an optional temporary lockdown.

A raid is a burst of joins: `threshold` members inside `window` seconds (the
numbers come from one of three sensitivity presets, so nobody has to pick
raw numbers). When one is detected the bot:

  1. posts an alert in the log channel (pinging the staff alert role),
  2. optionally LOCKS DOWN the server by raising its verification level to
     Highest for a few minutes (the previous level is saved and restored),
  3. optionally times out / kicks the accounts that tripped the alarm, and
     anyone else who joins while raid mode is active.

Raid mode ends by itself after the configured number of quiet minutes
(every join during a raid pushes the timer back), or when staff tap
"End lockdown". Because the timer lives in the database, a bot restart
mid-raid never leaves the server locked forever: the 30-second sweeper
(or the first sweep after boot) restores the verification level.

ONE slash command: /antiraid. It opens the setup wizard, which is the same
screen the Server Owners Panel opens from Moderation -> Anti-raid
(`open_antiraid`), so the two entry points can never drift apart.

Every wizard control is a discord.ui.DynamicItem with guild_id/clone_id
encoded in its custom_id, so the wizard survives bot restarts and never
times out — same mechanism as the honeypot and the other wizards.
Registered via bot.add_dynamic_items(*DYNAMIC_ITEMS) in bot.py's setup_hook.

Safety rules:
  * Staff (Manage Server / Ban / Kick / Timeout / Manage Messages /
    Administrator) are never actioned, only counted toward the spike.
  * The only mention the bot can ever send is the one configured alert role.
  * Lockdown only restores the verification level if it is still the level
    WE set — if an admin changed it by hand meanwhile, we leave it alone.
"""

import re
import time
import asyncio
import logging
from collections import deque
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

from database import db
from discord_bot import perm_check
from discord_bot.cogs._dm_support import GuildOnlyCog

logger = logging.getLogger(__name__)

# ── settings the wizard offers ────────────────────────────────────────────

# key -> (label, emoji, joins, seconds, description)
SENSITIVITY = {
    "relaxed":  ("Relaxed", "🌤️", 12, 60, "12 joins in 60s — large, busy servers"),
    "balanced": ("Balanced", "⚖️", 7, 30, "7 joins in 30s — good for most servers"),
    "strict":   ("Strict", "🚨", 4, 15, "4 joins in 15s — small servers, maximum caution"),
}
DEFAULT_SENSITIVITY = "balanced"

# key -> (label, emoji, description)
RESPONSES = {
    "alert":    ("Alert only", "🔔", "Ping staff, change nothing"),
    "lockdown": ("Alert + lockdown", "🔒", "Also raise verification to Highest until it's over"),
}
JOINER_ACTIONS = {
    "none":    ("Leave new joiners alone", "🤝", "Staff decide what to do"),
    "timeout": ("Timeout new joiners (1 hour)", "⏳", "They can read but not talk; staff can lift it"),
    "kick":    ("Kick new joiners", "👢", "Removes them; real people can rejoin later"),
}
LOCKDOWN_MINUTES = (5, 15, 30, 60, 180)
DEFAULT_LOCKDOWN_MINUTES = 15

RAID_TIMEOUT_MINUTES = 60
LOCKDOWN_LEVEL = discord.VerificationLevel.highest
_PERSIST_EVERY = 60.0      # seconds between DB writes while extending an active raid
_CACHE_TTL = 60.0
_TEST_COOLDOWN = 20.0
_MAX_TRACKED = 50          # join records kept per guild (bounds memory)

_cache: dict = {}          # (guild_id, clone_id) -> (expires_at, config)
_joins: dict = {}          # (guild_id, clone_id) -> deque[(monotonic_ts, member_id)]
_starting: set = set()     # guilds with a raid being started right now
_last_persist: dict = {}   # (guild_id, clone_id) -> monotonic time of last extend write
_last_test: dict = {}      # (guild_id, clone_id) -> monotonic time


# ── small helpers ─────────────────────────────────────────────────────────

def _clone_of(client):
    return getattr(client, "clone_id", None)


def _cid(kind: str, guild_id: int, clone_id) -> str:
    return f"antiraid:{kind}:{guild_id}:{'-' if clone_id is None else clone_id}"


def _pat(kinds: str) -> str:
    return rf"^antiraid:(?P<kind>{kinds}):(?P<guild>\d+):(?P<clone>-|\d+)$"


def _ids(match: "re.Match"):
    clone = match.group("clone")
    return int(match.group("guild")), (None if clone == "-" else int(clone))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _invalidate(guild_id: int, clone_id) -> None:
    _cache.pop((guild_id, clone_id), None)


def thresholds(cfg: dict) -> tuple:
    """(joins, seconds) for this config's sensitivity preset."""
    _, _, joins, seconds, _ = SENSITIVITY.get(cfg.get("sensitivity") or DEFAULT_SENSITIVITY,
                                              SENSITIVITY[DEFAULT_SENSITIVITY])
    return joins, seconds


def raid_active(cfg: dict) -> bool:
    until = cfg.get("active_until")
    return bool(until) and until > _now()


async def _cached_config(guild_id: int, clone_id):
    key = (guild_id, clone_id)
    hit = _cache.get(key)
    now = time.monotonic()
    if hit and hit[0] > now:
        return hit[1]
    cfg = None
    try:
        cfg = await db.get_antiraid_config(guild_id, clone_id=clone_id)
    except Exception:
        logger.exception("anti-raid config lookup failed for guild %s", guild_id)
    _cache[key] = (now + _CACHE_TTL, cfg)
    return cfg


def _is_staff(member: discord.Member) -> bool:
    if member.id == member.guild.owner_id:
        return True
    p = member.guild_permissions
    return any((p.administrator, p.manage_guild, p.manage_messages, p.ban_members,
                p.kick_members, p.moderate_members))


def record_join(joins: deque, now: float, member_id: int, window: int, threshold: int):
    """Pure spike detector. Records one join; returns the list of member ids
    in the window when `threshold` joins landed within `window` seconds
    (and clears the window so the same burst can't fire twice), else None."""
    joins.append((now, member_id))
    cutoff = now - window
    while joins and joins[0][0] < cutoff:
        joins.popleft()
    while len(joins) > _MAX_TRACKED:
        joins.popleft()
    if len(joins) >= threshold:
        ids = [m for _, m in joins]
        joins.clear()
        return ids
    return None


def missing_permissions(guild: discord.Guild, cfg: dict) -> list:
    """Server-level permissions the chosen settings need but the bot lacks."""
    me = guild.me
    if me is None:
        return []
    needs = []
    if cfg.get("response") == "lockdown":
        needs.append(("manage_guild", "Manage Server"))
    action = cfg.get("joiner_action")
    if action == "timeout":
        needs.append(("moderate_members", "Timeout Members"))
    elif action == "kick":
        needs.append(("kick_members", "Kick Members"))
    return [label for perm, label in needs if not getattr(me.guild_permissions, perm, False)]


async def resolve_log_channel(guild: discord.Guild, clone_id, cfg: dict):
    """The anti-raid log channel, else the server's mod-log channel, else None."""
    channel_id = cfg.get("log_channel_id")
    if not channel_id:
        try:
            automod = await db.get_automod_config(guild.id, clone_id=clone_id)
            channel_id = automod.get("log_channel_id")
        except Exception:
            channel_id = None
    return guild.get_channel(channel_id) if channel_id else None


def alert_role_of(guild: discord.Guild, cfg: dict):
    rid = cfg.get("alert_role_id")
    role = guild.get_role(int(rid)) if rid else None
    if role is None or role.is_default():
        return None
    return role


async def _send_log(guild: discord.Guild, clone_id, cfg: dict, embed: discord.Embed,
                    content: str | None = None, ping_role: discord.Role | None = None) -> bool:
    channel = await resolve_log_channel(guild, clone_id, cfg)
    if channel is None:
        return False
    # Only the one configured alert role can ever be pinged; never @everyone or users.
    allowed = (discord.AllowedMentions(everyone=False, users=False, roles=[ping_role])
               if ping_role is not None else discord.AllowedMentions.none())
    try:
        await channel.send(content=content, embed=embed, allowed_mentions=allowed)
        return True
    except (discord.Forbidden, discord.HTTPException):
        return False


# ── raid lifecycle ────────────────────────────────────────────────────────

async def apply_joiner_action(guild: discord.Guild, member: discord.Member, cfg: dict):
    """Returns True (done), False (tried and failed), or None (nothing to do)."""
    action = cfg.get("joiner_action") or "none"
    if action == "none" or _is_staff(member):
        return None
    perm = "moderate_members" if action == "timeout" else "kick_members"
    try:
        if perm_check.member_problem(guild, member, perm, "act on"):
            return False
        if action == "timeout":
            await member.timeout(timedelta(minutes=RAID_TIMEOUT_MINUTES), reason="[anti-raid] joined during a raid")
        else:
            await guild.kick(member, reason="[anti-raid] joined during a raid")
        return True
    except Exception:
        # One joiner we can't act on must never stop the alert or the other joiners.
        logger.debug("anti-raid couldn't act on %s in guild %s", getattr(member, "id", "?"), guild.id, exc_info=True)
        return False


def _raid_embed(title: str, description: str, color: discord.Color) -> discord.Embed:
    return discord.Embed(title=title, description=description, color=color)


async def start_raid(bot, guild: discord.Guild, joined_ids: list | None = None,
                     actor=None, force_lockdown: bool = False) -> tuple:
    """Begin raid mode: lock down (if configured), act on the joiners that tripped
    the alarm, alert staff. Never raises. Returns (started, message)."""
    clone_id = _clone_of(bot)
    key = (guild.id, clone_id)
    if key in _starting:
        return False, "A raid is already being handled."
    _starting.add(key)
    try:
        cfg = await db.get_antiraid_config(guild.id, clone_id=clone_id)
        manual = actor is not None
        if raid_active(cfg):
            return False, "Raid mode is already active."
        lockdown = force_lockdown or cfg.get("response") == "lockdown"
        minutes = int(cfg.get("lockdown_minutes") or DEFAULT_LOCKDOWN_MINUTES)

        notes = []
        prev_level = None
        if lockdown:
            current = guild.verification_level
            if current.value >= LOCKDOWN_LEVEL.value:
                notes.append("🔒 Verification was already at Highest.")
            else:
                try:
                    await guild.edit(verification_level=LOCKDOWN_LEVEL,
                                     reason="[anti-raid] lockdown" + (f" started by {actor}" if actor else ""))
                    prev_level = current.value
                    notes.append(f"🔒 Verification raised to **Highest** for **{minutes} min**.")
                    perm_check.clear(guild.id, clone_id, "antiraid")
                except (discord.Forbidden, discord.HTTPException):
                    notes.append("⚠️ I couldn't raise verification — " + perm_check.forbidden_hint(guild, "manage_guild"))
                    perm_check.flag(guild.id, clone_id, "antiraid",
                                    "Anti-raid couldn't lock the server down — give me **Manage Server**.")

        until = _now() + timedelta(minutes=minutes)
        cfg = await db.set_antiraid_config(guild.id, clone_id=clone_id, active_until=until,
                                           prev_verification=prev_level)
        try:
            await db.bump_antiraid_triggers(guild.id, clone_id=clone_id)
        except Exception:
            logger.exception("anti-raid counter bump failed for guild %s", guild.id)
        _cache[key] = (time.monotonic() + _CACHE_TTL, cfg)
        _last_persist[key] = time.monotonic()

        acted = failed = 0
        for uid in dict.fromkeys(joined_ids or []):
            member = guild.get_member(uid)
            if member is None:
                continue
            result = await apply_joiner_action(guild, member, cfg)
            if result is True:
                acted += 1
            elif result is False:
                failed += 1
        action = cfg.get("joiner_action") or "none"
        if action != "none":
            verb = "timed out" if action == "timeout" else "kicked"
            notes.append(f"{JOINER_ACTIONS[action][1]} {acted} account(s) {verb}"
                         + (f", **{failed}** I couldn't act on" if failed else "") + ". "
                         "New joiners get the same treatment until it's over.")

        joins, window = thresholds(cfg)
        who = f"Started by {actor.mention}." if manual else \
            f"**{len(joined_ids or [])}** members joined within {window}s (alarm is {joins} in {window}s)."
        embed = _raid_embed("🚨 Raid mode ON" if lockdown else "🚨 Possible raid detected",
                            who + "\n" + "\n".join(notes or ["No automatic action — alert only."]),
                            discord.Color.red())
        embed.add_field(name="Ends", value=f"<t:{int(until.timestamp())}:R> if joins stop", inline=True)
        embed.add_field(name="Stop it early", value="`/antiraid` → **End lockdown**", inline=True)
        role = alert_role_of(guild, cfg)
        sent = await _send_log(guild, clone_id, cfg, embed,
                               content=(f"{role.mention} 🚨 possible raid in progress." if role else None),
                               ping_role=role)
        if not sent:
            perm_check.flag(guild.id, clone_id, "antiraid_log",
                            "Anti-raid has nowhere to post alerts — pick a log channel in `/antiraid`.")
        return True, "\n".join(notes) or "Raid mode started (alert only)."
    except Exception:
        logger.exception("anti-raid start failed for guild %s", guild.id)
        return False, "Something went wrong starting raid mode — check the bot logs."
    finally:
        _starting.discard(key)


async def end_raid(bot, guild: discord.Guild, actor=None) -> tuple:
    """End raid mode and put the verification level back. Never raises."""
    clone_id = _clone_of(bot)
    key = (guild.id, clone_id)
    try:
        cfg = await db.get_antiraid_config(guild.id, clone_id=clone_id)
        if not cfg.get("active_until"):
            return False, "Raid mode isn't active."
        restored = None
        prev = cfg.get("prev_verification")
        # Only undo OUR change; if an admin moved the level meanwhile, respect that.
        if prev is not None and guild.verification_level == LOCKDOWN_LEVEL:
            try:
                await guild.edit(verification_level=discord.VerificationLevel(prev),
                                 reason="[anti-raid] lockdown ended")
                restored = discord.VerificationLevel(prev).name.replace("_", " ").title()
            except (discord.Forbidden, discord.HTTPException):
                perm_check.flag(guild.id, clone_id, "antiraid",
                                "Anti-raid couldn't restore the verification level — set it back by hand in Server Settings.")
        cfg = await db.set_antiraid_config(guild.id, clone_id=clone_id, active_until=None, prev_verification=None)
        _cache[key] = (time.monotonic() + _CACHE_TTL, cfg)
        _joins.pop(key, None)
        _last_persist.pop(key, None)
        line = (f"Verification put back to **{restored}**." if restored
                else "Verification level left as it is.")
        embed = _raid_embed("✅ Raid mode ended",
                            (f"Ended by {actor.mention}. " if actor else "No suspicious joins for a while. ") + line,
                            discord.Color.green())
        await _send_log(guild, clone_id, cfg, embed)
        return True, line
    except Exception:
        logger.exception("anti-raid end failed for guild %s", guild.id)
        return False, "Something went wrong ending raid mode — check the bot logs."


async def handle_join(bot, member: discord.Member) -> None:
    """Called for every member join. Cheap when the feature is off."""
    guild = member.guild
    clone_id = _clone_of(bot)
    cfg = await _cached_config(guild.id, clone_id)
    if not cfg or not cfg.get("enabled"):
        return
    key = (guild.id, clone_id)

    if raid_active(cfg):                      # raid in progress: act on joiner, push the timer back
        await apply_joiner_action(guild, member, cfg)
        now = time.monotonic()
        if now - _last_persist.get(key, 0.0) >= _PERSIST_EVERY:
            _last_persist[key] = now
            minutes = int(cfg.get("lockdown_minutes") or DEFAULT_LOCKDOWN_MINUTES)
            try:
                fresh = await db.set_antiraid_config(guild.id, clone_id=clone_id,
                                                     active_until=_now() + timedelta(minutes=minutes))
                _cache[key] = (now + _CACHE_TTL, fresh)
            except Exception:
                logger.exception("anti-raid extend failed for guild %s", guild.id)
        return

    joins, window = thresholds(cfg)
    ids = record_join(_joins.setdefault(key, deque()), time.monotonic(), member.id, window, joins)
    if ids:
        await start_raid(bot, guild, joined_ids=ids)


async def send_test_alert(bot, guild: discord.Guild, tester: discord.abc.User) -> tuple:
    """Posts a SAMPLE alert (with the real role ping). Changes nothing. Returns (ok, message)."""
    clone_id = _clone_of(bot)
    cfg = await db.get_antiraid_config(guild.id, clone_id=clone_id)
    now = time.monotonic()
    last = _last_test.get((guild.id, clone_id))
    if last is not None and now - last < _TEST_COOLDOWN:
        return False, f"Easy — try again in {int(_TEST_COOLDOWN - (now - last)) + 1}s."
    joins, window = thresholds(cfg)
    embed = _raid_embed(
        "🧪 Anti-raid TEST — nothing was changed",
        f"This is what a real alert looks like. Sent by {tester.mention}.\n"
        f"Alarm: **{joins} joins in {window}s** · Response: "
        f"{RESPONSES.get(cfg.get('response'), RESPONSES['lockdown'])[0]} · "
        f"Joiners: {JOINER_ACTIONS.get(cfg.get('joiner_action'), JOINER_ACTIONS['none'])[0]}",
        discord.Color.blurple())
    embed.set_thumbnail(url=tester.display_avatar.url)
    role = alert_role_of(guild, cfg)
    _last_test[(guild.id, clone_id)] = now
    sent = await _send_log(guild, clone_id, cfg, embed,
                           content=(f"{role.mention} 🧪 test alert — ignore." if role else None), ping_role=role)
    if not sent:
        return False, ("I couldn't post it. Pick a log channel (or set a mod-log channel) and make sure I can "
                       "view, send and embed links there.")
    channel = await resolve_log_channel(guild, clone_id, cfg)
    return True, (f"Sent a test alert to {channel.mention}" +
                  (f", pinging {role.mention}." if role else ". No alert role is set, so nobody was pinged."))


# ── wizard panel ──────────────────────────────────────────────────────────

def _step(done: bool) -> str:
    return "✅" if done else "⬜"


def status_lines(guild: discord.Guild, cfg: dict, log_channel) -> list:
    sens = SENSITIVITY.get(cfg.get("sensitivity") or DEFAULT_SENSITIVITY, SENSITIVITY[DEFAULT_SENSITIVITY])
    resp = RESPONSES.get(cfg.get("response") or "lockdown", RESPONSES["lockdown"])
    joiner = JOINER_ACTIONS.get(cfg.get("joiner_action") or "none", JOINER_ACTIONS["none"])
    minutes = int(cfg.get("lockdown_minutes") or DEFAULT_LOCKDOWN_MINUTES)
    role = alert_role_of(guild, cfg)

    if raid_active(cfg):
        until = int(cfg["active_until"].timestamp())
        state = f"🚨 **RAID MODE ACTIVE** — ends <t:{until}:R> if joins stop"
    elif cfg.get("enabled"):
        state = "🟢 **Watching** for join spikes"
    else:
        state = "⏸️ **Off** — finish the steps below, then tap **Turn on**"

    lines = [
        f"**Status:** {state}",
        "",
        f"{_step(True)} **1. Sensitivity** — {sens[1]} {sens[0]}: {sens[4]}",
        f"{_step(True)} **2. When a raid hits** — {resp[1]} {resp[0]}"
        + (f" (back to normal after **{minutes} min** of calm)" if cfg.get("response") == "lockdown" else ""),
        f"{_step(True)} **3. New joiners during a raid** — {joiner[1]} {joiner[0]}",
        f"{_step(bool(log_channel))} **4. Where alerts go** — "
        + (log_channel.mention if log_channel else "*not set — pick a channel below*")
        + (f" · pings {role.mention}" if role else " · no staff role pinged"),
    ]
    n = int(cfg.get("triggered_count") or 0)
    if n:
        last = cfg.get("last_triggered_at")
        lines.append(f"\n**Raids caught:** {n}" + (f" · last <t:{int(last.timestamp())}:R>" if last else ""))
    miss = missing_permissions(guild, cfg)
    if miss:
        lines.append(f"⚠️ **I'm missing:** {', '.join(miss)}")
    return lines


def build_panel(guild: discord.Guild, clone_id, cfg: dict, log_channel, note: str = "") -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=None)
    if raid_active(cfg):
        color = discord.Color.red()
    elif cfg.get("enabled"):
        color = discord.Color.green()
    else:
        color = discord.Color.orange()
    container = discord.ui.Container(accent_colour=color)
    if note:   # a Components v2 message can't carry plain `content`, so the note lives inside the panel
        container.add_item(discord.ui.TextDisplay(note))
    container.add_item(discord.ui.TextDisplay(
        "### 🛡️ Anti-raid setup\n"
        + "\n".join(perm_check.lines(guild.id, clone_id) + status_lines(guild, cfg, log_channel))
        + "\n\n-# Staff are never actioned. Raid mode ends on its own; **End lockdown** stops it early."
    ))
    container.add_item(discord.ui.Separator())

    for item in (
        AntiRaidSelect("sens", guild.id, clone_id, cfg.get("sensitivity") or DEFAULT_SENSITIVITY),
        AntiRaidSelect("resp", guild.id, clone_id, cfg.get("response") or "lockdown"),
        AntiRaidSelect("joiner", guild.id, clone_id, cfg.get("joiner_action") or "none"),
        AntiRaidSelect("dur", guild.id, clone_id, str(int(cfg.get("lockdown_minutes") or DEFAULT_LOCKDOWN_MINUTES))),
        AntiRaidLogSelect(guild.id, clone_id),
        AntiRaidRoleSelect(guild.id, clone_id),
    ):
        row = discord.ui.ActionRow()
        row.add_item(item)
        container.add_item(row)

    btns = discord.ui.ActionRow()
    btns.add_item(AntiRaidButton("toggle", guild.id, clone_id, enabled=bool(cfg.get("enabled"))))
    if raid_active(cfg):
        btns.add_item(AntiRaidButton("unlock", guild.id, clone_id))
    else:
        btns.add_item(AntiRaidButton("lockdown", guild.id, clone_id))
    btns.add_item(AntiRaidButton("test", guild.id, clone_id))
    btns.add_item(AntiRaidButton("panel", guild.id, clone_id))
    container.add_item(btns)

    view.add_item(container)
    return view


async def _reply(interaction: discord.Interaction, text: str) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(text, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
    else:
        await interaction.response.send_message(text, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


async def _authorize(interaction: discord.Interaction, guild_id: int):
    """Resolves the guild from the custom_id and checks Manage Server / owner.
    Returns the guild, or None after telling the user why."""
    guild = interaction.client.get_guild(guild_id)
    if guild is None:
        await _reply(interaction, "I'm not in that server anymore.")
        return None
    member = guild.get_member(interaction.user.id)
    if member is None or not (member.guild_permissions.manage_guild or member == guild.owner):
        await _reply(interaction, "You need the **Manage Server** permission to use anti-raid.")
        return None
    return guild


async def _rerender(interaction: discord.Interaction, guild: discord.Guild, clone_id, note: str = "") -> None:
    if not interaction.response.is_done():
        await interaction.response.defer()
    cfg = await db.get_antiraid_config(guild.id, clone_id=clone_id)
    log_channel = await resolve_log_channel(guild, clone_id, cfg)
    await interaction.edit_original_response(view=build_panel(guild, clone_id, cfg, log_channel, note=note))


async def _save(interaction, guild: discord.Guild, clone_id, **fields) -> dict:
    """Audited write (same trail as every other panel setting) + cache refresh."""
    from modules import server_panel as sp
    await sp.set_antiraid(guild.id, clone_id, interaction.user.id, **fields)
    _invalidate(guild.id, clone_id)
    return await db.get_antiraid_config(guild.id, clone_id=clone_id)


async def open_antiraid(interaction: discord.Interaction, guild: discord.Guild, clone_id) -> None:
    """Shared by /antiraid and the Server Panel's Moderation -> Anti-raid button.
    Call AFTER interaction.response.defer(ephemeral=True) and after _authorize()."""
    cfg = await db.get_antiraid_config(guild.id, clone_id=clone_id)
    log_channel = await resolve_log_channel(guild, clone_id, cfg)
    note = "" if cfg.get("enabled") else "👋 Anti-raid isn't on yet — go through the steps, then tap **Turn on**."
    await interaction.followup.send(view=build_panel(guild, clone_id, cfg, log_channel, note=note), ephemeral=True)


# ── dynamic items ─────────────────────────────────────────────────────────

def _select_options(kind: str, current: str) -> tuple:
    """(placeholder, [SelectOption])"""
    if kind == "sens":
        return "1. How sensitive should detection be?", [
            discord.SelectOption(label=v[0], value=k, emoji=v[1], description=v[4], default=(k == current))
            for k, v in SENSITIVITY.items()]
    if kind == "resp":
        return "2. What should I do when a raid hits?", [
            discord.SelectOption(label=v[0], value=k, emoji=v[1], description=v[2], default=(k == current))
            for k, v in RESPONSES.items()]
    if kind == "joiner":
        return "3. What about accounts joining during a raid?", [
            discord.SelectOption(label=v[0], value=k, emoji=v[1], description=v[2], default=(k == current))
            for k, v in JOINER_ACTIONS.items()]
    return "How long until things go back to normal?", [
        discord.SelectOption(label=f"{m} minutes of calm" if m < 60 else f"{m // 60} hour(s) of calm",
                             value=str(m), default=(str(m) == current))
        for m in LOCKDOWN_MINUTES]


class AntiRaidSelect(discord.ui.DynamicItem[discord.ui.Select], template=_pat("sens|resp|joiner|dur")):
    def __init__(self, kind: str, guild_id: int, clone_id, current: str = ""):
        self.kind, self.guild_id, self.clone_id = kind, guild_id, clone_id
        placeholder, options = _select_options(kind, current)
        super().__init__(discord.ui.Select(placeholder=placeholder, options=options, min_values=1, max_values=1,
                                           custom_id=_cid(kind, guild_id, clone_id)))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        guild_id, clone_id = _ids(match)
        return cls(match.group("kind"), guild_id, clone_id)

    async def callback(self, interaction: discord.Interaction):
        guild = await _authorize(interaction, self.guild_id)
        if guild is None:
            return
        await interaction.response.defer()
        value = self.item.values[0]
        field = {"sens": "sensitivity", "resp": "response", "joiner": "joiner_action", "dur": "lockdown_minutes"}[self.kind]
        valid = {"sens": SENSITIVITY, "resp": RESPONSES, "joiner": JOINER_ACTIONS,
                 "dur": {str(m): 1 for m in LOCKDOWN_MINUTES}}[self.kind]
        if value not in valid:
            return
        cfg = await _save(interaction, guild, self.clone_id,
                          **{field: int(value) if self.kind == "dur" else value})
        await _rerender(interaction, guild, self.clone_id)
        miss = missing_permissions(guild, cfg)
        if miss:
            await interaction.followup.send(
                f"Heads up — I'm missing **{', '.join(miss)}**, so I can't do that yet. Enable it on my role.",
                ephemeral=True)


class AntiRaidLogSelect(discord.ui.DynamicItem[discord.ui.ChannelSelect], template=_pat("log")):
    def __init__(self, guild_id: int, clone_id):
        self.guild_id, self.clone_id = guild_id, clone_id
        super().__init__(discord.ui.ChannelSelect(
            placeholder="4. Where should I post raid alerts?",
            channel_types=[discord.ChannelType.text], min_values=1, max_values=1,
            custom_id=_cid("log", guild_id, clone_id)))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(*_ids(match))

    async def callback(self, interaction: discord.Interaction):
        guild = await _authorize(interaction, self.guild_id)
        if guild is None:
            return
        await interaction.response.defer()
        picked = guild.get_channel(self.item.values[0].id)
        problem = (perm_check.channel_problem(picked, guild.me, ("view_channel", "send_messages", "embed_links"))
                   if picked else None)
        if problem:
            await interaction.followup.send(problem, ephemeral=True)
            return
        await _save(interaction, guild, self.clone_id, log_channel_id=picked.id)
        await _rerender(interaction, guild, self.clone_id)


class AntiRaidRoleSelect(discord.ui.DynamicItem[discord.ui.RoleSelect], template=_pat("role")):
    def __init__(self, guild_id: int, clone_id):
        self.guild_id, self.clone_id = guild_id, clone_id
        super().__init__(discord.ui.RoleSelect(
            placeholder="Staff role to ping during a raid (optional)",
            min_values=1, max_values=1, custom_id=_cid("role", guild_id, clone_id)))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(*_ids(match))

    async def callback(self, interaction: discord.Interaction):
        guild = await _authorize(interaction, self.guild_id)
        if guild is None:
            return
        role = guild.get_role(self.item.values[0].id)
        if role is None or role.is_default():
            await _reply(interaction, "Pick a real role — @everyone can't be the alert role.")
            return
        await interaction.response.defer()
        await _save(interaction, guild, self.clone_id, alert_role_id=role.id)
        await _rerender(interaction, guild, self.clone_id)
        if not role.mentionable and not guild.me.guild_permissions.mention_everyone:
            await interaction.followup.send(
                f"Heads up — {role.mention} isn't mentionable and I don't have **Mention Everyone**, "
                "so Discord won't ping it. Make the role mentionable or give me that permission.",
                ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


_BUTTONS = {
    "toggle":   ("Turn on", discord.ButtonStyle.success, "▶️"),
    "lockdown": ("Lockdown now", discord.ButtonStyle.danger, "🔒"),
    "unlock":   ("End lockdown", discord.ButtonStyle.success, "🔓"),
    "test":     ("Send test alert", discord.ButtonStyle.secondary, "🧪"),
    "panel":    ("Open server panel", discord.ButtonStyle.primary, "🛠️"),
}


class AntiRaidButton(discord.ui.DynamicItem[discord.ui.Button], template=_pat("toggle|lockdown|unlock|test|panel")):
    def __init__(self, kind: str, guild_id: int, clone_id, enabled: bool = False):
        self.kind, self.guild_id, self.clone_id = kind, guild_id, clone_id
        label, style, emoji = _BUTTONS[kind]
        if kind == "toggle" and enabled:
            label, style, emoji = "Pause", discord.ButtonStyle.secondary, "⏸️"
        super().__init__(discord.ui.Button(label=label, style=style, emoji=emoji,
                                           custom_id=_cid(kind, guild_id, clone_id)))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        guild_id, clone_id = _ids(match)
        return cls(match.group("kind"), guild_id, clone_id)

    async def callback(self, interaction: discord.Interaction):
        guild = await _authorize(interaction, self.guild_id)
        if guild is None:
            return
        clone_id = self.clone_id

        if self.kind == "panel":
            if interaction.guild is None or interaction.guild.id != guild.id:
                await _reply(interaction, "Open your server and run `/serversetup` there to open the panel.")
                return
            from discord_bot.cogs._views_server_panel import open_home
            await open_home(interaction)

        elif self.kind == "test":
            await interaction.response.defer(ephemeral=True)
            ok, msg = await send_test_alert(interaction.client, guild, interaction.user)
            await interaction.followup.send(("✅ " if ok else "⚠️ ") + msg, ephemeral=True,
                                            allowed_mentions=discord.AllowedMentions.none())

        elif self.kind == "toggle":
            await interaction.response.defer()
            cfg = await db.get_antiraid_config(guild.id, clone_id=clone_id)
            turning_on = not cfg.get("enabled")
            if turning_on and await resolve_log_channel(guild, clone_id, cfg) is None:
                await interaction.followup.send(
                    "Pick a **log channel** (step 4) first — a raid alert with nowhere to go is useless.",
                    ephemeral=True)
                return
            await _save(interaction, guild, clone_id, enabled=turning_on, created_by=interaction.user.id)
            _joins.pop((guild.id, clone_id), None)
            await _rerender(interaction, guild, clone_id)
            miss = missing_permissions(guild, await db.get_antiraid_config(guild.id, clone_id=clone_id))
            if turning_on and miss:
                await interaction.followup.send(
                    f"Anti-raid is on, but I'm missing **{', '.join(miss)}** — enable it on my role so I can act.",
                    ephemeral=True)

        elif self.kind == "lockdown":
            await interaction.response.defer()
            ok, msg = await start_raid(interaction.client, guild, actor=interaction.user, force_lockdown=True)
            await _rerender(interaction, guild, clone_id)
            await interaction.followup.send(("🔒 " if ok else "⚠️ ") + msg, ephemeral=True)

        elif self.kind == "unlock":
            await interaction.response.defer()
            ok, msg = await end_raid(interaction.client, guild, actor=interaction.user)
            await _rerender(interaction, guild, clone_id)
            await interaction.followup.send(("🔓 " if ok else "⚠️ ") + msg, ephemeral=True)


DYNAMIC_ITEMS = (AntiRaidSelect, AntiRaidLogSelect, AntiRaidRoleSelect, AntiRaidButton)


# ── cog ───────────────────────────────────────────────────────────────────

class AntiRaidCog(GuildOnlyCog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        self.sweeper.start()

    async def cog_unload(self):
        self.sweeper.cancel()

    def _clone_id(self):
        return _clone_of(self.bot)

    @app_commands.command(name="antiraid", description="Set up raid protection: spike detection, lockdown and staff alerts")
    @app_commands.guild_only()
    async def antiraid_cmd(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        guild = await _authorize(interaction, interaction.guild_id)
        if guild is None:
            return
        await open_antiraid(interaction, guild, self._clone_id())

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        try:
            await handle_join(self.bot, member)
        except Exception:
            logger.exception("anti-raid join handling failed for guild %s", member.guild.id)

    @tasks.loop(seconds=30)
    async def sweeper(self):
        """Ends raid modes whose quiet timer ran out (also recovers after a restart)."""
        try:
            expired = await db.list_expired_antiraid(self._clone_id())
        except Exception:
            logger.exception("anti-raid sweep query failed")
            return
        for row in expired:
            guild = self.bot.get_guild(row["guild_id"])
            if guild is None:
                continue
            await end_raid(self.bot, guild)

    @sweeper.before_loop
    async def _before_sweeper(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(AntiRaidCog(bot))
