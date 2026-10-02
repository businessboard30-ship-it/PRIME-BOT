# path: discord_bot/cogs/honeypot.py

"""
Honeypot — a trap channel for spam/scam bots and hacked accounts (FREE core, premium extras).

Spam bots and compromised accounts blast the same "free nitro" link into every
channel they can see. A real member reads the warning and never posts in the
trap channel, so ANYONE who does is treated as compromised. Regular members get
the configured action applied automatically; staff (admins, mods) are NOT exempt
but get a WARNING instead (message removed, short notice, logged) so an admin
poking the channel is never banned by accident:

  ban      ban + wipe their recent messages server-wide (default)
  kick     kick; if "delete history" is on it's a soft-ban (ban+unban) so their
           recent messages across the server are wiped too
  timeout  28-day timeout (Discord's max) + the trap message is removed

Entry points (all share `open_honeypot`, so they can never drift apart):
  * the "Honeypot" button on page 1 of the combined join DM
    (_views_join_dm.py -> _HoneypotButton)
  * the /honeypot slash command

Tapping either AUTO-CREATES the trap channel (reusing the configured one, or an
existing #honeypot, before making a new one), posts the "do not post here"
notice inside it, and opens an admin-only settings panel.

Every button/select is a discord.ui.DynamicItem with guild_id/clone_id encoded
in its custom_id, so the panel survives bot restarts and never times out —
same mechanism as the join DM and the other wizards. Registered via
bot.add_dynamic_items(*DYNAMIC_ITEMS) in bot.py's setup_hook.

Free for every server: setting up the trap, enforcement (ban + wipe the last 24h
of messages), pause/resume, repost, remove, the catch counter, and logging to the
server's mod-log channel.

Staff alerts and stats (free): pick a role and it is pinged in the log channel on
every catch (and on a failed action, which needs a human). The panel shows catches
over the last 24h / 7d / 30d, and a test button sends a sample alert without
touching anyone. Stats come from discord_honeypot_catches (90-day retention).

Premium extras (per server): choosing the action (kick / 28-day timeout), choosing
how much history to wipe, and a dedicated log channel. If premium lapses, the
saved extras are kept but not used: the trap keeps protecting the server with the
free defaults, and re-activates the saved extras when premium returns.
"""

import re
import time
import asyncio
import logging
from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands

from database import db
from discord_bot import perm_check
from discord_bot.cogs._dm_support import GuildOnlyCog

logger = logging.getLogger(__name__)

CHANNEL_NAME = "honeypot"
TIMEOUT_DAYS = 28  # Discord's maximum timeout length

ACTIONS = {
    "ban": ("Ban", "🔨", "banned"),
    "kick": ("Kick", "👢", "kicked"),
    "timeout": ("Timeout (28 days)", "⏳", f"timed out for {TIMEOUT_DAYS} days"),
}
DELETE_OPTIONS = {
    0: "Don't delete their other messages",
    3600: "Delete their last hour of messages",
    86400: "Delete their last 24 hours of messages",
    259200: "Delete their last 3 days of messages",
    604800: "Delete their last 7 days of messages",
}

FREE_ACTION = "ban"            # what a non-premium server's trap does
FREE_DELETE_SECONDS = 86400    # ...and how much history it wipes (24h)
STAFF_WARNING_SECONDS = 15     # how long the in-channel staff warning stays up
_STAFF_WARN_COOLDOWN = 30.0    # seconds between warnings for the same staff member

_CACHE_TTL = 60.0
_cache: dict = {}        # (guild_id, clone_id) -> (expires_at, config | None)
_tripping: set = set()   # (guild_id, user_id) currently being actioned
_warned: dict = {}       # (guild_id, user_id) -> monotonic time of last staff warning


# ── small helpers ─────────────────────────────────────────────────────────

def _clone_of(client):
    return getattr(client, "clone_id", None)


def _cid(kind: str, guild_id: int, clone_id) -> str:
    return f"honeypot:{kind}:{guild_id}:{'-' if clone_id is None else clone_id}"


def _pat(kinds: str) -> str:
    return rf"^honeypot:(?P<kind>{kinds}):(?P<guild>\d+):(?P<clone>-|\d+)$"


def _ids(match: "re.Match"):
    clone = match.group("clone")
    return int(match.group("guild")), (None if clone == "-" else int(clone))


def _invalidate(guild_id: int, clone_id) -> None:
    _cache.pop((guild_id, clone_id), None)


async def _cached_config(guild_id: int, clone_id):
    key = (guild_id, clone_id)
    hit = _cache.get(key)
    now = time.monotonic()
    if hit and hit[0] > now:
        return hit[1]
    cfg = None
    try:
        row = await db.get_honeypot_config(guild_id, clone_id=clone_id)
        cfg = row if row.get("channel_id") else None
    except Exception:
        logger.exception("honeypot config lookup failed for guild %s", guild_id)
    _cache[key] = (now + _CACHE_TTL, cfg)
    return cfg


async def is_premium(guild_id: int, clone_id) -> bool:
    try:
        return bool(await db.is_guild_premium_active(guild_id, clone_id))
    except Exception:
        return False


