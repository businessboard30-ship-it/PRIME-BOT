# path: discord_bot/cogs/ai_server_ops.py

"""
AI-ONLY server administration: everything the bot's AI could not do before — give/remove a role on a member,
nicknames, editing a role, channel permissions / lock / slowmode / topic / move, threads, voice moderation, invites,
single-message delete + pins, emojis, stickers, webhooks, scheduled events, server settings, the audit log and
integrations.

Zero new slash commands, zero Discord command slots
---------------------------------------------------
These are real discord.app_commands.Command objects (so the AI can build its tool schema and the guard can run the
usual checks from them) but this cog is NEVER added to the bot's command tree. `setup()` instead hands the commands
straight to the AI-only registry (modules.ai_command_guard.register_hidden_cog), so they can never appear in
Discord's command list, can't be typed by anyone, and cost nothing against the 100-command cap. They are allowlisted
(ai_only=True) in modules/ai_command_allowlist.py like the other AI-only commands. Do NOT add this cog to the tree.

Safety model (all of it applies on top of the guard's allowlist + daily cap)
-----------------------------------------------------------------------------
* The asker's real Discord permissions are checked (spec.needs_perms in the guard, then again in every planner),
  and so are the bot's.
* Every action that changes something shows a plan and needs an explicit Confirm click; the plan is built AGAIN at
  click time, so a stale confirmation can't act on a changed server.
* Role/member hierarchy: nothing at or above the asker's top role (or the bot's) is touched; managed roles are never
  handed out; the owner is never a target.
* Permission grants: never Administrator; dangerous permissions only by the owner/an Administrator; nobody grants a
  permission they lack (rules in modules/ai_server_ops.py).
* Reads that could leak into a public channel (audit log, webhooks, invites, pins, integrations) also need the
  Confirm click, because that is what moves the answer onto a real, EPHEMERAL interaction. Only emoji / sticker /
  event lists (public information) run without one.
* Images (emoji, server icon) are only fetched from Discord's own CDN (exact host allowlist, no redirects).
* Every Discord call carries an audit-log reason naming the asker, and every change is written to the mod log.
"""

import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Awaitable, Callable, NamedTuple, Optional

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from discord_bot.cogs import _ai_bulk_ops
from modules import ai_bulk as _ai_bulk
from modules import moderation_extra as modx
from modules import ai_server_ops as ops
from modules.ai_server_ops import (
    CHANNEL_PERMS, clip, grant_problem, is_privileged, member_problem, nice_perm, parse_perms, reason_for,
    role_problem, safe_text, snowflake,
)
from modules.text_styles import plain_name as _plain_name

logger = logging.getLogger(__name__)

NO_PINGS = discord.AllowedMentions.none()

# Reads of public information: run straight away, no Confirm click.
PUBLIC_READS = frozenset({"emojiops_list", "stickerops_list", "eventops_list"})
# Reads whose answer could leak if posted publicly: they need the Confirm click so they run on an ephemeral reply.
PRIVATE_READS = frozenset({"serverinvite_list", "messageops_pins", "webhookops_list", "serveredit_auditlog",
                           "serveredit_integrations"})
READS = PUBLIC_READS | PRIVATE_READS


class Plan(NamedTuple):
    error: Optional[str] = None
    text: str = ""
    run: Optional[Callable[[], Awaitable[str]]] = None
    target_user_id: Optional[int] = None


def _fail(msg: str) -> Plan:
    return Plan(error=msg)


async def _say(i, text: str, **kw):
    """Reply to whichever kind of interaction this is (fresh slash call, deferred /aichat message, confirm click)."""
    kw.setdefault("allowed_mentions", NO_PINGS)
    try:
        if i.response.is_done():
            return await i.followup.send(text[:1990], ephemeral=True, **kw)
        return await i.response.send_message(text[:1990], ephemeral=True, **kw)
    except TypeError:                      # reply/mention-chat proxy accepts fewer keywords
        kw.pop("allowed_mentions", None)
        return await i.followup.send(text[:1990], ephemeral=True, **kw)


# ── permission / lookup helpers ──────────────────────────────────────────

def _actor(i):
    return i.user


def _missing(perms: discord.Permissions, names) -> list:
    if getattr(perms, "administrator", False):
        return []
    return [n for n in names if not getattr(perms, n, False)]


def _need(i, *names) -> Optional[str]:
    """Server-wide permission check for the asker AND the bot."""
    g = i.guild
    if g is None:
        return "This only works inside a server."
    miss = _missing(_actor(i).guild_permissions, names)
    if miss:
        return "You need **" + ", ".join(nice_perm(n) for n in miss) + "** to do that."
    miss = _missing(g.me.guild_permissions, names)
    if miss:
        return "I don't have **" + ", ".join(nice_perm(n) for n in miss) + "** in this server."
    return None


def _chan_need(i, channel, *names) -> Optional[str]:
    """Same, but for a specific channel (so channel overrides count)."""
    g = i.guild
    if g is None:
        return "This only works inside a server."
    miss = _missing(channel.permissions_for(_actor(i)), names)
    if miss:
        return f"You need **{', '.join(nice_perm(n) for n in miss)}** in **{safe_text(channel.name, 40)}**."
    miss = _missing(channel.permissions_for(g.me), names)
    if miss:
        return f"I don't have **{', '.join(nice_perm(n) for n in miss)}** in **{safe_text(channel.name, 40)}**."
    return None


def _chan(i, channel):
    """The channel asked for, or the one the person is chatting in. (channel, error)."""
    ch = channel if channel is not None else getattr(i, "channel", None)
    if ch is None or getattr(ch, "guild", None) is None or ch.guild.id != i.guild.id:
        return None, "Tell me which channel."
    return ch, None


def _find_role(guild, raw):
    """(role, error) from a name, @mention or ID."""
    text = str(raw or "").strip()
    if not text:
        return None, "Tell me which role."
    if text.lower().lstrip("@") in ("everyone", "here"):
        return guild.default_role, None
    sf = snowflake(text)
    if sf and guild.get_role(sf):
        return guild.get_role(sf), None
    low = text.lstrip("@").lower()
    hits = [r for r in guild.roles if r.name.lower() == low]
    if not hits:
        want = _plain_name(text).lower().strip(" -")
        hits = [r for r in guild.roles if _plain_name(r.name).lower().strip(" -") == want]
    if len(hits) == 1:
        return hits[0], None
    if not hits:
        return None, f"I couldn't find a role called “{safe_text(text, 40)}”."
    return None, f"{len(hits)} roles share that name — use the @mention or the role ID."


def _find_by_name(items, raw, kind: str, id_of=lambda x: x.id, name_of=lambda x: x.name):
    """Generic (thing, error) lookup by ID/mention or case-insensitive unique name."""
    text = str(raw or "").strip()
    if not text:
        return None, f"Tell me which {kind}."
    sf = snowflake(text)
    if sf is None:
        m = re.search(r":(\d{15,25})>$", text)           # <:name:id> / <a:name:id>
        sf = int(m.group(1)) if m else None
    items = list(items)
    if sf:
        for it in items:
            if id_of(it) == sf:
                return it, None
    low = text.strip(":#<> ").lower()
    hits = [it for it in items if name_of(it).lower() == low]
    if len(hits) == 1:
        return hits[0], None
    if not hits:
        return None, f"I couldn't find a {kind} called “{safe_text(text, 40)}”."
    return None, f"{len(hits)} {kind}s share that name — use the ID."


async def _find_thread(guild, raw):
    text = str(raw or "").strip()
    if not text:
        return None, "Tell me which thread."
    sf = snowflake(text)
    if sf:
        t = guild.get_thread(sf)
        if t is not None:
            return t, None
        try:
            ch = await guild.fetch_channel(sf)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            ch = None
        if isinstance(ch, discord.Thread):
            return ch, None
    return _find_by_name(guild.threads, text, "thread")


def _find_target(guild, raw):
    """A role or a member for a channel override: (target, error)."""
    text = str(raw or "").strip()
    if not text:
        return None, "Tell me who the permission is for (a role name or a @mention)."
    if text.startswith("<@") and not text.startswith("<@&"):
        m = guild.get_member(snowflake(text) or 0)
        return (m, None) if m else (None, "I couldn't find that member.")
    role, err = _find_role(guild, text)
    if role is not None:
        return role, None
    sf = snowflake(text)
    m = guild.get_member(sf) if sf else None
    return (m, None) if m else (None, err)


async def _download_image(url: str, limit: int) -> bytes:
    """Fetch an image from Discord's CDN. The URL was already checked by cdn_image_url_ok (exact host allowlist);
    redirects are refused so it can't be bounced somewhere else."""
    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url, allow_redirects=False) as resp:
            if resp.status != 200:
                raise ValueError(f"the image link answered with status {resp.status}")
            if not (resp.content_type or "").startswith("image/"):
                raise ValueError("that link isn't an image")
            data = await resp.content.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f"that image is bigger than {limit // 1024} KB")
    return data


def _is_member(x) -> bool:
    """Members have a top_role, roles don't (duck-typed so the same code serves real objects and test fakes)."""
    return hasattr(x, "top_role")


def _ts(dt) -> str:
    return f"<t:{int(dt.timestamp())}:f>" if dt else "never"


def _lock_keys(channel) -> dict:
    """Which permissions 'locking' this kind of channel takes away."""
    text = {"send_messages": False, "send_messages_in_threads": False,
            "create_public_threads": False, "create_private_threads": False}
    if isinstance(channel, discord.CategoryChannel):
        return {**text, "connect": False}
    if isinstance(channel, discord.ForumChannel):
        return {"create_public_threads": False, "send_messages_in_threads": False}
    if isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
        return {"connect": False}
    return text


