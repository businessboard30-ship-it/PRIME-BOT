# path: discord_bot/cogs/honeypot.py

"""
Honeypot — a trap channel for spam/scam bots and hacked accounts (PREMIUM).

Spam bots and compromised accounts blast the same "free nitro" link into every
channel they can see. A real member reads the warning and never posts in the
trap channel, so ANYONE who does (except staff) is treated as compromised and
gets the configured action applied automatically:

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

Premium: setting up / changing the honeypot needs Go Premium (per server).
Enforcement also only runs while the server is premium. Pausing and removing
are always allowed so a lapsed server is never stuck with a trap it can't turn
off.
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

_CACHE_TTL = 60.0
_cache: dict = {}        # (guild_id, clone_id) -> (expires_at, config | None)
_tripping: set = set()   # (guild_id, user_id) currently being actioned


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


def _is_staff(member: discord.Member) -> bool:
    """Staff are exempt so an admin poking the channel never bans themselves."""
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

async def _send_log(guild: discord.Guild, clone_id, cfg: dict, embed: discord.Embed) -> None:
    channel_id = cfg.get("log_channel_id")
    if not channel_id:
        try:
            automod = await db.get_automod_config(guild.id, clone_id=clone_id)
            channel_id = automod.get("log_channel_id")
        except Exception:
            channel_id = None
    channel = guild.get_channel(channel_id) if channel_id else None
    if channel is None:
        return
    try:
        await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
    except (discord.Forbidden, discord.HTTPException):
        pass


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
        await _send_log(guild, clone_id, cfg, embed)
    finally:
        # keep the key briefly so a burst of queued messages is still swallowed
        async def _release():
            await asyncio.sleep(15)
            _tripping.discard(key)
        asyncio.create_task(_release())


# ── settings panel ────────────────────────────────────────────────────────

def _status_lines(guild: discord.Guild, cfg: dict, premium: bool) -> list:
    ch = cfg.get("channel_id")
    channel = f"<#{ch}>" if ch and guild.get_channel(ch) else "*missing — tap **Repost / recreate***"
    action = cfg.get("action") or "ban"
    if not premium:
        state = "🔒 **Locked** — premium expired (trap is not enforcing)"
    elif cfg.get("enabled"):
        state = "🟢 **Active**"
    else:
        state = "⏸️ **Paused**"
    log_id = cfg.get("log_channel_id")
    lines = [
        f"**Status:** {state}",
        f"**Trap channel:** {channel}",
        f"**Action:** {ACTIONS[action][1]} {ACTIONS[action][0]}",
    ]
    if action != "timeout":
        lines.append(f"**History:** {DELETE_OPTIONS.get(int(cfg.get('delete_seconds') or 0), 'custom')}")
    lines.append(f"**Log channel:** {f'<#{log_id}>' if log_id else 'server mod-log (if set)'}")
    n = int(cfg.get("triggered_count") or 0)
    last = cfg.get("last_triggered_at")
    caught = f"**Caught so far:** {n}"
    if last:
        caught += f" · last <t:{int(last.timestamp())}:R>"
    lines.append(caught)
    miss = missing_permissions(guild, action)
    if miss:
        lines.append(f"⚠️ **I'm missing:** {', '.join(miss)}")
    return lines


def build_panel(guild: discord.Guild, clone_id, cfg: dict, premium: bool) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=None)
    if not premium:
        color = discord.Color.dark_grey()
    elif cfg.get("enabled"):
        color = discord.Color.green()
    else:
        color = discord.Color.orange()
    container = discord.ui.Container(accent_colour=color)
    container.add_item(discord.ui.TextDisplay(
        "### 🍯 Honeypot\n"
        + "\n".join(perm_check.lines(guild.id, clone_id) + _status_lines(guild, cfg, premium))
        + "\n\n-# Anyone who posts in the trap channel gets the action below. Staff (mod/admin perms) are exempt."
    ))
    container.add_item(discord.ui.Separator())

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

    btn_row = discord.ui.ActionRow()
    paused = not cfg.get("enabled")
    btn_row.add_item(HoneypotButton("pause", guild.id, clone_id, paused=paused))
    btn_row.add_item(HoneypotButton("repost", guild.id, clone_id))
    btn_row.add_item(HoneypotButton("remove", guild.id, clone_id))
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


async def _authorize(interaction: discord.Interaction, guild_id: int, need_premium: bool = True):
    """Works from a DM (join-DM button) or a guild. Resolves the guild from the
    custom_id, checks Manage Server, and (optionally) premium. Returns the
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
            "🍯 The **Honeypot** is a premium feature. Here's how to unlock it for your server 👇", ephemeral=True,
        )
        await send_premium_pitch(interaction, guild_id, _clone_of(interaction.client))
        return None
    return guild