def effective_config(cfg: dict, premium: bool) -> dict:
    """The settings the trap actually uses. Premium servers get what they saved;
    free servers get the free defaults (saved extras are kept, just not used)."""
    if premium:
        return cfg
    out = dict(cfg)
    out["action"] = FREE_ACTION
    out["delete_seconds"] = FREE_DELETE_SECONDS
    out["log_channel_id"] = None   # falls back to the server's mod-log channel
    return out


def _extras_paused(cfg: dict) -> bool:
    """True when a lapsed server has saved premium choices that aren't being used."""
    return ((cfg.get("action") or FREE_ACTION) != FREE_ACTION
            or int(cfg.get("delete_seconds") or 0) != FREE_DELETE_SECONDS
            or bool(cfg.get("log_channel_id")))


def _is_staff(member: discord.Member) -> bool:
    """Staff are warned, not actioned, so an admin poking the channel is never banned by accident."""
    if member.id == member.guild.owner_id:
        return True
    p = member.guild_permissions
    return any((p.administrator, p.manage_guild, p.manage_messages, p.ban_members,
                p.kick_members, p.moderate_members))


def missing_permissions(guild: discord.Guild, action: str, creating: bool = False) -> list:
    me = guild.me
    if me is None:
        return []
    needs = ["manage_messages"]
    needs.append({"ban": "ban_members", "kick": "ban_members", "timeout": "moderate_members"}[action])
    if action == "kick":
        needs = ["manage_messages", "kick_members"]
    if creating:
        needs.append("manage_channels")
    return [perm_check.LABELS.get(p, p) for p in needs if not getattr(me.guild_permissions, p, False)]


def _warning_view(action: str) -> discord.ui.LayoutView:
    verb = ACTIONS[action][2]
    view = discord.ui.LayoutView(timeout=None)
    container = discord.ui.Container(accent_colour=discord.Color.red())
    container.add_item(discord.ui.TextDisplay(
        "## 🍯 DO NOT POST HERE\n"
        "This channel is a **trap for spam bots and hacked accounts**.\n"
        f"Anyone who sends a message in this channel is automatically **{verb}**.\n\n"
        "-# Real member? Just ignore this channel — nothing here is for you. "
        "If your account got hacked, secure it (change your password, turn on 2FA) "
        "and contact the server staff."
    ))
    view.add_item(container)
    return view


# ── channel + warning notice (the "auto create when used" part) ───────────

async def _refresh_warning(guild: discord.Guild, cfg: dict) -> int | None:
    """Edit the existing notice in place, or post a fresh one if it's gone.
    Returns the (possibly new) message id."""
    channel = guild.get_channel(cfg.get("channel_id") or 0)
    if channel is None:
        return None
    view = _warning_view(cfg.get("action") or "ban")
    msg_id = cfg.get("warning_message_id")
    if msg_id:
        try:
            msg = await channel.fetch_message(int(msg_id))
            await msg.edit(view=view)
            return msg.id
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass
    try:
        msg = await channel.send(view=view)
    except (discord.Forbidden, discord.HTTPException):
        return None
    try:
        await msg.pin(reason="Honeypot notice")
    except (discord.Forbidden, discord.HTTPException):
        pass
    return msg.id


def _find_existing_channel(guild: discord.Guild):
    for ch in guild.text_channels:
        if ch.name.lower().strip("-_ ") in (CHANNEL_NAME, f"{CHANNEL_NAME}s"):
            return ch
    return None


async def ensure_honeypot(guild: discord.Guild, clone_id, user) -> tuple:
    """Make sure a honeypot channel exists and is configured. Reuses the
    configured channel, then an existing #honeypot, before creating one.
    Returns (config, channel, created, error_message)."""
    cfg = await db.get_honeypot_config(guild.id, clone_id=clone_id)
    channel = guild.get_channel(cfg["channel_id"]) if cfg.get("channel_id") else None
    created = False

    if channel is None:
        channel = _find_existing_channel(guild)
        if channel is None:
            if not guild.me.guild_permissions.manage_channels:
                return cfg, None, False, (
                    "I need the **Manage Channels** permission to create the honeypot channel — "
                    "grant it, then tap Honeypot again."
                )
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(
                    view_channel=True, send_messages=True, read_message_history=True,
                    add_reactions=False, use_application_commands=False,
                    create_public_threads=False, create_private_threads=False,
                    send_messages_in_threads=False,
                ),
                guild.me: discord.PermissionOverwrite(
                    view_channel=True, send_messages=True, manage_messages=True,
                    embed_links=True, read_message_history=True,
                ),
            }
            try:
                channel = await guild.create_text_channel(
                    CHANNEL_NAME, overwrites=overwrites,
                    topic="🍯 Trap for spam bots & hacked accounts — DO NOT POST HERE. Posting = automatic action.",
                    reason=f"Honeypot set up by {user}",
                )
            except discord.Forbidden:
                return cfg, None, False, "Discord wouldn't let me create the channel — check my permissions."
            except discord.HTTPException:
                return cfg, None, False, "Discord rejected the channel creation — try again in a moment."
            created = True

    problem = perm_check.channel_problem(channel, guild.me, ("view_channel", "send_messages", "manage_messages"))
    if problem:
        return cfg, channel, created, problem

    cfg = await db.set_honeypot_config(
        guild.id, clone_id=clone_id, channel_id=channel.id, enabled=True,
        created_by=getattr(user, "id", None),
        channel_auto_created=created or bool(cfg.get("channel_auto_created")),
        warning_message_id=cfg.get("warning_message_id") if channel.id == cfg.get("channel_id") else None,
    )
    warn_id = await _refresh_warning(guild, cfg)
    if warn_id != cfg.get("warning_message_id"):
        cfg = await db.set_honeypot_config(guild.id, clone_id=clone_id, warning_message_id=warn_id)
    _invalidate(guild.id, clone_id)
    return cfg, channel, created, None