class AIServerOpsCog(commands.Cog):
    """See the module docstring: hidden from Discord, runnable only by the AI."""

    # ── confirm plumbing (hook names are derived from the key, wired at the bottom of this file) ──
    AI_CONFIRM_PROMPTS: dict = {}
    AI_CONFIRMED_HANDLERS: dict = {}

    def __init__(self, bot):
        self.bot = bot

    # ── shared engine ────────────────────────────────────────────────────

    async def _plan(self, key: str, i, **kw) -> Plan:
        if i.guild is None:
            return _fail("This only works inside a server.")
        try:
            return await getattr(self, "plan_" + key)(i, **kw)
        except Exception:
            logger.exception(f"[ai_server_ops] planning {key} failed")
            return _fail("Something went wrong working that out — nothing was changed.")

    async def _execute(self, i, key: str, plan: Plan) -> str:
        try:
            msg = await plan.run()
        except discord.Forbidden:
            return ("❌ Discord refused: I'm missing a permission here, or my role is too low for that. "
                    "Nothing was changed.")
        except discord.HTTPException as e:
            return f"❌ Discord said no: {safe_text(getattr(e, 'text', None) or e, 160)}"
        except ValueError as e:
            return f"❌ {safe_text(e, 200)}"
        except Exception:
            logger.exception(f"[ai_server_ops] {key} failed")
            return "❌ Something went wrong — it may not have completed."
        if key not in READS:
            await modx.log_action(i.guild.id, f"ai_{key}", i.user.id, target_user_id=plan.target_user_id,
                                  reason=clip(plan.text, 300))
        return msg

    async def _confirmed(self, key: str, i, **kw):
        """Runs after the Confirm click: re-plan against the server as it is NOW, then do it."""
        if not i.response.is_done():
            await i.response.defer(ephemeral=True)
        plan = await self._plan(key, i, **kw)
        text = f"❌ {plan.error}" if plan.error else await self._execute(i, key, plan)
        await i.followup.send(text[:1990], ephemeral=True, allowed_mentions=NO_PINGS)

    async def _prompt(self, key: str, i, **kw):
        plan = await self._plan(key, i, **kw)
        return (False, f"❌ {plan.error}") if plan.error else (True, plan.text + "\nConfirm?")

    async def _slash(self, i, key: str, **kw):
        """What a slash callback does if it is ever invoked directly (the AI normally goes through the Confirm
        handlers above): plan, then either run (public reads) or show a Confirm button."""
        plan = await self._plan(key, i, **kw)
        if plan.error:
            return await _say(i, f"❌ {plan.error}")
        if key in PUBLIC_READS:
            return await _say(i, await self._execute(i, key, plan))
        from discord_bot.cogs._views_moderation import ConfirmActionView

        async def go(ci):
            await self._confirmed(key, ci, **kw)
        await _say(i, plan.text, view=ConfirmActionView(i.user.id, go, confirm_label="Confirm", danger=True))

    # ═══════════════════════════════ MEMBERS ═════════════════════════════

    member_g = app_commands.guild_only()(app_commands.Group(name="member", description="Roles and nicknames on members"))

    @member_g.command(name="role-add", description="Give a role to a member")
    @app_commands.describe(member="The member (@mention)", role="Role name, @mention or ID")
    async def member_role_add(self, interaction: discord.Interaction, member: discord.Member, role: str):
        await self._slash(interaction, "member_role_add", member=member, role=role)

    async def plan_member_role_add(self, i, member=None, role=None, **_):
        if err := _need(i, "manage_roles"):
            return _fail(err)
        if member is None:
            return _fail("Tell me which member — @mention them.")
        r, err = _find_role(i.guild, role)
        if err:
            return _fail(err)
        if err := role_problem(i.guild, i.user, r, assigning=True) or member_problem(i.guild, i.user, member, allow_self=True):
            return _fail(err)
        if r in member.roles:
            return _fail(f"{safe_text(member.display_name, 32)} already has **{r.name}**.")

        async def run():
            await member.add_roles(r, reason=reason_for(i.user))
            return f"✅ Gave **{r.name}** to {member.mention}."
        return Plan(None, f"Give the **{safe_text(r.name, 60)}** role to {member.mention}?", run, member.id)

    @member_g.command(name="role-remove", description="Take a role away from a member")
    @app_commands.describe(member="The member (@mention)", role="Role name, @mention or ID")
    async def member_role_remove(self, interaction: discord.Interaction, member: discord.Member, role: str):
        await self._slash(interaction, "member_role_remove", member=member, role=role)

    async def plan_member_role_remove(self, i, member=None, role=None, **_):
        if err := _need(i, "manage_roles"):
            return _fail(err)
        if member is None:
            return _fail("Tell me which member — @mention them.")
        r, err = _find_role(i.guild, role)
        if err:
            return _fail(err)
        if err := role_problem(i.guild, i.user, r, assigning=True) or member_problem(i.guild, i.user, member, allow_self=True):
            return _fail(err)
        if r not in member.roles:
            return _fail(f"{safe_text(member.display_name, 32)} doesn't have **{r.name}**.")

        async def run():
            await member.remove_roles(r, reason=reason_for(i.user))
            return f"✅ Took **{r.name}** from {member.mention}."
        return Plan(None, f"Take the **{safe_text(r.name, 60)}** role from {member.mention}?", run, member.id)

    # ── bulk: one role to many members ──
    # Same rules as the single-member commands (role hierarchy, no managed roles), plus: no powerful permissions in bulk, a hard cap,
    # the exact head-count in the Confirm text, one job per server (modules/ai_bulk.JOBS), and a paced background run with a summary.

    _BULK_TASKS: set = set()

    async def _bulk_role_plan(self, i, role, who, has_role, adding: bool) -> Plan:
        if err := _need(i, "manage_roles"):
            return _fail(err)
        g = i.guild
        r, err = _find_role(g, role)
        if err:
            return _fail(err)
        if r.is_default():
            return _fail("Everyone already has @everyone.")
        if err := role_problem(g, i.user, r, assigning=True):
            return _fail(err)
        if adding:
            bad = [p.replace("_", " ") for p in _ai_bulk.RISKY_BULK_PERMS if getattr(r.permissions, p, False)]
            if bad:
                return _fail(f"**{safe_text(r.name, 60)}** carries powerful permissions ({', '.join(bad[:4])}), so I won't hand it to many "
                             "people at once. Give it to specific members one by one instead.")
        scope = str(who or "everyone").strip().lower()
        if scope not in ("everyone", "all", "humans", "people", "members", "bots"):
            return _fail("`who` must be everyone, humans or bots.")
        hr = None
        if str(has_role or "").strip():
            hr, err = _find_role(g, has_role)
            if err:
                return _fail(err)
        if not getattr(g, "chunked", True):
            try:
                await g.chunk()
            except Exception:
                logger.warning("[ai_server_ops] member chunk failed", exc_info=True)
        pool = list(g.members)
        if scope == "bots":
            pool = [m for m in pool if m.bot]
        elif scope != "everyone" and scope != "all":
            pool = [m for m in pool if not m.bot]
        if hr is not None:
            pool = [m for m in pool if hr in m.roles]
        targets = [m for m in pool if (r not in m.roles) == adding and m.id != g.me.id]
        if not targets:
            return _fail(f"Nobody in that group {'is missing' if adding else 'has'} **{safe_text(r.name, 60)}** — nothing to do.")
        if len(targets) > _ai_bulk.MAX_BULK_ASSIGN:
            return _fail(f"That is {len(targets)} members; I do at most {_ai_bulk.MAX_BULK_ASSIGN} per request. Narrow it with `who` or `has_role`.")
        group = {"everyone": "everyone", "all": "everyone", "bots": "bots only"}.get(scope, "humans only")
        if hr is not None:
            group += f", who already have **{safe_text(hr.name, 60)}**"
        mins = max(1, int(len(targets) * _ai_bulk.ASSIGN_PAUSE_SECONDS / 60 + 0.5))
        verb, prep = ("Give", "to") if adding else ("Take", "from")
        text = (f"{verb} the **{safe_text(r.name, 60)}** role {prep} **{len(targets)}** member{'s' if len(targets) != 1 else ''} ({group})? "
                f"It runs in the background (about {mins} min) and I post here when it is done.")
        ids = [m.id for m in targets]
        who_id, channel = i.user, i.channel

        async def job():
            done = failed = 0
            stopped = None
            try:
                for mid in ids:
                    m = g.get_member(mid)
                    if m is None or (r in m.roles) == adding:
                        continue
                    try:
                        if adding:
                            await m.add_roles(r, reason=reason_for(who_id), atomic=True)
                        else:
                            await m.remove_roles(r, reason=reason_for(who_id), atomic=True)
                        done += 1
                    except discord.Forbidden:
                        stopped = "Discord refused (my role is probably too low now)."
                        break
                    except discord.HTTPException:
                        failed += 1
                    await asyncio.sleep(_ai_bulk.ASSIGN_PAUSE_SECONDS)
            finally:
                _ai_bulk.JOBS.finish(g.id)
            msg = f"✅ {'Gave' if adding else 'Took'} **{safe_text(r.name, 60)}** {prep} **{done}** of {len(ids)} members."
            if failed:
                msg += f" {failed} could not be changed."
            if stopped:
                msg += f" Stopped early: {stopped}"
            try:
                await channel.send(f"{who_id.mention} {msg}", allowed_mentions=discord.AllowedMentions(users=[who_id], roles=False, everyone=False))
            except Exception:
                logger.debug("[ai_server_ops] bulk role summary not sent", exc_info=True)

        async def run():
            busy = _ai_bulk.JOBS.try_start(g.id)
            if busy:
                return f"❌ {busy}"
            task = asyncio.create_task(job())
            AIServerOpsCog._BULK_TASKS.add(task)
            task.add_done_callback(AIServerOpsCog._BULK_TASKS.discard)
            return f"⏳ Started: {verb.lower()}ing **{safe_text(r.name, 60)}** {prep} {len(ids)} members. I'll post here when it is done."
        return Plan(None, text, run, None)

    @member_g.command(name="role-add-all", description="Give a role to many members at once")
    @app_commands.describe(role="Role name, @mention or ID", who="everyone, humans or bots", has_role="Only members who already have this role")
    async def member_role_add_all(self, interaction: discord.Interaction, role: str, who: str = "everyone", has_role: str = ""):
        await self._slash(interaction, "member_role_add_all", role=role, who=who, has_role=has_role)

    async def plan_member_role_add_all(self, i, role=None, who="everyone", has_role="", **_):
        return await self._bulk_role_plan(i, role, who, has_role, True)

    @member_g.command(name="role-remove-all", description="Take a role away from many members at once")
    @app_commands.describe(role="Role name, @mention or ID", who="everyone, humans or bots", has_role="Only members who also have this role")
    async def member_role_remove_all(self, interaction: discord.Interaction, role: str, who: str = "everyone", has_role: str = ""):
        await self._slash(interaction, "member_role_remove_all", role=role, who=who, has_role=has_role)

    async def plan_member_role_remove_all(self, i, role=None, who="everyone", has_role="", **_):
        return await self._bulk_role_plan(i, role, who, has_role, False)

    @member_g.command(name="nickname", description="Change or reset a member's server nickname")
    @app_commands.describe(member="The member (@mention)", nickname="New nickname (max 32 chars); leave empty to reset")
    async def member_nickname(self, interaction: discord.Interaction, member: discord.Member, nickname: str = ""):
        await self._slash(interaction, "member_nickname", member=member, nickname=nickname)

    async def plan_member_nickname(self, i, member=None, nickname="", **_):
        if err := _need(i, "manage_nicknames"):
            return _fail(err)
        if member is None:
            return _fail("Tell me which member — @mention them.")
        if err := member_problem(i.guild, i.user, member, allow_self=True):
            return _fail(err)
        nick = clip(nickname, 32) or None
        if nick == member.nick or (nick is None and member.nick is None):
            return _fail("That's already their nickname.")

        async def run():
            await member.edit(nick=nick, reason=reason_for(i.user))
            return f"✅ {member.mention}'s nickname is now **{nick}**." if nick else f"✅ Reset {member.mention}'s nickname."
        what = f"set {member.mention}'s nickname to **{safe_text(nick, 32)}**" if nick else f"reset {member.mention}'s nickname"
        return Plan(None, f"{what[0].upper()}{what[1:]}?", run, member.id)

    # ═══════════════════════════════ ROLES ═══════════════════════════════

    roleedit_g = app_commands.guild_only()(app_commands.Group(name="roleedit", description="Edit existing roles"))

    @roleedit_g.command(name="edit", description="Edit a role's name, colour, display or permissions")
    @app_commands.describe(role="Role name, @mention or ID", name="New name", color="New colour (name or #hex, or 'none')",
                           hoist="Show members separately", mentionable="Let anyone @mention it",
                           grant="Permissions to ADD, comma-separated", revoke="Permissions to REMOVE, comma-separated")
    async def roleedit_edit(self, interaction: discord.Interaction, role: str, name: str = "", color: str = "",
                            hoist: Optional[bool] = None, mentionable: Optional[bool] = None,
                            grant: str = "", revoke: str = ""):
        await self._slash(interaction, "roleedit_edit", role=role, name=name, color=color, hoist=hoist,
                          mentionable=mentionable, grant=grant, revoke=revoke)

    async def plan_roleedit_edit(self, i, role=None, name="", color="", hoist=None, mentionable=None,
                                 grant="", revoke="", **_):
        if err := _need(i, "manage_roles"):
            return _fail(err)
        g = i.guild
        r, err = _find_role(g, role)
        if err:
            return _fail(err)
        if err := role_problem(g, i.user, r):
            return _fail(err)
        kw, notes = {}, []
        if str(name or "").strip():
            new = _ai_bulk.role_name(name)
            if not new or new.lower() in ("everyone", "here"):
                return _fail("That isn't a name I can use for a role.")
            if new != r.name:
                kw["name"] = new
                notes.append(f"rename to **{new}**")
        if str(color or "").strip():
            c = 0 if str(color).strip().lower() in ("none", "default", "clear", "reset") else _ai_bulk.parse_color(color)
            if c == -1:
                return _fail("I don't know that colour — use a name like red/blue or hex like #ff8800.")
            if c != r.color.value:
                kw["color"] = discord.Colour(c)
                notes.append("colour " + ("reset" if c == 0 else f"#{c:06x}"))
        if hoist is not None and bool(hoist) != r.hoist:
            kw["hoist"] = bool(hoist)
            notes.append("show members separately" if hoist else "stop showing members separately")
        if mentionable is not None and bool(mentionable) != r.mentionable:
            kw["mentionable"] = bool(mentionable)
            notes.append("make it mentionable" if mentionable else "make it not mentionable")
        add, bad_a = parse_perms(grant)
        rem, bad_r = parse_perms(revoke)
        if bad_a or bad_r:
            return _fail("I don't know these permissions: " + ", ".join(f"`{safe_text(b, 30)}`" for b in bad_a + bad_r))
        if set(add) & set(rem):
            return _fail("The same permission can't be both added and removed.")
        if add or rem:
            gp = _actor(i).guild_permissions
            if err := grant_problem(lambda n: getattr(gp, n, False), add, privileged=is_privileged(g, i.user)):
                return _fail(err)
            perms = discord.Permissions(r.permissions.value)
            perms.update(**{n: True for n in add}, **{n: False for n in rem})
            if perms.value != r.permissions.value:
                kw["permissions"] = perms
                if add:
                    notes.append("add " + ", ".join(nice_perm(n) for n in add))
                if rem:
                    notes.append("remove " + ", ".join(nice_perm(n) for n in rem))
        if not kw:
            return _fail("That wouldn't change anything.")

        async def run():
            await r.edit(**kw, reason=reason_for(i.user))
            return f"✅ Updated **{r.name}**: " + "; ".join(notes) + "."
        return Plan(None, f"Edit role **{safe_text(r.name, 60)}**: " + "; ".join(notes) + ".", run)

    @roleedit_g.command(name="move", description="Change a role's position in the hierarchy")
    @app_commands.describe(role="Role name, @mention or ID", above="Put it directly above this role (name)",
                           position="Or an exact position (1 = lowest, just above @everyone)")
    async def roleedit_move(self, interaction: discord.Interaction, role: str, above: str = "",
                            position: Optional[int] = None):
        await self._slash(interaction, "roleedit_move", role=role, above=above, position=position)

    async def plan_roleedit_move(self, i, role=None, above="", position=None, **_):
        if err := _need(i, "manage_roles"):
            return _fail(err)
        g = i.guild
        r, err = _find_role(g, role)
        if err:
            return _fail(err)
        if err := role_problem(g, i.user, r):
            return _fail(err)
        if str(above or "").strip():
            ref, err = _find_role(g, above)
            if err:
                return _fail(err)
            target = ref.position if r.position < ref.position else ref.position + 1
        elif position is not None:
            target = int(position)
        else:
            return _fail("Tell me where: put it `above` another role, or give a `position`.")
        ceiling = g.me.top_role.position - 1
        if not ops.is_owner(g, i.user):
            ceiling = min(ceiling, _actor(i).top_role.position - 1)
        target = max(1, target)
        if target > ceiling:
            return _fail(f"I can only place it at position {ceiling} or lower — roles at or above "
                         "yours (or mine) are off limits.")
        if target == r.position:
            return _fail(f"**{r.name}** is already there.")

        async def run():
            await r.edit(position=target, reason=reason_for(i.user))
            return f"✅ Moved **{r.name}** to position {target}."
        return Plan(None, f"Move role **{safe_text(r.name, 60)}** from position {r.position} to {target}?", run)

    # ═══════════════════════════════ CHANNELS ════════════════════════════

    channeledit_g = app_commands.guild_only()(app_commands.Group(name="channeledit", description="Edit existing channels"))

    @channeledit_g.command(name="lock", description="Lock a channel so a role can't send messages / join voice")
    @app_commands.describe(channel="The channel (#mention, name, or 'this')", role="Who is locked out (default @everyone)")
    async def channeledit_lock(self, interaction: discord.Interaction, channel: Optional[discord.abc.GuildChannel] = None,
                               role: str = ""):
        await self._slash(interaction, "channeledit_lock", channel=channel, role=role)

    async def _lock_plan(self, i, channel, role, lock: bool):
        ch, err = _chan(i, channel)
        if err:
            return _fail(err)
        if isinstance(ch, discord.Thread):
            return _fail("Use the thread tools for threads.")
        if err := _chan_need(i, ch, "manage_channels", "manage_roles"):
            return _fail(err)
        target, err = (i.guild.default_role, None) if not str(role or "").strip() else _find_role(i.guild, role)
        if err:
            return _fail(err)
        if err := role_problem(i.guild, i.user, target, allow_everyone=True):
            return _fail(err)
        keys = _lock_keys(ch)
        ow = ch.overwrites_for(target)
        now_locked = all(getattr(ow, k) is False for k in keys)
        if lock and now_locked:
            return _fail(f"{ch.mention} is already locked for **{target.name}**.")
        if not lock and not any(getattr(ow, k) is False for k in keys):
            return _fail(f"{ch.mention} isn't locked for **{target.name}**.")
        who = "everyone" if target.is_default() else f"**{safe_text(target.name, 60)}**"

        async def run():
            new = ch.overwrites_for(target)
            new.update(**{k: (False if lock else None) for k in keys})
            await ch.set_permissions(target, overwrite=None if new.is_empty() else new, reason=reason_for(i.user))
            return f"🔒 Locked {ch.mention} for {who}." if lock else f"🔓 Unlocked {ch.mention} for {who}."
        note = " (a role with an explicit Allow here can still get through)" if lock else ""
        return Plan(None, f"{'Lock' if lock else 'Unlock'} {ch.mention} for {who}{note}?", run)

    async def plan_channeledit_lock(self, i, channel=None, role="", **_):
        return await self._lock_plan(i, channel, role, True)

    @channeledit_g.command(name="unlock", description="Undo a channel lock for a role")
    @app_commands.describe(channel="The channel (#mention, name, or 'this')", role="Who to unlock (default @everyone)")
    async def channeledit_unlock(self, interaction: discord.Interaction, channel: Optional[discord.abc.GuildChannel] = None,
                                 role: str = ""):
        await self._slash(interaction, "channeledit_unlock", channel=channel, role=role)

    async def plan_channeledit_unlock(self, i, channel=None, role="", **_):
        return await self._lock_plan(i, channel, role, False)

    @channeledit_g.command(name="slowmode", description="Set a channel's slowmode (seconds between messages)")
    @app_commands.describe(channel="The channel (#mention, name, or 'this')", seconds="0 turns it off, max 21600")
    async def channeledit_slowmode(self, interaction: discord.Interaction, seconds: int,
                                   channel: Optional[discord.abc.GuildChannel] = None):
        await self._slash(interaction, "channeledit_slowmode", channel=channel, seconds=seconds)

    async def plan_channeledit_slowmode(self, i, channel=None, seconds=None, **_):
        ch, err = _chan(i, channel)
        if err:
            return _fail(err)
        if not isinstance(ch, (discord.TextChannel, discord.VoiceChannel, discord.ForumChannel)):
            return _fail("Slowmode only works on text, voice and forum channels.")
        if err := _chan_need(i, ch, "manage_channels"):
            return _fail(err)
        try:
            secs = int(seconds)
        except (TypeError, ValueError):
            return _fail("Tell me how many seconds (0–21600).")
        if not 0 <= secs <= 21600:
            return _fail("Slowmode has to be between 0 and 21600 seconds (6 hours).")
        if secs == ch.slowmode_delay:
            return _fail("It's already set to that.")

        async def run():
            await ch.edit(slowmode_delay=secs, reason=reason_for(i.user))
            return f"✅ Slowmode in {ch.mention} is now {secs}s." if secs else f"✅ Slowmode is off in {ch.mention}."
        return Plan(None, f"Set slowmode in {ch.mention} to {secs}s?" if secs else f"Turn slowmode off in {ch.mention}?", run)

    @channeledit_g.command(name="topic", description="Set a channel's topic / description")
    @app_commands.describe(channel="The channel (#mention, name, or 'this')", topic="New topic; leave empty to clear")
    async def channeledit_topic(self, interaction: discord.Interaction, topic: str = "",
                                channel: Optional[discord.abc.GuildChannel] = None):
        await self._slash(interaction, "channeledit_topic", channel=channel, topic=topic)

    async def plan_channeledit_topic(self, i, channel=None, topic="", **_):
        ch, err = _chan(i, channel)
        if err:
            return _fail(err)
        if not isinstance(ch, (discord.TextChannel, discord.ForumChannel)):
            return _fail("Only text and forum channels have a topic.")
        if err := _chan_need(i, ch, "manage_channels"):
            return _fail(err)
        text = str(topic or "").strip()
        limit = 4096 if isinstance(ch, discord.ForumChannel) else 1024
        if len(text) > limit:
            return _fail(f"That topic is {len(text)} characters — the limit here is {limit}.")
        if text == (ch.topic or ""):
            return _fail("That's already the topic.")

        async def run():
            await ch.edit(topic=text or None, reason=reason_for(i.user))
            return f"✅ Updated the topic of {ch.mention}." if text else f"✅ Cleared the topic of {ch.mention}."
        return Plan(None, (f"Set the topic of {ch.mention} to “{safe_text(text, 120)}”?" if text
                           else f"Clear the topic of {ch.mention}?"), run)

    @channeledit_g.command(name="move", description="Move a channel to another category and/or reorder it")
    @app_commands.describe(channel="The channel (#mention or name)",
                           category="Category name/ID, or 'none' to take it out of its category",
                           position="Position inside the category (1 = top)")
    async def channeledit_move(self, interaction: discord.Interaction, channel: discord.abc.GuildChannel,
                               category: str = "", position: Optional[int] = None):
        await self._slash(interaction, "channeledit_move", channel=channel, category=category, position=position)

    async def plan_channeledit_move(self, i, channel=None, category="", position=None, **_):
        if channel is None:
            return _fail("Tell me which channel to move.")
        if isinstance(channel, discord.Thread):
            return _fail("Threads can't be moved.")
        if err := _chan_need(i, channel, "manage_channels"):
            return _fail(err)
        kw, notes = {}, []
        raw = str(category or "").strip()
        if raw:
            if isinstance(channel, discord.CategoryChannel):
                return _fail("Categories can't go inside other categories.")
            if raw.lower() in ("none", "no category", "nothing", "uncategorized", "uncategorised"):
                cat = None
            else:
                cat, perr = _ai_bulk_ops.find_category(i.guild, raw)
                if perr:
                    return _fail(perr)
            if cat != channel.category:
                kw["category"] = cat
                notes.append(f"into **{safe_text(cat.name, 60)}**" if cat else "out of its category")
        if position is not None:
            if int(position) < 1:
                return _fail("Positions start at 1.")
            kw["position"] = int(position) - 1
            notes.append(f"to position {int(position)}")
        if not kw:
            return _fail("Tell me where to move it (a `category` and/or a `position`).")

        async def run():
            await channel.edit(**kw, reason=reason_for(i.user))
            return f"✅ Moved {channel.mention} " + " ".join(notes) + "."
        return Plan(None, f"Move {channel.mention} " + " ".join(notes) + "?", run)

    @channeledit_g.command(name="permissions", description="Allow/deny permissions for a role or member in a channel")
    @app_commands.describe(channel="The channel (#mention, name, or 'this')", target="Role name/@mention or member @mention",
                           allow="Permissions to ALLOW, comma-separated", deny="Permissions to DENY, comma-separated",
                           reset="Permissions to put back to 'inherit'", clear="Remove this target's override entirely")
    async def channeledit_permissions(self, interaction: discord.Interaction, target: str,
                                      channel: Optional[discord.abc.GuildChannel] = None, allow: str = "",
                                      deny: str = "", reset: str = "", clear: Optional[bool] = None):
        await self._slash(interaction, "channeledit_permissions", channel=channel, target=target, allow=allow,
                          deny=deny, reset=reset, clear=clear)

    async def plan_channeledit_permissions(self, i, channel=None, target=None, allow="", deny="", reset="",
                                           clear=None, **_):
        ch, err = _chan(i, channel)
        if err:
            return _fail(err)
        if isinstance(ch, discord.Thread):
            return _fail("Threads inherit their parent channel's permissions.")
        if err := _chan_need(i, ch, "manage_roles"):
            return _fail(err)
        tgt, err = _find_target(i.guild, target)
        if err:
            return _fail(err)
        err = (role_problem(i.guild, i.user, tgt, allow_everyone=True) if not _is_member(tgt)
               else member_problem(i.guild, i.user, tgt, allow_self=True))
        if err:
            return _fail(err)
        a, bad1 = parse_perms(allow, only=CHANNEL_PERMS)
        d, bad2 = parse_perms(deny, only=CHANNEL_PERMS)
        z, bad3 = parse_perms(reset, only=CHANNEL_PERMS)
        if bad1 or bad2 or bad3:
            return _fail("I can't use these as channel permissions: "
                         + ", ".join(f"`{safe_text(b, 30)}`" for b in bad1 + bad2 + bad3))
        if len(set(a) | set(d) | set(z)) != len(a) + len(d) + len(z):
            return _fail("A permission can only be in one of allow / deny / reset.")
        name = tgt.name if not _is_member(tgt) else tgt.display_name
        who = "everyone" if not _is_member(tgt) and tgt.is_default() else f"**{safe_text(name, 60)}**"
        if clear:
            if a or d or z:
                return _fail("Use either `clear` or the allow/deny/reset lists, not both.")
            if tgt not in ch.overwrites:
                return _fail(f"{who} has no override in {ch.mention}.")

            async def run_clear():
                await ch.set_permissions(tgt, overwrite=None, reason=reason_for(i.user))
                return f"✅ Removed the override for {who} in {ch.mention}."
            return Plan(None, f"Remove the permission override for {who} in {ch.mention}?", run_clear,
                        getattr(tgt, "id", None) if _is_member(tgt) else None)
        if not (a or d or z):
            return _fail("Tell me what to `allow`, `deny` or `reset`.")
        mine = ch.permissions_for(_actor(i))
        if err := grant_problem(lambda n: getattr(mine, n, False), a, privileged=is_privileged(i.guild, i.user)):
            return _fail(err)
        before = ch.overwrites_for(tgt)
        new = ch.overwrites_for(tgt)
        new.update(**{n: True for n in a}, **{n: False for n in d}, **{n: None for n in z})
        if new == before:
            return _fail("That wouldn't change anything.")
        bits = ([f"allow {', '.join(nice_perm(n) for n in a)}"] if a else []) \
            + ([f"deny {', '.join(nice_perm(n) for n in d)}"] if d else []) \
            + ([f"reset {', '.join(nice_perm(n) for n in z)}"] if z else [])

        async def run():
            await ch.set_permissions(tgt, overwrite=None if new.is_empty() else new, reason=reason_for(i.user))
            return f"✅ Updated {who} in {ch.mention}: " + "; ".join(bits) + "."
        return Plan(None, f"In {ch.mention}, for {who}: " + "; ".join(bits) + ".", run,
                    tgt.id if _is_member(tgt) else None)

    # ═══════════════════════════════ THREADS ═════════════════════════════

    thread_g = app_commands.guild_only()(app_commands.Group(name="thread", description="Manage threads"))

    @thread_g.command(name="create", description="Create a thread in a text channel")
    @app_commands.describe(name="Thread name", channel="Parent text channel (default: this one)",
                           message="Optional message ID/link to start the thread from",
                           private="Make it a private thread")
    async def thread_create(self, interaction: discord.Interaction, name: str,
                            channel: Optional[discord.abc.GuildChannel] = None, message: str = "",
                            private: Optional[bool] = None):
        await self._slash(interaction, "thread_create", name=name, channel=channel, message=message, private=private)

    async def plan_thread_create(self, i, name="", channel=None, message="", private=None, **_):
        ch, err = _chan(i, channel)
        if err:
            return _fail(err)
        if isinstance(ch, discord.Thread):
            ch = ch.parent
        if not isinstance(ch, discord.TextChannel):
            return _fail("I can only start threads in text channels.")
        if err := _chan_need(i, ch, "manage_threads", "create_private_threads" if private else "create_public_threads"):
            return _fail(err)
        title = clip(name, 100)
        if not title:
            return _fail("Give the thread a name.")
        _cid, mid = ops.message_ref(message) if str(message or "").strip() else (None, None)
        if str(message or "").strip() and mid is None:
            return _fail("I couldn't read that message ID/link.")
        if private and mid:
            return _fail("A private thread can't be started from a message.")

        async def run():
            kw = dict(name=title, reason=reason_for(i.user))
            if mid:
                t = await ch.create_thread(message=ch.get_partial_message(mid), **kw)
            else:
                t = await ch.create_thread(type=discord.ChannelType.private_thread if private else
                                           discord.ChannelType.public_thread, **kw)
            return f"✅ Created {t.mention}."
        return Plan(None, f"Create {'a private ' if private else 'a '}thread **{safe_text(title, 60)}** in {ch.mention}?", run)

    @thread_g.command(name="state", description="Archive/unarchive or lock/unlock a thread")
    @app_commands.describe(thread="Thread name, #mention or ID", archived="True to archive, false to reopen",
                           locked="True to lock (only moderators can post), false to unlock")
    async def thread_state(self, interaction: discord.Interaction, thread: str, archived: Optional[bool] = None,
                           locked: Optional[bool] = None):
        await self._slash(interaction, "thread_state", thread=thread, archived=archived, locked=locked)

    async def plan_thread_state(self, i, thread=None, archived=None, locked=None, **_):
        t, err = await _find_thread(i.guild, thread)
        if err:
            return _fail(err)
        if err := _chan_need(i, t.parent or t, "manage_threads"):
            return _fail(err)
        kw, notes = {}, []
        if archived is not None and bool(archived) != t.archived:
            kw["archived"] = bool(archived)
            notes.append("archive it" if archived else "reopen it")
        if locked is not None and bool(locked) != t.locked:
            kw["locked"] = bool(locked)
            notes.append("lock it" if locked else "unlock it")
        if not kw:
            return _fail("Tell me what to change (`archived` and/or `locked`) — it's already that way otherwise.")

        async def run():
            await t.edit(**kw, reason=reason_for(i.user))
            return f"✅ {t.mention}: " + ", ".join(notes) + "."
        return Plan(None, f"For {t.mention}: " + ", ".join(notes) + "?", run)

    @thread_g.command(name="rename", description="Rename a thread")
    @app_commands.describe(thread="Thread name, #mention or ID", name="New name")
    async def thread_rename(self, interaction: discord.Interaction, thread: str, name: str):
        await self._slash(interaction, "thread_rename", thread=thread, name=name)

    async def plan_thread_rename(self, i, thread=None, name="", **_):
        t, err = await _find_thread(i.guild, thread)
        if err:
            return _fail(err)
        if err := _chan_need(i, t.parent or t, "manage_threads"):
            return _fail(err)
        title = clip(name, 100)
        if not title or title == t.name:
            return _fail("Give me a new name for it.")

        async def run():
            await t.edit(name=title, reason=reason_for(i.user))
            return f"✅ Renamed the thread to **{title}**."
        return Plan(None, f"Rename {t.mention} to **{safe_text(title, 60)}**?", run)

    @thread_g.command(name="delete", description="Permanently delete a thread")
    @app_commands.describe(thread="Thread name, #mention or ID")
    async def thread_delete(self, interaction: discord.Interaction, thread: str):
        await self._slash(interaction, "thread_delete", thread=thread)

    async def plan_thread_delete(self, i, thread=None, **_):
        t, err = await _find_thread(i.guild, thread)
        if err:
            return _fail(err)
        if err := _chan_need(i, t.parent or t, "manage_threads"):
            return _fail(err)
        title = t.name

        async def run():
            await t.delete(reason=reason_for(i.user))
            return f"🗑️ Deleted the thread **{safe_text(title, 60)}**."
        return Plan(None, f"⚠️ Permanently delete the thread **{safe_text(title, 60)}** and all its messages? This can't be undone.", run)

    async def _thread_member(self, i, thread, member, add: bool):
        t, err = await _find_thread(i.guild, thread)
        if err:
            return _fail(err)
        if member is None:
            return _fail("Tell me which member — @mention them.")
        if err := _chan_need(i, t.parent or t, "manage_threads"):
            return _fail(err)

        async def run():
            await (t.add_user(member) if add else t.remove_user(member))
            return f"✅ {'Added' if add else 'Removed'} {member.mention} {'to' if add else 'from'} {t.mention}."
        return Plan(None, f"{'Add' if add else 'Remove'} {member.mention} {'to' if add else 'from'} {t.mention}?", run, member.id)

    @thread_g.command(name="member-add", description="Add a member to a thread")
    @app_commands.describe(thread="Thread name, #mention or ID", member="The member (@mention)")
    async def thread_member_add(self, interaction: discord.Interaction, thread: str, member: discord.Member):
        await self._slash(interaction, "thread_member_add", thread=thread, member=member)

    async def plan_thread_member_add(self, i, thread=None, member=None, **_):
        return await self._thread_member(i, thread, member, True)

    @thread_g.command(name="member-remove", description="Remove a member from a thread")
    @app_commands.describe(thread="Thread name, #mention or ID", member="The member (@mention)")
    async def thread_member_remove(self, interaction: discord.Interaction, thread: str, member: discord.Member):
        await self._slash(interaction, "thread_member_remove", thread=thread, member=member)

    async def plan_thread_member_remove(self, i, thread=None, member=None, **_):
        return await self._thread_member(i, thread, member, False)

    # ═══════════════════════════════ VOICE ═══════════════════════════════

    voice_g = app_commands.guild_only()(app_commands.Group(name="voice", description="Voice moderation"))

    @staticmethod
    def _in_voice(member) -> Optional[str]:
        vs = getattr(member, "voice", None)
        if vs is None or vs.channel is None:
            return f"{safe_text(member.display_name, 32)} isn't in a voice channel right now."
        return None

    @voice_g.command(name="move", description="Move a member to another voice channel")
    @app_commands.describe(member="The member (@mention)", channel="Destination voice channel (name or #mention)")
    async def voice_move(self, interaction: discord.Interaction, member: discord.Member, channel: discord.abc.GuildChannel):
        await self._slash(interaction, "voice_move", member=member, channel=channel)

    async def plan_voice_move(self, i, member=None, channel=None, **_):
        if err := _need(i, "move_members"):
            return _fail(err)
        if member is None:
            return _fail("Tell me which member — @mention them.")
        if not isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
            return _fail("Tell me which voice channel to move them to.")
        if err := self._in_voice(member):
            return _fail(err)
        if member.voice.channel.id == channel.id:
            return _fail("They're already in that channel.")
        # Discord needs Connect + Move Members for the BOT on the destination; the asker only needs Move Members.
        if _missing(channel.permissions_for(i.guild.me), ("connect", "move_members")):
            return _fail(f"I need **Connect** and **Move Members** in **{safe_text(channel.name, 40)}** to move someone there.")

        async def run():
            await member.move_to(channel, reason=reason_for(i.user))
            return f"✅ Moved {member.mention} to **{safe_text(channel.name, 60)}**."
        return Plan(None, f"Move {member.mention} to voice channel **{safe_text(channel.name, 60)}**?", run, member.id)

    @voice_g.command(name="disconnect", description="Disconnect a member from voice")
    @app_commands.describe(member="The member (@mention)")
    async def voice_disconnect(self, interaction: discord.Interaction, member: discord.Member):
        await self._slash(interaction, "voice_disconnect", member=member)

    async def plan_voice_disconnect(self, i, member=None, **_):
        if err := _need(i, "move_members"):
            return _fail(err)
        if member is None:
            return _fail("Tell me which member — @mention them.")
        if err := self._in_voice(member):
            return _fail(err)

        async def run():
            await member.move_to(None, reason=reason_for(i.user))
            return f"✅ Disconnected {member.mention} from voice."
        return Plan(None, f"Disconnect {member.mention} from voice?", run, member.id)

    async def _voice_flag(self, i, member, value, flag: str, perm: str, verb_on: str, verb_off: str):
        """flag is the edit() keyword ('mute' / 'deafen'); the VoiceState attribute is 'mute' / 'deaf'."""
        if err := _need(i, perm):
            return _fail(err)
        if member is None:
            return _fail("Tell me which member — @mention them.")
        if err := self._in_voice(member):
            return _fail(err)
        want = True if value is None else bool(value)
        if getattr(member.voice, "deaf" if flag == "deafen" else "mute") == want:
            return _fail(f"They're already {verb_on if want else verb_off}.")

        async def run():
            await member.edit(**{flag: want}, reason=reason_for(i.user))
            return f"✅ {member.mention} is now {verb_on if want else verb_off}."
        text = f"Server-{flag} {member.mention}?" if want else f"Remove the server {flag} from {member.mention}?"
        return Plan(None, text, run, member.id)

    @voice_g.command(name="mute", description="Server-mute or unmute a member in voice")
    @app_commands.describe(member="The member (@mention)", muted="True to mute (default), false to unmute")
    async def voice_mute(self, interaction: discord.Interaction, member: discord.Member, muted: Optional[bool] = None):
        await self._slash(interaction, "voice_mute", member=member, muted=muted)

    async def plan_voice_mute(self, i, member=None, muted=None, **_):
        return await self._voice_flag(i, member, muted, "mute", "mute_members", "server-muted", "unmuted")

    @voice_g.command(name="deafen", description="Server-deafen or undeafen a member in voice")
    @app_commands.describe(member="The member (@mention)", deafened="True to deafen (default), false to undeafen")
    async def voice_deafen(self, interaction: discord.Interaction, member: discord.Member, deafened: Optional[bool] = None):
        await self._slash(interaction, "voice_deafen", member=member, deafened=deafened)

    async def plan_voice_deafen(self, i, member=None, deafened=None, **_):
        return await self._voice_flag(i, member, deafened, "deafen", "deafen_members", "server-deafened", "undeafened")

    # ═══════════════════════════════ INVITES ═════════════════════════════

    serverinvite_g = app_commands.guild_only()(app_commands.Group(name="serverinvite", description="Server invite links"))

    @serverinvite_g.command(name="create", description="Create an invite link for a channel")
    @app_commands.describe(channel="Channel to invite to (default: this one)", max_age_hours="Hours until it expires (0 = never)",
                           max_uses="How many times it can be used (0 = unlimited)",
                           temporary="Members who join only stay while they're online")
    async def serverinvite_create(self, interaction: discord.Interaction, channel: Optional[discord.abc.GuildChannel] = None,
                                  max_age_hours: Optional[int] = None, max_uses: Optional[int] = None,
                                  temporary: Optional[bool] = None):
        await self._slash(interaction, "serverinvite_create", channel=channel, max_age_hours=max_age_hours,
                          max_uses=max_uses, temporary=temporary)

    async def plan_serverinvite_create(self, i, channel=None, max_age_hours=None, max_uses=None, temporary=None, **_):
        ch, err = _chan(i, channel)
        if err:
            return _fail(err)
        if isinstance(ch, (discord.Thread, discord.CategoryChannel)):
            return _fail("Pick a text or voice channel for the invite.")
        if err := _chan_need(i, ch, "create_instant_invite"):
            return _fail(err)
        hours = 24 if max_age_hours is None else int(max_age_hours)
        uses = 0 if max_uses is None else int(max_uses)
        if not 0 <= hours <= 168:
            return _fail("An invite can last 0 (never) to 168 hours (7 days).")
        if not 0 <= uses <= 100:
            return _fail("Max uses can be 0 (unlimited) to 100.")
        desc = (f"expires in {hours}h" if hours else "never expires") + (f", {uses} use(s)" if uses else ", unlimited uses") \
            + (", temporary membership" if temporary else "")

        async def run():
            inv = await ch.create_invite(max_age=hours * 3600, max_uses=uses, temporary=bool(temporary),
                                         unique=True, reason=reason_for(i.user))
            return f"✅ Invite for {ch.mention} ({desc}): {inv.url}"
        return Plan(None, f"Create an invite to {ch.mention} ({desc})?", run)

    @serverinvite_g.command(name="delete", description="Delete (revoke) an invite link")
    @app_commands.describe(invite="The invite code or link")
    async def serverinvite_delete(self, interaction: discord.Interaction, invite: str):
        await self._slash(interaction, "serverinvite_delete", invite=invite)

    @staticmethod
    def _invite_code(raw) -> Optional[str]:
        m = re.search(r"(?:discord\.gg|discord(?:app)?\.com/invite)/([A-Za-z0-9-]+)", str(raw or "")) \
            or re.fullmatch(r"\s*([A-Za-z0-9-]{2,32})\s*", str(raw or ""))
        return m.group(1) if m else None

    async def plan_serverinvite_delete(self, i, invite="", **_):
        if err := _need(i, "manage_guild"):
            return _fail(err)
        code = self._invite_code(invite)
        if not code:
            return _fail("Give me the invite code or link.")
        found = discord.utils.find(lambda v: v.code == code, await i.guild.invites())
        if found is None:
            return _fail(f"I couldn't find an invite with the code `{safe_text(code, 20)}` in this server.")

        async def run():
            await found.delete(reason=reason_for(i.user))
            return f"🗑️ Deleted invite `{found.code}`."
        return Plan(None, f"Delete invite `{found.code}` (to {found.channel.mention if found.channel else 'a channel'}, "
                          f"{found.uses or 0} use(s) so far)?", run)

    @serverinvite_g.command(name="list", description="List this server's active invites")
    async def serverinvite_list(self, interaction: discord.Interaction):
        await self._slash(interaction, "serverinvite_list")

    async def plan_serverinvite_list(self, i, **_):
        if err := _need(i, "manage_guild"):
            return _fail(err)

        async def run():
            invs = await i.guild.invites()
            if not invs:
                return "There are no active invites."
            lines = [f"`{v.code}` → {v.channel.mention if v.channel else '?'} · {v.uses or 0}/{v.max_uses or '∞'} uses · "
                     f"by {safe_text(getattr(v.inviter, 'name', 'unknown'), 24)} · "
                     f"{'expires ' + _ts(v.expires_at) if v.expires_at else 'never expires'}" for v in invs[:20]]
            more = f"\n…and {len(invs) - 20} more." if len(invs) > 20 else ""
            return f"**{len(invs)} invite(s)**\n" + "\n".join(lines) + more
        return Plan(None, "Show this server's invite list (only you will see it)?", run)

    # ═══════════════════════════════ MESSAGES / PINS ═════════════════════

    messageops_g = app_commands.guild_only()(app_commands.Group(name="messageops", description="Single-message tools"))

    async def _fetch_message(self, i, channel, raw):
        """(message, channel, error): a message from a link or an ID in the given/current channel."""
        cid, mid = ops.message_ref(raw)
        if mid is None:
            return None, None, "Give me the message ID or link."
        ch = (i.guild.get_channel_or_thread(cid) if cid else None) or _chan(i, channel)[0]
        if ch is None:
            return None, None, "Tell me which channel the message is in."
        if not hasattr(ch, "fetch_message"):
            return None, None, "That isn't a channel with messages."
        try:
            return await ch.fetch_message(mid), ch, None
        except discord.NotFound:
            return None, None, "I couldn't find that message there."
        except discord.Forbidden:
            return None, None, "I'm not allowed to read that channel."

    @messageops_g.command(name="delete", description="Delete one specific message")
    @app_commands.describe(message="Message ID or link", channel="Channel it's in (default: this one; not needed for a link)")
    async def messageops_delete(self, interaction: discord.Interaction, message: str,
                                channel: Optional[discord.abc.GuildChannel] = None):
        await self._slash(interaction, "messageops_delete", message=message, channel=channel)

    async def plan_messageops_delete(self, i, message="", channel=None, **_):
        msg, ch, err = await self._fetch_message(i, channel, message)
        if err:
            return _fail(err)
        if err := _chan_need(i, ch, "manage_messages", "read_message_history"):
            return _fail(err)
        preview = ops.safe_text(msg.content or ("[attachment/embed]" if (msg.attachments or msg.embeds) else "[no text]"), 100)

        async def run():
            await ch.get_partial_message(msg.id).delete()
            return f"🗑️ Deleted the message from **{safe_text(msg.author.display_name, 32)}** in {ch.mention}."
        return Plan(None, f"Delete this message by **{safe_text(msg.author.display_name, 32)}** in {ch.mention}: “{preview}”?",
                    run, getattr(msg.author, "id", None))

    async def _pin_plan(self, i, message, channel, pin: bool):
        msg, ch, err = await self._fetch_message(i, channel, message)
        if err:
            return _fail(err)
        if err := _chan_need(i, ch, "manage_messages"):
            return _fail(err)
        if pin == msg.pinned:
            return _fail("That message is already pinned." if pin else "That message isn't pinned.")
        preview = safe_text(msg.content or "[no text]", 100)

        async def run():
            await (msg.pin(reason=reason_for(i.user)) if pin else msg.unpin(reason=reason_for(i.user)))
            return f"📌 {'Pinned' if pin else 'Unpinned'} the message in {ch.mention}."
        return Plan(None, f"{'Pin' if pin else 'Unpin'} this message in {ch.mention}: “{preview}”?", run)

    @messageops_g.command(name="pin", description="Pin a message")
    @app_commands.describe(message="Message ID or link", channel="Channel it's in (default: this one)")
    async def messageops_pin(self, interaction: discord.Interaction, message: str,
                             channel: Optional[discord.abc.GuildChannel] = None):
        await self._slash(interaction, "messageops_pin", message=message, channel=channel)

    async def plan_messageops_pin(self, i, message="", channel=None, **_):
        return await self._pin_plan(i, message, channel, True)

    @messageops_g.command(name="unpin", description="Unpin a message")
    @app_commands.describe(message="Message ID or link", channel="Channel it's in (default: this one)")
    async def messageops_unpin(self, interaction: discord.Interaction, message: str,
                               channel: Optional[discord.abc.GuildChannel] = None):
        await self._slash(interaction, "messageops_unpin", message=message, channel=channel)

    async def plan_messageops_unpin(self, i, message="", channel=None, **_):
        return await self._pin_plan(i, message, channel, False)

    @messageops_g.command(name="pins", description="List the pinned messages in a channel")
    @app_commands.describe(channel="The channel (default: this one)")
    async def messageops_pins(self, interaction: discord.Interaction, channel: Optional[discord.abc.GuildChannel] = None):
        await self._slash(interaction, "messageops_pins", channel=channel)

    async def plan_messageops_pins(self, i, channel=None, **_):
        ch, err = _chan(i, channel)
        if err:
            return _fail(err)
        if not hasattr(ch, "pins"):
            return _fail("That channel has no pins.")
        if err := _chan_need(i, ch, "view_channel", "read_message_history"):
            return _fail(err)

        async def run():
            found = [getattr(p, "message", p) async for p in ch.pins(limit=25)]
            if not found:
                return f"No pinned messages in {ch.mention}."
            lines = [f"{n}. **{safe_text(m.author.display_name, 24)}**: {safe_text(m.content or '[attachment/embed]', 90)} "
                     f"([jump]({m.jump_url}))" for n, m in enumerate(found, 1)]
            return f"**Pins in {ch.mention}**\n" + "\n".join(lines)
        return Plan(None, f"Show the pinned messages in {ch.mention} (only you will see it)?", run)

    # ═══════════════════════════════ EMOJIS ══════════════════════════════

    emojiops_g = app_commands.guild_only()(app_commands.Group(name="emojiops", description="Custom emojis"))

    @emojiops_g.command(name="list", description="List the server's custom emojis")
    async def emojiops_list(self, interaction: discord.Interaction):
        await self._slash(interaction, "emojiops_list")

    async def plan_emojiops_list(self, i, **_):
        async def run():
            ems = i.guild.emojis
            if not ems:
                return "This server has no custom emojis."
            shown = " ".join(f"{e} `{e.name}`" for e in ems[:40])
            more = f"\n…and {len(ems) - 40} more." if len(ems) > 40 else ""
            return f"**{len(ems)}/{i.guild.emoji_limit * 2} emojis**\n{shown}{more}"
        return Plan(None, "List emojis", run)

    @emojiops_g.command(name="add", description="Add a custom emoji from a Discord CDN image link")
    @app_commands.describe(name="Emoji name (letters, numbers, underscores)",
                           image_url="A cdn.discordapp.com / media.discordapp.net image link (png/jpg/gif/webp, max 256 KB)")
    async def emojiops_add(self, interaction: discord.Interaction, name: str, image_url: str):
        await self._slash(interaction, "emojiops_add", name=name, image_url=image_url)

    async def plan_emojiops_add(self, i, name="", image_url="", **_):
        if err := _need(i, "manage_expressions"):
            return _fail(err)
        nm = re.sub(r"[^A-Za-z0-9_]", "_", str(name or "").strip().strip(":"))[:32]
        if len(nm) < 2:
            return _fail("An emoji name needs at least 2 letters, numbers or underscores.")
        if not ops.cdn_image_url_ok(image_url):
            return _fail("I can only add emojis from a Discord image link (cdn.discordapp.com or media.discordapp.net, "
                         "png/jpg/gif/webp). Upload the image in a message, then copy its link.")

        async def run():
            data = await _download_image(image_url.strip(), 256 * 1024)
            e = await i.guild.create_custom_emoji(name=nm, image=data, reason=reason_for(i.user))
            return f"✅ Added {e} as `:{e.name}:`."
        return Plan(None, f"Add a custom emoji named `:{nm}:` from that image link?", run)

    @emojiops_g.command(name="rename", description="Rename a custom emoji")
    @app_commands.describe(emoji="Emoji name, ID or the emoji itself", name="New name")
    async def emojiops_rename(self, interaction: discord.Interaction, emoji: str, name: str):
        await self._slash(interaction, "emojiops_rename", emoji=emoji, name=name)

    async def plan_emojiops_rename(self, i, emoji="", name="", **_):
        if err := _need(i, "manage_expressions"):
            return _fail(err)
        e, err = _find_by_name(i.guild.emojis, emoji, "emoji")
        if err:
            return _fail(err)
        nm = re.sub(r"[^A-Za-z0-9_]", "_", str(name or "").strip().strip(":"))[:32]
        if len(nm) < 2 or nm == e.name:
            return _fail("Give me a different new name (2+ letters, numbers or underscores).")

        async def run():
            await e.edit(name=nm, reason=reason_for(i.user))
            return f"✅ Renamed {e} to `:{nm}:`."
        return Plan(None, f"Rename {e} `:{e.name}:` to `:{nm}:`?", run)

    @emojiops_g.command(name="delete", description="Delete a custom emoji")
    @app_commands.describe(emoji="Emoji name, ID or the emoji itself")
    async def emojiops_delete(self, interaction: discord.Interaction, emoji: str):
        await self._slash(interaction, "emojiops_delete", emoji=emoji)

    async def plan_emojiops_delete(self, i, emoji="", **_):
        if err := _need(i, "manage_expressions"):
            return _fail(err)
        e, err = _find_by_name(i.guild.emojis, emoji, "emoji")
        if err:
            return _fail(err)

        async def run():
            await e.delete(reason=reason_for(i.user))
            return f"🗑️ Deleted the emoji `:{e.name}:`."
        return Plan(None, f"⚠️ Permanently delete the emoji {e} `:{e.name}:`?", run)

    # ═══════════════════════════════ STICKERS ════════════════════════════

    stickerops_g = app_commands.guild_only()(app_commands.Group(name="stickerops", description="Custom stickers"))

    @stickerops_g.command(name="list", description="List the server's custom stickers")
    async def stickerops_list(self, interaction: discord.Interaction):
        await self._slash(interaction, "stickerops_list")

    async def plan_stickerops_list(self, i, **_):
        async def run():
            sts = i.guild.stickers
            if not sts:
                return "This server has no custom stickers."
            return f"**{len(sts)} sticker(s)**\n" + "\n".join(
                f"• **{safe_text(s.name, 32)}** — {safe_text(s.description or 'no description', 60)}" for s in sts[:30])
        return Plan(None, "List stickers", run)

    @stickerops_g.command(name="edit", description="Rename a sticker or change its description")
    @app_commands.describe(sticker="Sticker name or ID", name="New name", description="New description")
    async def stickerops_edit(self, interaction: discord.Interaction, sticker: str, name: str = "", description: str = ""):
        await self._slash(interaction, "stickerops_edit", sticker=sticker, name=name, description=description)

    async def plan_stickerops_edit(self, i, sticker="", name="", description="", **_):
        if err := _need(i, "manage_expressions"):
            return _fail(err)
        s, err = _find_by_name(i.guild.stickers, sticker, "sticker")
        if err:
            return _fail(err)
        kw = {}
        if str(name or "").strip() and clip(name, 30) != s.name:
            if len(clip(name, 30)) < 2:
                return _fail("A sticker name needs at least 2 characters.")
            kw["name"] = clip(name, 30)
        if str(description or "").strip() and clip(description, 100) != (s.description or ""):
            kw["description"] = clip(description, 100)
        if not kw:
            return _fail("Tell me a new `name` and/or `description`.")

        async def run():
            await s.edit(**kw, reason=reason_for(i.user))
            return f"✅ Updated the sticker **{safe_text(s.name, 32)}**."
        return Plan(None, f"Update sticker **{safe_text(s.name, 32)}**: " + ", ".join(f"{k} → “{safe_text(v, 40)}”" for k, v in kw.items()) + "?", run)

    @stickerops_g.command(name="delete", description="Delete a custom sticker")
    @app_commands.describe(sticker="Sticker name or ID")
    async def stickerops_delete(self, interaction: discord.Interaction, sticker: str):
        await self._slash(interaction, "stickerops_delete", sticker=sticker)

    async def plan_stickerops_delete(self, i, sticker="", **_):
        if err := _need(i, "manage_expressions"):
            return _fail(err)
        s, err = _find_by_name(i.guild.stickers, sticker, "sticker")
        if err:
            return _fail(err)

        async def run():
            await s.delete(reason=reason_for(i.user))
            return f"🗑️ Deleted the sticker **{safe_text(s.name, 32)}**."
        return Plan(None, f"⚠️ Permanently delete the sticker **{safe_text(s.name, 32)}**?", run)

    # ═══════════════════════════════ WEBHOOKS ════════════════════════════

    webhookops_g = app_commands.guild_only()(app_commands.Group(name="webhookops", description="Webhooks"))

    @webhookops_g.command(name="list", description="List the server's webhooks (names and channels only)")
    async def webhookops_list(self, interaction: discord.Interaction):
        await self._slash(interaction, "webhookops_list")

    async def plan_webhookops_list(self, i, **_):
        if err := _need(i, "manage_webhooks"):
            return _fail(err)

        async def run():
            hooks = await i.guild.webhooks()
            if not hooks:
                return "This server has no webhooks."
            return f"**{len(hooks)} webhook(s)** (links are never shown)\n" + "\n".join(
                f"• **{safe_text(h.name or 'unnamed', 32)}** → {h.channel.mention if h.channel else '?'} · `{h.id}`"
                for h in hooks[:25])
        return Plan(None, "Show this server's webhooks (names and channels only, visible just to you)?", run)

    @webhookops_g.command(name="create", description="Create a webhook in a channel")
    @app_commands.describe(channel="The channel (default: this one)", name="Webhook name")
    async def webhookops_create(self, interaction: discord.Interaction, name: str,
                                channel: Optional[discord.abc.GuildChannel] = None):
        await self._slash(interaction, "webhookops_create", name=name, channel=channel)

    async def plan_webhookops_create(self, i, name="", channel=None, **_):
        ch, err = _chan(i, channel)
        if err:
            return _fail(err)
        if not isinstance(ch, (discord.TextChannel, discord.ForumChannel, discord.VoiceChannel, discord.StageChannel)):
            return _fail("Webhooks can only be made in text, forum and voice channels.")
        if err := _chan_need(i, ch, "manage_webhooks"):
            return _fail(err)
        nm = clip(name, 80)
        if len(nm) < 1:
            return _fail("Give the webhook a name.")

        async def run():
            h = await ch.create_webhook(name=nm, reason=reason_for(i.user))
            return (f"✅ Created webhook **{safe_text(nm, 40)}** in {ch.mention}.\n"
                    f"🔐 Its link lets anyone post as it — keep it secret (only you can see this):\n||{h.url}||")
        return Plan(None, f"Create a webhook **{safe_text(nm, 40)}** in {ch.mention}? (its link is shown only to you)", run)

    @webhookops_g.command(name="delete", description="Delete a webhook")
    @app_commands.describe(webhook="Webhook name or ID")
    async def webhookops_delete(self, interaction: discord.Interaction, webhook: str):
        await self._slash(interaction, "webhookops_delete", webhook=webhook)

    async def plan_webhookops_delete(self, i, webhook="", **_):
        if err := _need(i, "manage_webhooks"):
            return _fail(err)
        h, err = _find_by_name(await i.guild.webhooks(), webhook, "webhook", name_of=lambda x: x.name or "")
        if err:
            return _fail(err)

        async def run():
            await h.delete(reason=reason_for(i.user))
            return f"🗑️ Deleted the webhook **{safe_text(h.name, 40)}**."
        return Plan(None, f"⚠️ Delete the webhook **{safe_text(h.name, 40)}** ({h.channel.mention if h.channel else 'a channel'})?", run)

    # ═══════════════════════════════ SCHEDULED EVENTS ════════════════════

    eventops_g = app_commands.guild_only()(app_commands.Group(name="eventops", description="Scheduled events"))

    @eventops_g.command(name="list", description="List upcoming scheduled events")
    async def eventops_list(self, interaction: discord.Interaction):
        await self._slash(interaction, "eventops_list")

    async def plan_eventops_list(self, i, **_):
        async def run():
            evs = sorted(i.guild.scheduled_events, key=lambda e: e.start_time)
            if not evs:
                return "There are no scheduled events."
            return f"**{len(evs)} event(s)**\n" + "\n".join(
                f"• **{safe_text(e.name, 50)}** — {_ts(e.start_time)} · {e.status.name}" for e in evs[:15])
        return Plan(None, "List events", run)

    @eventops_g.command(name="create", description="Schedule a server event")
    @app_commands.describe(name="Event name", start="When it starts, in UTC: 'in 2 hours' or '2026-10-20 18:00'",
                           channel="Voice/stage channel for the event", location="Or a place/link for an outside event",
                           end="When it ends (required for a location event)", description="What it's about")
    async def eventops_create(self, interaction: discord.Interaction, name: str, start: str,
                              channel: Optional[discord.abc.GuildChannel] = None, location: str = "",
                              end: str = "", description: str = ""):
        await self._slash(interaction, "eventops_create", name=name, start=start, channel=channel, location=location,
                          end=end, description=description)

    async def plan_eventops_create(self, i, name="", start="", channel=None, location="", end="", description="", **_):
        if err := _need(i, "manage_events"):
            return _fail(err)
        title = clip(name, 100)
        if not title:
            return _fail("Give the event a name.")
        now = datetime.now(timezone.utc)
        st = ops.parse_when(start, now)
        if st is None:
            return _fail("I couldn't read the start time — use UTC like `in 2 hours` or `2026-10-20 18:00`.")
        if st <= now:
            return _fail("The start time has to be in the future.")
        en = ops.parse_when(end, now) if str(end or "").strip() else None
        if str(end or "").strip() and en is None:
            return _fail("I couldn't read the end time — use UTC like `in 3 hours` or `2026-10-20 20:00`.")
        if en is not None and en <= st:
            return _fail("The end has to be after the start.")
        loc = clip(location, 100)
        kw = dict(name=title, start_time=st, privacy_level=discord.PrivacyLevel.guild_only,
                  description=clip(description, 1000) or discord.utils.MISSING, reason=reason_for(i.user))
        if isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
            if err := _chan_need(i, channel, "view_channel", "connect"):
                return _fail(err)
            kw["channel"] = channel
            kw["entity_type"] = (discord.EntityType.stage_instance if isinstance(channel, discord.StageChannel)
                                 else discord.EntityType.voice)
            if en is not None:
                kw["end_time"] = en
            where = channel.mention
        elif loc:
            if en is None:
                return _fail("An event at a location needs an end time too.")
            kw.update(entity_type=discord.EntityType.external, location=loc, end_time=en)
            where = f"“{safe_text(loc, 60)}”"
        else:
            return _fail("Tell me where: a voice/stage `channel`, or a `location`.")

        async def run():
            ev = await i.guild.create_scheduled_event(**kw)
            return f"✅ Scheduled **{safe_text(ev.name, 60)}** for {_ts(ev.start_time)}. {ev.url}"
        return Plan(None, f"Schedule the event **{safe_text(title, 60)}** at {_ts(st)} in {where}?", run)

    @eventops_g.command(name="cancel", description="Cancel / delete a scheduled event")
    @app_commands.describe(event="Event name or ID")
    async def eventops_cancel(self, interaction: discord.Interaction, event: str):
        await self._slash(interaction, "eventops_cancel", event=event)

    async def plan_eventops_cancel(self, i, event="", **_):
        if err := _need(i, "manage_events"):
            return _fail(err)
        e, err = _find_by_name(i.guild.scheduled_events, event, "event")
        if err:
            return _fail(err)

        async def run():
            await e.delete(reason=reason_for(i.user))
            return f"🗑️ Cancelled the event **{safe_text(e.name, 60)}**."
        return Plan(None, f"⚠️ Cancel the event **{safe_text(e.name, 60)}** ({_ts(e.start_time)})?", run)

    # ═══════════════════════════════ SERVER SETTINGS ═════════════════════

    serveredit_g = app_commands.guild_only()(app_commands.Group(name="serveredit", description="Server settings, audit log, integrations"))

    @serveredit_g.command(name="edit", description="Change the server name, verification level or default notifications")
    @app_commands.describe(name="New server name",
                           verification_level="none, low, medium, high or highest",
                           notifications="all_messages or only_mentions")
    async def serveredit_edit(self, interaction: discord.Interaction, name: str = "", verification_level: str = "",
                              notifications: str = ""):
        await self._slash(interaction, "serveredit_edit", name=name, verification_level=verification_level,
                          notifications=notifications)

    async def plan_serveredit_edit(self, i, name="", verification_level="", notifications="", **_):
        if err := _need(i, "manage_guild"):
            return _fail(err)
        g = i.guild
        kw, notes = {}, []
        if str(name or "").strip():
            new = clip(name, 100)
            if len(new) < 2:
                return _fail("A server name needs at least 2 characters.")
            if new != g.name:
                kw["name"] = new
                notes.append(f"rename to **{safe_text(new, 60)}**")
        if str(verification_level or "").strip():
            key = verification_level.strip().lower()
            lvl = discord.VerificationLevel.__members__.get(key)
            if lvl is None:
                return _fail("Verification level must be one of: none, low, medium, high, highest.")
            if lvl != g.verification_level:
                kw["verification_level"] = lvl
                notes.append(f"verification level **{key}** (now {g.verification_level.name})")
        if str(notifications or "").strip():
            key = notifications.strip().lower().replace(" ", "_")
            lvl = discord.NotificationLevel.__members__.get(key)
            if lvl is None:
                return _fail("Notifications must be `all_messages` or `only_mentions`.")
            if lvl != g.default_notifications:
                kw["default_notifications"] = lvl
                notes.append(f"default notifications **{key}**")
        if not kw:
            return _fail("That wouldn't change anything — tell me a new `name`, `verification_level` or `notifications`.")

        async def run():
            await g.edit(**kw, reason=reason_for(i.user))
            return "✅ Updated the server: " + "; ".join(notes) + "."
        return Plan(None, "Change the server: " + "; ".join(notes) + ".", run)

    @serveredit_g.command(name="icon", description="Change or remove the server icon from a Discord CDN image link")
    @app_commands.describe(image_url="cdn.discordapp.com / media.discordapp.net image link (png/jpg/gif/webp), or 'remove'")
    async def serveredit_icon(self, interaction: discord.Interaction, image_url: str):
        await self._slash(interaction, "serveredit_icon", image_url=image_url)

    async def plan_serveredit_icon(self, i, image_url="", **_):
        if err := _need(i, "manage_guild"):
            return _fail(err)
        remove = str(image_url or "").strip().lower() in ("remove", "none", "clear")
        if not remove and not ops.cdn_image_url_ok(image_url):
            return _fail("I can only set the icon from a Discord image link (cdn.discordapp.com or media.discordapp.net, "
                         "png/jpg/gif/webp). Upload the image in a message, then copy its link.")
        if remove and i.guild.icon is None:
            return _fail("The server has no icon to remove.")

        async def run():
            data = None if remove else await _download_image(image_url.strip(), ops.MAX_IMAGE_BYTES)
            await i.guild.edit(icon=data, reason=reason_for(i.user))
            return "✅ Removed the server icon." if remove else "✅ Updated the server icon."
        return Plan(None, "Remove the server icon?" if remove else "Change the server icon to that image?", run)

    @serveredit_g.command(name="auditlog", description="Show recent audit log entries")
    @app_commands.describe(limit="How many entries (1-20, default 10)", action="Filter by action, e.g. ban, kick, role_update",
                           member="Only actions done by this member (@mention)")
    async def serveredit_auditlog(self, interaction: discord.Interaction, limit: Optional[int] = None, action: str = "",
                                  member: Optional[discord.Member] = None):
        await self._slash(interaction, "serveredit_auditlog", limit=limit, action=action, member=member)

    async def plan_serveredit_auditlog(self, i, limit=None, action="", member=None, **_):
        if err := _need(i, "view_audit_log"):
            return _fail(err)
        n = max(1, min(20, int(limit) if limit else 10))
        kw = {"limit": n}
        if str(action or "").strip():
            act = discord.AuditLogAction.__members__.get(action.strip().lower().replace(" ", "_").replace("-", "_"))
            if act is None:
                return _fail(f"I don't know the audit action `{safe_text(action, 30)}` — try ban, kick, role_update, "
                             "channel_delete, message_delete…")
            kw["action"] = act
        if member is not None:
            kw["user"] = member

        async def run():
            lines = []
            async for e in i.guild.audit_logs(**kw):
                tgt = getattr(e.target, "name", None) or getattr(e.target, "id", None) or "—"
                who = safe_text(getattr(e.user, "name", "unknown"), 24)
                why = f" — {safe_text(e.reason, 60)}" if e.reason else ""
                lines.append(f"{discord.utils.format_dt(e.created_at, 'R')} **{who}** `{e.action.name}` → {safe_text(tgt, 40)}{why}")
            return ("**Audit log**\n" + "\n".join(lines)) if lines else "No audit log entries matched."
        return Plan(None, f"Show the last {n} audit log entries (only you will see it)?", run)

    @serveredit_g.command(name="integrations", description="List the server's integrations")
    async def serveredit_integrations(self, interaction: discord.Interaction):
        await self._slash(interaction, "serveredit_integrations")

    async def plan_serveredit_integrations(self, i, **_):
        if err := _need(i, "manage_guild"):
            return _fail(err)

        async def run():
            ints = await i.guild.integrations()
            if not ints:
                return "This server has no integrations."
            return f"**{len(ints)} integration(s)**\n" + "\n".join(
                f"• **{safe_text(x.name, 40)}** — {x.type} · {'enabled' if x.enabled else 'disabled'}" for x in ints[:25])
        return Plan(None, "Show this server's integrations (only you will see it)?", run)

    @serveredit_g.command(name="integration-remove", description="Remove an integration")
    @app_commands.describe(name="Integration name")
    async def serveredit_integration_remove(self, interaction: discord.Interaction, name: str):
        await self._slash(interaction, "serveredit_integration_remove", name=name)

    async def plan_serveredit_integration_remove(self, i, name="", **_):
        if err := _need(i, "manage_guild"):
            return _fail(err)
        it, err = _find_by_name(await i.guild.integrations(), name, "integration")
        if err:
            return _fail(err)

        async def run():
            await it.delete(reason=reason_for(i.user))
            return f"🗑️ Removed the integration **{safe_text(it.name, 40)}**."
        return Plan(None, f"⚠️ Remove the integration **{safe_text(it.name, 40)}** ({it.type})?", run)


# ── wire the Confirm hooks (one prompt + one handler per command that needs a Confirm click) ──

def _wire(cls):
    keys = [n[len("plan_"):] for n in dir(cls) if n.startswith("plan_")]
    for key in keys:
        if key in PUBLIC_READS:
            continue

        async def prompt(self, interaction, _k=key, **kw):
            return await self._prompt(_k, interaction, **kw)

        async def handler(self, interaction, _k=key, **kw):
            return await self._confirmed(_k, interaction, **kw)

        setattr(cls, f"ai_{key}_prompt", prompt)
        setattr(cls, f"ai_{key}", handler)
        cls.AI_CONFIRM_PROMPTS[key] = f"ai_{key}_prompt"
        cls.AI_CONFIRMED_HANDLERS[key] = f"ai_{key}"
    return keys


PLAN_KEYS = _wire(AIServerOpsCog)


async def setup(bot: commands.Bot):
    """Deliberately NOT bot.add_cog: these commands stay off Discord's command tree entirely (see module docstring)."""
    from modules.ai_command_guard import register_hidden_cog
    n = register_hidden_cog(AIServerOpsCog(bot))
    logger.info(f"[ai_server_ops] registered {n} AI-only commands (not on the slash tree)")