async def _rerender(interaction: discord.Interaction, guild: discord.Guild, clone_id) -> None:
    if not interaction.response.is_done():
        await interaction.response.defer()
    cfg = await db.get_honeypot_config(guild.id, clone_id=clone_id)
    premium = await is_premium(guild.id, clone_id)
    await interaction.edit_original_response(view=build_panel(guild, clone_id, cfg, premium))


async def open_honeypot(interaction: discord.Interaction, guild: discord.Guild, clone_id) -> None:
    """Shared by the join-DM button and /honeypot. Call AFTER
    interaction.response.defer(ephemeral=True) and after _authorize()."""
    cfg, channel, created, error = await ensure_honeypot(guild, clone_id, interaction.user)
    if error:
        await interaction.followup.send(error, ephemeral=True)
        return
    premium = True  # _authorize already gated on it
    view = build_panel(guild, clone_id, cfg, premium)
    note = (f"🍯 Created {channel.mention} and posted the warning notice in it."
            if created else f"🍯 Honeypot is live in {channel.mention}.")
    await interaction.followup.send(note, view=view, ephemeral=True)


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
        guild = await _authorize(interaction, self.guild_id)
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
        guild = await _authorize(interaction, self.guild_id)
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
        guild = await _authorize(interaction, self.guild_id)
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


_BUTTONS = {
    "pause":     ("Pause", discord.ButtonStyle.secondary, "⏸️"),
    "repost":    ("Repost / recreate", discord.ButtonStyle.primary, "🔁"),
    "remove":    ("Remove honeypot", discord.ButtonStyle.danger, "🗑️"),
    "removeyes": ("Yes, remove it", discord.ButtonStyle.danger, "🗑️"),
    "removeno":  ("Keep it", discord.ButtonStyle.secondary, "↩️"),
}


class HoneypotButton(discord.ui.DynamicItem[discord.ui.Button], template=_pat("pause|repost|remove|removeyes|removeno")):
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
        # Pausing/removing never needs premium — a lapsed server must always be
        # able to turn the trap off.
        needs_premium = self.kind == "repost"
        guild = await _authorize(interaction, self.guild_id, need_premium=needs_premium)
        if guild is None:
            return
        clone_id = self.clone_id

        if self.kind == "pause":
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


DYNAMIC_ITEMS = (HoneypotActionSelect, HoneypotHistorySelect, HoneypotLogSelect, HoneypotButton)


# ── cog ───────────────────────────────────────────────────────────────────

class HoneypotCog(GuildOnlyCog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def _clone_id(self):
        return getattr(self.bot, "clone_id", None)

    @app_commands.command(name="honeypot", description="Set up a trap channel that auto-actions spam bots & hacked accounts (premium)")
    @app_commands.guild_only()
    async def honeypot_cmd(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        guild = await _authorize(interaction, interaction.guild_id)
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
        if not isinstance(message.author, discord.Member) or _is_staff(message.author):
            return
        if not await is_premium(message.guild.id, self._clone_id()):
            return
        await trip(self.bot, message, cfg)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel):
        cfg = await _cached_config(channel.guild.id, self._clone_id())
        if cfg and cfg.get("channel_id") == channel.id:
            perm_check.flag(channel.guild.id, self._clone_id(), "honeypot_channel",
                            "The honeypot channel was deleted — open `/honeypot` and tap **Repost / recreate**.")


async def setup(bot: commands.Bot):
    await bot.add_cog(HoneypotCog(bot))