# ── enforcement ───────────────────────────────────────────────────────────

async def _send_log(guild: discord.Guild, clone_id, cfg: dict, embed: discord.Embed,
                    content: str | None = None, ping_role: discord.Role | None = None) -> bool:
    channel_id = cfg.get("log_channel_id")
    if not channel_id:
        try:
            automod = await db.get_automod_config(guild.id, clone_id=clone_id)
            channel_id = automod.get("log_channel_id")
        except Exception:
            channel_id = None
    channel = guild.get_channel(channel_id) if channel_id else None
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


def alert_role_of(guild: discord.Guild, cfg: dict) -> discord.Role | None:
    rid = cfg.get("alert_role_id")
    role = guild.get_role(int(rid)) if rid else None
    if role is None or role.is_default():
        return None
    return role


def alert_content(role: discord.Role | None, ok: bool) -> str | None:
    if role is None:
        return None
    return (f"{role.mention} 🍯 a honeypot was triggered." if ok
            else f"{role.mention} ⚠️ a honeypot was triggered and I could NOT act — needs a human.")


async def _apply_action(guild: discord.Guild, member: discord.Member, cfg: dict) -> tuple:
    """Returns (ok, detail). Never raises."""
    action = cfg.get("action") or "ban"
    delete_seconds = int(cfg.get("delete_seconds") or 0)
    reason = "[honeypot] posted in the trap channel"
    perm = {"ban": "ban_members", "kick": "kick_members", "timeout": "moderate_members"}[action]
    if action == "kick" and delete_seconds > 0:
        perm = "ban_members"  # soft-ban needs ban rights to clear history
    problem = perm_check.member_problem(guild, member, perm, {"ban": "ban", "kick": "kick", "timeout": "time out"}[action])
    if problem:
        return False, problem
    try:
        if action == "ban":
            await guild.ban(member, reason=reason, delete_message_seconds=min(delete_seconds, 604800))
            return True, "Banned"
        if action == "kick":
            if delete_seconds > 0:
                await guild.ban(member, reason=reason, delete_message_seconds=min(delete_seconds, 604800))
                await guild.unban(member, reason="[honeypot] soft-ban — history cleared")
                return True, "Kicked (history cleared)"
            await guild.kick(member, reason=reason)
            return True, "Kicked"
        await member.timeout(timedelta(days=TIMEOUT_DAYS), reason=reason)
        return True, f"Timed out for {TIMEOUT_DAYS} days"
    except discord.Forbidden:
        return False, perm_check.forbidden_hint(guild, perm)
    except discord.HTTPException as e:
        return False, f"Discord error: {e.text or e.status}"


async def trip(bot, message: discord.Message, cfg: dict) -> None:
    guild, member = message.guild, message.author
    clone_id = _clone_of(bot)
    key = (guild.id, member.id)

    try:
        await message.delete()
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        pass
    if key in _tripping:
        return  # rapid-fire spam: message removed above, action already in flight
    _tripping.add(key)
    try:
        action = cfg.get("action") or "ban"
        snippet = discord.utils.escape_markdown(discord.utils.escape_mentions(message.content or ""))[:300]
        extras = f"\n📎 {len(message.attachments)} attachment(s)" if message.attachments else ""

        # Tell the person why BEFORE the action (can't DM someone after a ban).
        # Hacked accounts are the usual case, so give them a way back.
        try:
            await member.send(
                f"You were **{ACTIONS[action][2]}** in **{guild.name}** for posting in its honeypot "
                "channel — a trap that only spam bots and hacked accounts ever post in.\n"
                "If that was your real account, it's probably compromised: change your password, "
                "turn on 2FA, remove unknown authorised apps, then contact the server staff to appeal."
            )
        except (discord.Forbidden, discord.HTTPException):
            pass

        ok, detail = await _apply_action(guild, member, cfg)
        try:
            await db.record_honeypot_catch(guild.id, clone_id, member.id, action, ok)
        except Exception:
            logger.exception("honeypot catch log failed for guild %s", guild.id)
        if ok:
            try:
                await db.bump_honeypot_triggers(guild.id, clone_id=clone_id)
            except Exception:
                logger.exception("honeypot counter bump failed for guild %s", guild.id)
            perm_check.clear(guild.id, clone_id, "honeypot")
        else:
            perm_check.flag(guild.id, clone_id, "honeypot", f"Honeypot couldn't act on {member}: {detail}")

        created = int(member.created_at.timestamp())
        joined = int(member.joined_at.timestamp()) if member.joined_at else None
        embed = discord.Embed(
            title="🍯 Honeypot triggered" if ok else "⚠️ Honeypot triggered — action FAILED",
            description=(f"{member.mention} (`{member.id}`) posted in <#{cfg['channel_id']}>."
                         + (f"\n>>> {snippet}" if snippet else "") + extras),
            color=discord.Color.red() if ok else discord.Color.orange(),
        )
        embed.add_field(name="Result", value=detail, inline=False)
        embed.add_field(name="Account created", value=f"<t:{created}:R>", inline=True)
        if joined:
            embed.add_field(name="Joined server", value=f"<t:{joined}:R>", inline=True)
        embed.set_thumbnail(url=member.display_avatar.url)
        role = alert_role_of(guild, cfg)
        await _send_log(guild, clone_id, cfg, embed, content=alert_content(role, ok), ping_role=role)
    finally:
        # keep the key briefly so a burst of queued messages is still swallowed
        async def _release():
            await asyncio.sleep(15)
            _tripping.discard(key)
        asyncio.create_task(_release())


async def warn_staff(bot, message: discord.Message, cfg: dict) -> None:
    """A staff member posted in the trap. They are NOT exempt, but they are not
    actioned either: the message is removed, they get a short notice, and it is
    logged. Never raises."""
    guild, member = message.guild, message.author
    clone_id = _clone_of(bot)
    try:
        await message.delete()
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        pass
    key = (guild.id, member.id)
    now = time.monotonic()
    last = _warned.get(key)
    if last is not None and now - last < _STAFF_WARN_COOLDOWN:
        return   # burst of messages: removed above, one warning is enough
    _warned[key] = now
    if len(_warned) > 500:   # keep the map small
        for k in [k for k, t in _warned.items() if now - t > _STAFF_WARN_COOLDOWN]:
            _warned.pop(k, None)
    channel = message.channel
    try:
        await channel.send(
            f"⚠️ {member.mention} this is the **honeypot** — anyone who posts here is treated as a hacked "
            "account. You're staff, so nothing happened to you this time. "
            "Please don't post here.",
            delete_after=STAFF_WARNING_SECONDS,
            allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=[member]),
        )
    except (discord.Forbidden, discord.HTTPException):
        pass
    snippet = discord.utils.escape_markdown(discord.utils.escape_mentions(message.content or ""))[:300]
    embed = discord.Embed(
        title="🍯 Staff member posted in the honeypot",
        description=(f"{member.mention} (`{member.id}`) posted in <#{cfg['channel_id']}>."
                     + (f"\n>>> {snippet}" if snippet else "")),
        color=discord.Color.gold(),
    )
    embed.add_field(name="Result", value="Warned — no action taken (staff aren't banned)", inline=False)
    await _send_log(guild, clone_id, cfg, embed)


# ── test alert + stats ────────────────────────────────────────────────────

_TEST_COOLDOWN = 20.0
_last_test: dict = {}   # (guild_id, clone_id) -> monotonic time


async def send_test_alert(bot, guild: discord.Guild, tester: discord.abc.User) -> tuple:
    """Posts a SAMPLE catch (with the real alert-role ping) to wherever real catches
    go. Nobody is actioned and the counters are untouched. Returns (ok, message)."""
    clone_id = _clone_of(bot)
    cfg = await db.get_honeypot_config(guild.id, clone_id=clone_id)
    if not cfg.get("channel_id"):
        return False, "Set up the honeypot first — there's no trap channel yet."
    now = time.monotonic()
    last = _last_test.get((guild.id, clone_id))
    if last is not None and now - last < _TEST_COOLDOWN:
        return False, f"Easy — try again in {int(_TEST_COOLDOWN - (now - last)) + 1}s."
    premium = await is_premium(guild.id, clone_id)
    eff = effective_config(cfg, premium)
    role = alert_role_of(guild, eff)
    embed = discord.Embed(
        title="🧪 Honeypot TEST — nobody was actioned",
        description=(f"This is what a real catch looks like. Triggered by {tester.mention} pressing "
                     f"**Send test alert**; nothing was banned and the stats were not changed."),
        color=discord.Color.blurple(),
    )
    embed.add_field(name="Would have done",
                    value=f"{ACTIONS[eff.get('action') or 'ban'][1]} {ACTIONS[eff.get('action') or 'ban'][0]}",
                    inline=False)
    embed.set_thumbnail(url=tester.display_avatar.url)
    _last_test[(guild.id, clone_id)] = now
    sent = await _send_log(guild, clone_id, eff, embed,
                           content=(f"{role.mention} 🧪 test alert — ignore." if role else None), ping_role=role)
    if not sent:
        return False, ("I couldn't post it. Set a log channel (or a mod-log channel) and make sure I can "
                       "view, send and embed links there.")
    where = f"<#{eff['log_channel_id']}>" if eff.get("log_channel_id") else "your mod-log channel"
    return True, f"Sent a test alert to {where}" + (f", pinging {role.mention}." if role else ". No alert role is set, so nobody was pinged.")


def stats_lines(stats: dict) -> list:
    last = stats.get("last_at")
    out = [f"**Caught:** {stats.get('day', 0)} today · {stats.get('week', 0)} this week · "
           f"{stats.get('month', 0)} this month · {stats.get('total', 0)} all time"]
    if stats.get("failed_month"):
        out.append(f"⚠️ **{stats['failed_month']}** catch(es) this month where I couldn't act — check my permissions.")
    if last:
        out.append(f"**Last catch:** <t:{int(last.timestamp())}:R>")
    return out


async def _safe_stats(guild_id: int, clone_id) -> dict:
    try:
        return await db.get_honeypot_stats(guild_id, clone_id=clone_id)
    except Exception:
        logger.exception("honeypot stats failed for guild %s", guild_id)
        return {}


# ── settings panel ────────────────────────────────────────────────────────

def _status_lines(guild: discord.Guild, cfg: dict, premium: bool, stats: dict | None = None) -> list:
    ch = cfg.get("channel_id")
    channel = f"<#{ch}>" if ch and guild.get_channel(ch) else "*missing — tap **Repost / recreate***"
    saved = cfg
    cfg = effective_config(cfg, premium)
    action = cfg.get("action") or "ban"
    state = "🟢 **Active**" if cfg.get("enabled") else "⏸️ **Paused**"
    log_id = cfg.get("log_channel_id")
    lines = [
        f"**Status:** {state}",
        f"**Trap channel:** {channel}",
        f"**Action:** {ACTIONS[action][1]} {ACTIONS[action][0]}",
    ]
    if action != "timeout":
        lines.append(f"**History:** {DELETE_OPTIONS.get(int(cfg.get('delete_seconds') or 0), 'custom')}")
    lines.append(f"**Log channel:** {f'<#{log_id}>' if log_id else 'server mod-log (if set)'}")
    arole = alert_role_of(guild, cfg)
    lines.append(f"**Staff alert:** {arole.mention if arole else 'no role set — pick one below'}")
    if stats:
        lines.extend(stats_lines(stats))
    else:
        n = int(cfg.get("triggered_count") or 0)
        last = cfg.get("last_triggered_at")
        caught = f"**Caught so far:** {n}"
        if last:
            caught += f" · last <t:{int(last.timestamp())}:R>"
        lines.append(caught)
    if not premium:
        lines.append(
            "\n💎 **Premium extras** (the trap itself stays free):\n"
            "• Choose what happens: ban, kick or a 28-day timeout\n"
            "• Choose how much of their message history gets wiped\n"
            "• Send catches to a dedicated log channel\n"
            "Free plan: ban + wipe the last 24h, logged to your mod-log. Tap **Unlock premium extras** below."
            + ("\n-# Your saved premium settings are paused, not lost." if _extras_paused(saved) else ""))
    miss = missing_permissions(guild, action)
    if miss:
        lines.append(f"⚠️ **I'm missing:** {', '.join(miss)}")
    return lines


def build_panel(guild: discord.Guild, clone_id, cfg: dict, premium: bool, note: str = "",
                stats: dict | None = None) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=None)
    if cfg.get("enabled"):
        color = discord.Color.green()
    else:
        color = discord.Color.orange()
    container = discord.ui.Container(accent_colour=color)
    if note:   # a Components v2 message can't carry plain `content`, so the note lives inside the panel
        container.add_item(discord.ui.TextDisplay(note))
    container.add_item(discord.ui.TextDisplay(
        "### 🍯 Honeypot\n"
        + "\n".join(perm_check.lines(guild.id, clone_id) + _status_lines(guild, cfg, premium, stats))
        + "\n\n-# Anyone who posts in the trap channel gets the action below. Staff (mod/admin perms) get a warning instead."
    ))
    container.add_item(discord.ui.Separator())

    if premium:   # the extras are premium-only; free servers see an upgrade button instead
        action_row = discord.ui.ActionRow()
        action_row.add_item(HoneypotActionSelect(guild.id, clone_id, cfg.get("action") or "ban"))
        container.add_item(action_row)

        if (cfg.get("action") or "ban") != "timeout":
            hist_row = discord.ui.ActionRow()
            hist_row.add_item(HoneypotHistorySelect(guild.id, clone_id, int(cfg.get("delete_seconds") or 0)))
            container.add_item(hist_row)

        log_row = discord.ui.ActionRow()
        log_row.add_item(HoneypotLogSelect(guild.id, clone_id))
        container.add_item(log_row)

    alert_row = discord.ui.ActionRow()   # free for every server: it's the core safety feature
    alert_row.add_item(HoneypotAlertRoleSelect(guild.id, clone_id))
    container.add_item(alert_row)

    nav_row = discord.ui.ActionRow()   # own row so the existing buttons below never move
    if cfg.get("alert_role_id"):
        nav_row.add_item(HoneypotButton("clearalert", guild.id, clone_id))
    nav_row.add_item(HoneypotButton("panel", guild.id, clone_id))
    container.add_item(nav_row)

    btn_row = discord.ui.ActionRow()
    paused = not cfg.get("enabled")
    btn_row.add_item(HoneypotButton("pause", guild.id, clone_id, paused=paused))
    btn_row.add_item(HoneypotButton("repost", guild.id, clone_id))
    btn_row.add_item(HoneypotButton("remove", guild.id, clone_id))
    btn_row.add_item(HoneypotButton("test", guild.id, clone_id))
    if not premium:
        btn_row.add_item(HoneypotButton("upgrade", guild.id, clone_id))
    container.add_item(btn_row)

    view.add_item(container)
    return view


def _build_confirm_remove(guild_id: int, clone_id, cfg: dict) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=None)
    container = discord.ui.Container(accent_colour=discord.Color.red())
    extra = ""
    if cfg.get("channel_auto_created") and cfg.get("channel_id"):
        extra = f" The <#{cfg['channel_id']}> channel I created will be **deleted** too."
    container.add_item(discord.ui.TextDisplay(
        f"### Remove the honeypot?\nThe trap stops working immediately.{extra}"
    ))
    row = discord.ui.ActionRow()
    row.add_item(HoneypotButton("removeyes", guild_id, clone_id))
    row.add_item(HoneypotButton("removeno", guild_id, clone_id))
    container.add_item(row)
    view.add_item(container)
    return view


async def _reply(interaction: discord.Interaction, text: str) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(text, ephemeral=True)
    else:
        await interaction.response.send_message(text, ephemeral=True)


async def _authorize(interaction: discord.Interaction, guild_id: int, need_premium: bool = False):
    """Works from a DM (join-DM button) or a guild. Resolves the guild from the
    custom_id, checks Manage Server, and (optionally) premium. The honeypot itself is
    FREE, so only the premium extras (action / history / log channel selects and
    the upgrade button) pass need_premium=True. Returns the
    guild, or None after telling the user why."""
    guild = interaction.client.get_guild(guild_id)
    if guild is None:
        await _reply(interaction, "I'm not in that server anymore.")
        return None
    member = guild.get_member(interaction.user.id)
    if member is None or not (member.guild_permissions.manage_guild or member == guild.owner):
        await _reply(interaction, "You need the **Manage Server** permission in that server to use the honeypot.")
        return None
    if need_premium and not await is_premium(guild_id, _clone_of(interaction.client)):
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        from discord_bot.cogs._views_premium import send_premium_pitch
        await interaction.followup.send(
            "🍯 Choosing the action, history window and log channel are **premium** extras — the honeypot itself stays free. Here's how to unlock them 👇", ephemeral=True,
        )
        await send_premium_pitch(interaction, guild_id, _clone_of(interaction.client))
        return None
    return guild


async def _rerender(interaction: discord.Interaction, guild: discord.Guild, clone_id) -> None:
    if not interaction.response.is_done():
        await interaction.response.defer()
    cfg = await db.get_honeypot_config(guild.id, clone_id=clone_id)
    premium = await is_premium(guild.id, clone_id)
    stats = await _safe_stats(guild.id, clone_id)
    await interaction.edit_original_response(view=build_panel(guild, clone_id, cfg, premium, stats=stats))


async def open_honeypot(interaction: discord.Interaction, guild: discord.Guild, clone_id) -> None:
    """Shared by the join-DM button and /honeypot. Call AFTER
    interaction.response.defer(ephemeral=True) and after _authorize()."""
    cfg, channel, created, error = await ensure_honeypot(guild, clone_id, interaction.user)
    if error:
        await interaction.followup.send(error, ephemeral=True)
        return
    premium = await is_premium(guild.id, clone_id)
    note = (f"🍯 Created {channel.mention} and posted the warning notice in it."
            if created else f"🍯 Honeypot is live in {channel.mention}.")
    view = build_panel(guild, clone_id, cfg, premium, note=note, stats=await _safe_stats(guild.id, clone_id))
    await interaction.followup.send(view=view, ephemeral=True)   # no `content=`: v2 views reject it


# ── dynamic items ─────────────────────────────────────────────────────────

class HoneypotActionSelect(discord.ui.DynamicItem[discord.ui.Select], template=_pat("action")):
    def __init__(self, guild_id: int, clone_id, current: str = "ban"):
        self.guild_id, self.clone_id = guild_id, clone_id
        options = [
            discord.SelectOption(label=label, value=key, emoji=emoji, default=(key == current),
                                 description={"ban": "Ban + wipe their recent messages",
                                              "kick": "Kick (soft-ban if history deletion is on)",
                                              "timeout": "Mute them for 28 days, keep their account here"}[key])
            for key, (label, emoji, _) in ACTIONS.items()
        ]
        super().__init__(discord.ui.Select(
            placeholder="What happens to anyone who posts in the trap?",
            options=options, min_values=1, max_values=1, custom_id=_cid("action", guild_id, clone_id),
        ))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(*_ids(match))

    async def callback(self, interaction: discord.Interaction):
        guild = await _authorize(interaction, self.guild_id, need_premium=True)
        if guild is None:
            return
        await interaction.response.defer()
        cfg = await db.set_honeypot_config(guild.id, clone_id=self.clone_id, action=self.item.values[0])
        _invalidate(guild.id, self.clone_id)
        await _refresh_warning(guild, cfg)  # keep the public notice truthful
        await _rerender(interaction, guild, self.clone_id)
        miss = missing_permissions(guild, self.item.values[0])
        if miss:
            await interaction.followup.send(
                f"Heads up — I'm missing **{', '.join(miss)}**, so I can't do that yet. Enable it on my role.",
                ephemeral=True)


class HoneypotHistorySelect(discord.ui.DynamicItem[discord.ui.Select], template=_pat("hist")):
    def __init__(self, guild_id: int, clone_id, current: int = 86400):
        self.guild_id, self.clone_id = guild_id, clone_id
        options = [discord.SelectOption(label=label, value=str(sec), default=(sec == current))
                   for sec, label in DELETE_OPTIONS.items()]
        super().__init__(discord.ui.Select(
            placeholder="Clean up the intruder's other messages?",
            options=options, min_values=1, max_values=1, custom_id=_cid("hist", guild_id, clone_id),
        ))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(*_ids(match))

    async def callback(self, interaction: discord.Interaction):
        guild = await _authorize(interaction, self.guild_id, need_premium=True)
        if guild is None:
            return
        await interaction.response.defer()
        await db.set_honeypot_config(guild.id, clone_id=self.clone_id, delete_seconds=int(self.item.values[0]))
        _invalidate(guild.id, self.clone_id)
        await _rerender(interaction, guild, self.clone_id)


class HoneypotLogSelect(discord.ui.DynamicItem[discord.ui.ChannelSelect], template=_pat("log")):
    def __init__(self, guild_id: int, clone_id):
        self.guild_id, self.clone_id = guild_id, clone_id
        super().__init__(discord.ui.ChannelSelect(
            placeholder="Where should I log catches? (optional)",
            channel_types=[discord.ChannelType.text], min_values=1, max_values=1,
            custom_id=_cid("log", guild_id, clone_id),
        ))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(*_ids(match))

    async def callback(self, interaction: discord.Interaction):
        guild = await _authorize(interaction, self.guild_id, need_premium=True)
        if guild is None:
            return
        await interaction.response.defer()
        picked = guild.get_channel(self.item.values[0].id)
        problem = perm_check.channel_problem(picked, guild.me, ("view_channel", "send_messages", "embed_links")) if picked else None
        if problem:
            await interaction.followup.send(problem, ephemeral=True)
            return
        await db.set_honeypot_config(guild.id, clone_id=self.clone_id, log_channel_id=picked.id)
        _invalidate(guild.id, self.clone_id)
        await _rerender(interaction, guild, self.clone_id)


class HoneypotAlertRoleSelect(discord.ui.DynamicItem[discord.ui.RoleSelect], template=_pat("alertrole")):
    def __init__(self, guild_id: int, clone_id):
        self.guild_id, self.clone_id = guild_id, clone_id
        super().__init__(discord.ui.RoleSelect(
            placeholder="Staff role to ping on every catch (optional)",
            min_values=1, max_values=1, custom_id=_cid("alertrole", guild_id, clone_id),
        ))

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
        await db.set_honeypot_config(guild.id, clone_id=self.clone_id, alert_role_id=role.id)
        _invalidate(guild.id, self.clone_id)
        await _rerender(interaction, guild, self.clone_id)
        if not role.mentionable and not guild.me.guild_permissions.mention_everyone:
            await interaction.followup.send(
                f"Heads up — {role.mention} isn't mentionable and I don't have **Mention Everyone**, "
                "so Discord won't ping it. Make the role mentionable or give me that permission.",
                ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


_BUTTONS = {
    "panel":     ("Open server panel", discord.ButtonStyle.primary, "🛠️"),
    "test":      ("Send test alert", discord.ButtonStyle.success, "🧪"),
    "clearalert": ("Clear alert role", discord.ButtonStyle.secondary, "🔕"),
    "pause":     ("Pause", discord.ButtonStyle.secondary, "⏸️"),
    "repost":    ("Repost / recreate", discord.ButtonStyle.primary, "🔁"),
    "remove":    ("Remove honeypot", discord.ButtonStyle.danger, "🗑️"),
    "removeyes": ("Yes, remove it", discord.ButtonStyle.danger, "🗑️"),
    "removeno":  ("Keep it", discord.ButtonStyle.secondary, "↩️"),
    "upgrade":   ("Unlock premium extras", discord.ButtonStyle.success, "💎"),
}


class HoneypotButton(discord.ui.DynamicItem[discord.ui.Button], template=_pat("pause|repost|remove|removeyes|removeno|upgrade|test|clearalert|panel")):
    def __init__(self, kind: str, guild_id: int, clone_id, paused: bool = False):
        self.kind, self.guild_id, self.clone_id = kind, guild_id, clone_id
        label, style, emoji = _BUTTONS[kind]
        if kind == "pause" and paused:
            label, style, emoji = "Resume", discord.ButtonStyle.success, "▶️"
        super().__init__(discord.ui.Button(label=label, style=style, emoji=emoji,
                                           custom_id=_cid(kind, guild_id, clone_id)))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        guild_id, clone_id = _ids(match)
        return cls(match.group("kind"), guild_id, clone_id)

    async def callback(self, interaction: discord.Interaction):
        # The honeypot is free: nothing here needs premium except the upgrade
        # button, whose whole job is to show the premium pitch.
        guild = await _authorize(interaction, self.guild_id, need_premium=(self.kind == "upgrade"))
        if guild is None:
            return
        clone_id = self.clone_id

        if self.kind == "test":
            await interaction.response.defer(ephemeral=True)
            ok, msg = await send_test_alert(interaction.client, guild, interaction.user)
            await interaction.followup.send(("✅ " if ok else "⚠️ ") + msg, ephemeral=True,
                                            allowed_mentions=discord.AllowedMentions.none())

        elif self.kind == "panel":
            # Same screen as /serversetup. A DM (join-DM honeypot button) has no server to open it in.
            if interaction.guild is None or interaction.guild.id != guild.id:
                await _reply(interaction, "Open your server and run `/serversetup` there to open the panel.")
                return
            from discord_bot.cogs._views_server_panel import open_home
            await open_home(interaction)

        elif self.kind == "clearalert":
            await interaction.response.defer()
            await db.set_honeypot_config(guild.id, clone_id=clone_id, alert_role_id=None)
            _invalidate(guild.id, clone_id)
            await _rerender(interaction, guild, clone_id)

        elif self.kind == "upgrade":
            # Reached only when the server is premium now (the pitch is shown otherwise): just refresh.
            await _rerender(interaction, guild, clone_id)

        elif self.kind == "pause":
            await interaction.response.defer()
            cfg = await db.get_honeypot_config(guild.id, clone_id=clone_id)
            await db.set_honeypot_config(guild.id, clone_id=clone_id, enabled=not cfg.get("enabled"))
            _invalidate(guild.id, clone_id)
            await _rerender(interaction, guild, clone_id)

        elif self.kind == "repost":
            await interaction.response.defer()
            cfg, channel, created, error = await ensure_honeypot(guild, clone_id, interaction.user)
            if error:
                await interaction.followup.send(error, ephemeral=True)
                return
            await _rerender(interaction, guild, clone_id)
            await interaction.followup.send(
                f"🍯 Recreated {channel.mention} and reposted the notice." if created
                else f"🍯 Notice refreshed in {channel.mention}.", ephemeral=True)

        elif self.kind == "remove":
            await interaction.response.defer()
            cfg = await db.get_honeypot_config(guild.id, clone_id=clone_id)
            await interaction.edit_original_response(view=_build_confirm_remove(guild.id, clone_id, cfg))

        elif self.kind == "removeno":
            await _rerender(interaction, guild, clone_id)

        elif self.kind == "removeyes":
            await interaction.response.defer()
            cfg = await db.get_honeypot_config(guild.id, clone_id=clone_id)
            channel = guild.get_channel(cfg.get("channel_id") or 0)
            deleted = False
            if channel is not None and cfg.get("channel_auto_created"):
                try:
                    await channel.delete(reason=f"Honeypot removed by {interaction.user}")
                    deleted = True
                except (discord.Forbidden, discord.HTTPException):
                    pass
            elif channel is not None and cfg.get("warning_message_id"):
                try:
                    await (await channel.fetch_message(int(cfg["warning_message_id"]))).delete()
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass
            await db.delete_honeypot_config(guild.id, clone_id=clone_id)
            _invalidate(guild.id, clone_id)
            done = discord.ui.LayoutView(timeout=None)
            box = discord.ui.Container(accent_colour=discord.Color.greyple())
            box.add_item(discord.ui.TextDisplay(
                "### 🍯 Honeypot removed\n"
                + ("The trap channel was deleted too. " if deleted else "")
                + "Tap **Honeypot** in the setup DM (or run `/honeypot`) to set it up again anytime."
            ))
            done.add_item(box)
            await interaction.edit_original_response(view=done)


DYNAMIC_ITEMS = (HoneypotActionSelect, HoneypotHistorySelect, HoneypotLogSelect, HoneypotAlertRoleSelect, HoneypotButton)


# ── cog ───────────────────────────────────────────────────────────────────

class HoneypotCog(GuildOnlyCog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def _clone_id(self):
        return getattr(self.bot, "clone_id", None)

    @app_commands.command(name="honeypot", description="Set up a free trap channel that auto-actions spam bots & hacked accounts")
    @app_commands.guild_only()
    async def honeypot_cmd(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        guild = await _authorize(interaction, interaction.guild_id)   # free: Manage Server only
        if guild is None:
            return
        await open_honeypot(interaction, guild, self._clone_id())

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.guild is None or message.author.bot or message.webhook_id is not None:
            return
        cfg = await _cached_config(message.guild.id, self._clone_id())
        if not cfg or not cfg.get("enabled") or message.channel.id != cfg.get("channel_id"):
            return
        if not isinstance(message.author, discord.Member):
            return
        if _is_staff(message.author):          # not exempt: warned, never actioned
            await warn_staff(self.bot, message, effective_config(cfg, await is_premium(message.guild.id, self._clone_id())))
            return
        premium = await is_premium(message.guild.id, self._clone_id())
        await trip(self.bot, message, effective_config(cfg, premium))

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel):
        cfg = await _cached_config(channel.guild.id, self._clone_id())
        if cfg and cfg.get("channel_id") == channel.id:
            perm_check.flag(channel.guild.id, self._clone_id(), "honeypot_channel",
                            "The honeypot channel was deleted — open `/honeypot` and tap **Repost / recreate**.")


async def setup(bot: commands.Bot):
    await bot.add_cog(HoneypotCog(bot))
