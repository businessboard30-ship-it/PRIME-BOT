"""
Owner panel, Batch 6 — Health dashboard, Server inspector and User inspector.

Same rules as the other panel files (see _views_admin_panel.py): buttons,
selects and forms only (no new slash commands), only the person who opened the
panel can use it, access is re-checked on every click and modal submit, and
every action writes an audit line.

Both inspectors are owner-only and not grantable to helpers. Reversible actions
apply at once (block / unblock, coins, DM). Anything destructive is two-step
(button -> Confirm): revoking premium, making the bot leave a server and
resetting someone's XP. The panel shows who and where before you confirm.

Clones run as separate processes, so "force-leave" only covers the main bot.
For a clone's server the way to cut it off is to block the server.
"""

from __future__ import annotations

import logging
from typing import List, Optional

import discord

from config import DISCORD_CLONE_ADMIN_IDS
from database import db
from discord_bot.cogs import _views_admin_panel as main
from discord_bot.cogs._views_admin_panel import PANEL_TIMEOUT, PanelView, _btn, allowed_sections, audit
from discord_bot.cogs._views_admin_panel_controls import _clip, _denied, _fit, _ts
from modules import admin_controls as ac
from modules import admin_inspect as ai

logger = logging.getLogger(__name__)

HEALTH = "health"
INSPECT = "inspect"

DM_MAX = 1500


def _premium_grace() -> int:
    try:
        from config import PREMIUM_GRACE_DAYS
        return int(PREMIUM_GRACE_DAYS)
    except Exception:
        return 0


async def _home(view: PanelView, i: discord.Interaction) -> None:
    await view.go(i, main.HomeView(view.cog, view.owner_id))


def _bot_label(clone_id: Optional[int], username: Optional[str] = None) -> str:
    if clone_id is None:
        return "Main bot"
    return f"Clone #{clone_id} ({username or 'unknown'})"


# ── health ───────────────────────────────────────────────────────────────

class HealthView(PanelView):
    title = "🩺 Health"

    def __init__(self, cog, owner_id, section=HEALTH):
        self.snap: dict = {}
        self.db_ms: Optional[float] = None
        self.errors = (0, 0)
        self.running = 0
        self.stopped: List[str] = []
        self.clones: Optional[List[dict]] = None
        self.loaded = False
        super().__init__(cog, owner_id, section)

    async def load(self) -> None:
        bot = getattr(self.cog, "bot", None)
        self.snap = ai.bot_snapshot(bot) if bot is not None else {}
        self.running, self.stopped = ai.loop_report(bot) if bot is not None else (0, [])
        self.errors = ai.error_counts()
        self.db_ms = await ai.db_ping()
        self.clones = await ai.clone_heartbeats()
        self.loaded = True
        self._build()

    def body(self):
        if not self.loaded:
            return ["Loading…"]
        s = self.snap
        bits = []
        bits.append(f"latency {s['latency_ms']} ms" if s.get("latency_ms") is not None else "latency unknown")
        bits.append(f"up {s.get('uptime', '?')}")
        if s.get("memory_mb") is not None:
            bits.append(f"memory {s['memory_mb']:.0f} MB")
        if s.get("servers") is not None:
            bits.append(f"{s['servers']} servers")
        if s.get("cogs") is not None:
            bits.append(f"{s['cogs']} cogs")
        lines = ["**Main bot:** " + " · ".join(bits)]

        if self.db_ms is None:
            lines.append("**Database:** ⚠️ not reachable")
        else:
            lines.append(f"**Database:** ✅ ok ({self.db_ms:.0f} ms)")

        errors, warnings = self.errors
        icon = "⚠️" if errors else "✅"
        lines.append(f"**Last hour:** {icon} {errors} error(s), {warnings} warning(s) "
                     "-# this process only")

        if self.stopped:
            shown = ", ".join(f"`{n}`" for n in self.stopped[:6])
            more = f" +{len(self.stopped) - 6} more" if len(self.stopped) > 6 else ""
            lines.append(f"**Background loops:** ⚠️ {len(self.stopped)} stopped ({shown}{more}), "
                         f"{self.running} running")
        else:
            lines.append(f"**Background loops:** ✅ {self.running} running, none stopped")

        if self.clones is None:
            lines.append("**Clones:** couldn't be checked")
        elif not self.clones:
            lines.append("**Clones:** none active")
        else:
            quiet = ai.quiet_clones(self.clones)
            if quiet:
                names = _fit([f"#{r['clone_id']} @{r.get('bot_username') or 'unknown'} "
                              f"(last seen {ai.age_text(r.get('last_heartbeat'))})" for r in quiet[:6]], 700)
                lines.append(f"**Clones:** ⚠️ {len(quiet)} of {len(self.clones)} quiet for over "
                             f"{ai.HEARTBEAT_STALE_MIN} min — " + "; ".join(names))
            else:
                lines.append(f"**Clones:** ✅ all {len(self.clones)} checked in recently")
        lines.append("-# A stopped loop or quiet clone usually means a crash; check the Log tail.")
        return lines

    def controls(self):
        S = discord.ButtonStyle.secondary
        a = allowed_sections(self.owner_id)
        return [
            _btn("Refresh", S, self._refresh, "🔄"),
            _btn("Log tail", S, self._logs, "📋", disabled="logs" not in a),
            _btn("Back", S, self._back, "⬅️"),
        ]

    async def _refresh(self, i: discord.Interaction):
        await i.response.defer()
        await self.load()
        audit(i, "health.refresh")
        await i.edit_original_response(view=self)

    async def _logs(self, i: discord.Interaction):
        from discord_bot.cogs._views_admin_panel_ops import LogsView
        audit(i, "health.logs")
        await self.go(i, LogsView(self.cog, self.owner_id))   # LogsView renders from the in-memory buffer, no load()

    async def _back(self, i): await _home(self, i)


# ── lookups ──────────────────────────────────────────────────────────────

async def _open_inspector(interaction: discord.Interaction, cog, kind: str, target_id: int) -> None:
    if kind == "server":
        view: PanelView = ServerInspectView(cog, interaction.user.id, target_id)
    else:
        view = UserInspectView(cog, interaction.user.id, target_id)
    await view.load()
    audit(interaction, f"inspect.{kind}", target=target_id)
    await interaction.response.edit_message(view=view)


class LookupModal(discord.ui.Modal):
    """One box: a pasted ID, or part of a name. IDs open the inspector straight
    away; names show a pick-list first."""

    def __init__(self, cog, kind: str):
        super().__init__(title="Inspect a server" if kind == "server" else "Inspect a person",
                         timeout=PANEL_TIMEOUT)
        self.cog = cog
        self.kind = kind
        self.query = discord.ui.TextInput(
            label="Server name or ID" if kind == "server" else "Username or user ID", max_length=100,
            placeholder="e.g. 123456789012345678")
        self.add_item(self.query)

    async def on_submit(self, interaction: discord.Interaction):
        if INSPECT not in allowed_sections(interaction.user.id):   # modals bypass interaction_check
            await _denied(interaction)
            return
        q = self.query.value.strip()
        if not q:
            await interaction.response.send_message("Type a name or paste an ID.", ephemeral=True)
            return
        if q.isdigit():
            target = ai.parse_snowflake(q)
            if target is None:
                await interaction.response.send_message(
                    "That doesn't look like a Discord ID. It should be a long number (enable Developer "
                    "Mode, right-click, **Copy ID**).", ephemeral=True)
                return
            await _open_inspector(interaction, self.cog, self.kind, target)
            return
        try:
            if self.kind == "server":
                found = await db.search_discord_guilds(q, limit=12)
            else:
                found = await db.search_cached_usernames(q, limit=12)
        except Exception:
            logger.exception("[admin-panel] inspector search failed")
            await interaction.response.send_message("Couldn't search right now (database problem).",
                                                    ephemeral=True)
            return
        if not found:
            await interaction.response.send_message(f"Nothing matching **{_clip(q, 60)}** found.", ephemeral=True)
            return
        if len(found) == 1:
            key = "guild_id" if self.kind == "server" else "user_id"
            await _open_inspector(interaction, self.cog, self.kind, int(found[0][key]))
            return
        view = PickView(self.cog, interaction.user.id, self.kind, q, found)
        await interaction.response.send_message(view=view, ephemeral=True)


class PickView(PanelView):
    title = "🔎 Pick one"

    def __init__(self, cog, owner_id, kind: str, query: str, found: List[dict]):
        self.kind = kind
        self.query = query
        self.found = found[:25]
        super().__init__(cog, owner_id, INSPECT)

    def body(self):
        return [f"{len(self.found)} match(es) for **{_clip(self.query, 60)}**. Pick one to inspect it."]

    def controls(self):
        opts = []
        for r in self.found:
            if self.kind == "server":
                label = f"{r.get('guild_name') or 'Unknown'} ({r.get('member_count') or '?'} members)"
                value = str(r["guild_id"])
            else:
                label = f"{r['username']} ({r['user_id']})"
                value = str(r["user_id"])
            opts.append(discord.SelectOption(label=label[:100], value=value))
        sel = discord.ui.Select(placeholder="Open a result…", options=opts)
        sel.callback = self._pick
        return [sel]

    async def _pick(self, i: discord.Interaction):
        target = int(i.data["values"][0])
        if self.kind == "server":
            view: PanelView = ServerInspectView(self.cog, self.owner_id, target)
        else:
            view = UserInspectView(self.cog, self.owner_id, target)
        await view.load()
        audit(i, f"inspect.{self.kind}", target=target)
        await self.go(i, view)


# ── server inspector ─────────────────────────────────────────────────────

class BlockReasonModal(discord.ui.Modal, title="Block this server"):
    def __init__(self, view: "ServerInspectView"):
        super().__init__(timeout=PANEL_TIMEOUT)
        self.view = view
        self.reason = discord.ui.TextInput(label="Reason (optional)", required=False, max_length=200,
                                           style=discord.TextStyle.paragraph)
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction):
        if INSPECT not in allowed_sections(interaction.user.id):
            await _denied(interaction)
            return
        v = self.view
        reason = self.reason.value.strip()
        try:
            await ac.add_blacklist("guild", v.guild_id, reason, interaction.user.id)
        except Exception:
            logger.exception("[admin-panel] couldn't block server from the inspector")
            await interaction.response.send_message("Couldn't save that (database problem). Nothing was changed.",
                                                    ephemeral=True)
            return
        audit(interaction, "blacklist.add", kind="guild", target=v.guild_id, reason=reason)
        v.notice = f"✅ Blocked server `{v.guild_id}`."
        v.confirm = None
        await v.load()
        await interaction.response.edit_message(view=v)


class MessageOwnerModal(discord.ui.Modal, title="Message the server owner"):
    def __init__(self, view: "ServerInspectView"):
        super().__init__(timeout=PANEL_TIMEOUT)
        self.view = view
        self.text = discord.ui.TextInput(label="Message", max_length=DM_MAX, style=discord.TextStyle.paragraph,
                                         placeholder="Sent as a DM from the main bot.")
        self.add_item(self.text)

    async def on_submit(self, interaction: discord.Interaction):
        if INSPECT not in allowed_sections(interaction.user.id):
            await _denied(interaction)
            return
        v = self.view
        row = v.current_row()
        owner_id = row.get("owner_id") if row else None
        text = self.text.value.strip()
        if not owner_id or not text:
            await interaction.response.send_message("There's no owner on record, or the message is empty.",
                                                    ephemeral=True)
            return
        bot = getattr(v.cog, "bot", None)
        try:
            user = bot.get_user(owner_id) or await bot.fetch_user(owner_id)
            await user.send(f"📩 **Message from the PRIME-BOT team**\n{text}")
            v.notice = f"✅ Sent to <@{owner_id}>."
            sent = True
        except (discord.Forbidden, discord.HTTPException):
            v.notice = f"⚠️ Couldn't DM <@{owner_id}> (their DMs are closed or the bot can't reach them)."
            sent = False
        except Exception:
            logger.exception("[admin-panel] owner DM failed")
            v.notice = "⚠️ Couldn't send that message."
            sent = False
        audit(interaction, "server.dm_owner", guild=v.guild_id, owner=owner_id, sent=sent, length=len(text))
        v._build()
        await interaction.response.edit_message(view=v)


class ServerInspectView(PanelView):
    title = "🔍 Server inspector"

    def __init__(self, cog, owner_id, guild_id: int, section=INSPECT):
        self.guild_id = guild_id
        self.rows: List[dict] = []
        self.extras: dict = {}
        self.error = False
        self.notice: Optional[str] = None
        self.confirm: Optional[str] = None      # "leave" | "revoke"
        self.pick: Optional[str] = None         # "main" / "<clone id>" when several bots share the server
        super().__init__(cog, owner_id, section)

    @staticmethod
    def _key(r: dict) -> str:
        return "main" if r.get("clone_id") is None else str(r["clone_id"])

    def current_row(self) -> Optional[dict]:
        if not self.rows:
            return None
        return next((r for r in self.rows if self._key(r) == self.pick), self.rows[0])

    def _premium_for(self, row: Optional[dict]) -> Optional[dict]:
        if row is None:
            return None
        return next((p for p in self.extras.get("premium", [])
                     if p.get("clone_id") == row.get("clone_id")), None)

    def _premium_is_active(self, row: Optional[dict]) -> bool:
        p = self._premium_for(row)
        return bool(p) and ai.premium_active(p.get("expires_at"), _premium_grace())

    def _main_guild(self):
        bot = getattr(self.cog, "bot", None)
        try:
            return bot.get_guild(self.guild_id) if bot is not None else None
        except Exception:
            return None

    async def load(self) -> None:
        try:
            self.rows = await ai.server_rows(self.guild_id)
            self.extras = await ai.server_extras(self.guild_id)
            self.error = False
        except Exception:
            logger.exception("[admin-panel] couldn't load server %s", self.guild_id)
            self.rows, self.extras, self.error = [], {}, True
        if self.pick not in {self._key(r) for r in self.rows}:
            self.pick = None
        self._build()

    def body(self):
        lines: List[str] = []
        if self.notice:
            lines.append(self.notice)
        if self.error:
            return lines + ["⚠️ Couldn't load that server right now."]
        r = self.current_row()
        if r is None:
            lines.append(f"No server `{self.guild_id}` in the database — the bots have never joined it.")
        else:
            state = "left" if r.get("left_at") else "joined"
            lines.append(f"**{_clip(r.get('guild_name'), 80) or 'Unknown'}** · `{self.guild_id}` · "
                         f"{_bot_label(r.get('clone_id'), r.get('bot_username'))}")
            owner = f"<@{r['owner_id']}>" if r.get("owner_id") else "unknown"
            lines.append(f"**Members:** {r.get('member_count') or '?'} · **Owner:** {owner}")
            lines.append(f"**Joined:** {_ts(r.get('joined_at'))}"
                         + (f" · **Left:** {_ts(r['left_at'])}" if state == "left" else ""))
            if len(self.rows) > 1:
                lines.append(f"-# {len(self.rows)} bots have been in this server — pick one below.")
            p = self._premium_for(r)
            if p is None:
                lines.append("**Premium:** none")
            elif self._premium_is_active(r):
                lines.append(f"**Premium:** 💎 until {_ts(p.get('expires_at'), 'f')}")
            else:
                lines.append(f"**Premium:** lapsed {_ts(p.get('expires_at'))}")
        blocked = self.extras.get("blocked")
        if blocked:
            why = f" — {_clip(blocked.get('reason'), 80)}" if blocked.get("reason") else ""
            lines.append(f"**Blocked:** 🚫 since {_ts(blocked.get('created_at'))}{why}")
        elif r is not None:
            lines.append("**Blocked:** no")
        reports = self.extras.get("reports") or {}
        if reports:
            lines.append("**Reports:** 🚩 " + ", ".join(f"{n} {status}" for status, n in sorted(reports.items())))
        elif r is not None:
            lines.append("**Reports:** none")
        if self.confirm == "revoke" and r is not None:
            lines.append("⚠️ **Press Confirm to end this server's premium right now.**")
        if self.confirm == "leave":
            lines.append("⚠️ **Press Confirm to make the main bot leave this server.**")
        return lines

    def controls(self):
        S, D = discord.ButtonStyle.secondary, discord.ButtonStyle.danger
        items: list = []
        if len(self.rows) > 1:
            cur = self.current_row()
            sel = discord.ui.Select(
                placeholder="Which bot?",
                options=[discord.SelectOption(
                    label=_bot_label(r.get("clone_id"), r.get("bot_username"))[:100] + (" (left)" if r.get("left_at") else ""),
                    value=self._key(r), default=r is cur) for r in self.rows[:25]])
            sel.callback = self._pick
            items.append(sel)
        row = self.current_row()
        blocked = bool(self.extras.get("blocked"))
        items += [
            _btn("Look up another", S, self._lookup, "🔎"),
            _btn("Unblock server" if blocked else "Block server", S if blocked else D, self._block, "🚫"),
            _btn("Confirm revoke" if self.confirm == "revoke" else "Revoke premium", D, self._revoke, "💎",
                 disabled=not self._premium_is_active(row)),
            _btn("Confirm leave" if self.confirm == "leave" else "Force-leave", D, self._leave, "🚪",
                 disabled=self._main_guild() is None),
            _btn("Message owner", S, self._message, "✉️", disabled=not (row and row.get("owner_id"))),
            _btn("Back", S, self._back, "⬅️"),
        ]
        return items

    async def _pick(self, i: discord.Interaction):
        self.pick = i.data["values"][0]
        self.confirm = None
        self.notice = None
        self._build()
        await i.response.edit_message(view=self)

    async def _lookup(self, i: discord.Interaction):
        await i.response.send_modal(LookupModal(self.cog, "server"))

    async def _block(self, i: discord.Interaction):
        if self.extras.get("blocked"):
            try:
                removed = await ac.remove_blacklist("guild", self.guild_id)
            except Exception:
                logger.exception("[admin-panel] couldn't unblock server")
                await i.response.send_message("Couldn't remove that (database problem). Nothing was changed.",
                                              ephemeral=True)
                return
            audit(i, "blacklist.remove", kind="guild", target=self.guild_id)
            self.notice = f"✅ Unblocked server `{self.guild_id}`." if removed else "That entry was already gone."
            self.confirm = None
            await self.load()
            await i.response.edit_message(view=self)
            return
        await i.response.send_modal(BlockReasonModal(self))

    async def _revoke(self, i: discord.Interaction):
        row = self.current_row()
        if row is None or not self._premium_is_active(row):
            await i.response.send_message("This server has no active premium on that bot.", ephemeral=True)
            return
        if self.confirm != "revoke":
            self.confirm = "revoke"
            self.notice = None
            self._build()
            await i.response.edit_message(view=self)
            return
        try:
            done = await ac.revoke_premium(self.guild_id, row.get("clone_id"))
        except Exception:
            logger.exception("[admin-panel] couldn't revoke premium")
            await i.response.send_message("Couldn't revoke that (database problem). Nothing was changed.",
                                          ephemeral=True)
            return
        audit(i, "premium.revoke", guild=self.guild_id, clone=row.get("clone_id"))
        self.notice = "✅ Premium ended." if done else "There was no subscription row to end."
        self.confirm = None
        await self.load()
        await i.response.edit_message(view=self)

    async def _leave(self, i: discord.Interaction):
        guild = self._main_guild()
        if guild is None:
            await i.response.send_message(
                "The main bot isn't in that server. Clones run as separate processes, so to cut a "
                "clone's server off, block the server instead.", ephemeral=True)
            return
        if self.confirm != "leave":
            self.confirm = "leave"
            self.notice = None
            self._build()
            await i.response.edit_message(view=self)
            return
        try:
            await guild.leave()
        except Exception:
            logger.exception("[admin-panel] couldn't leave server %s", self.guild_id)
            await i.response.send_message("Couldn't leave that server (Discord refused or timed out).",
                                          ephemeral=True)
            return
        try:
            await db.mark_discord_guild_left(self.guild_id, None)
        except Exception:
            logger.debug("[admin-panel] leave bookkeeping skipped", exc_info=True)   # the guild-remove event also records it
        audit(i, "server.leave", guild=self.guild_id)
        self.notice = "✅ The main bot left the server."
        self.confirm = None
        await self.load()
        await i.response.edit_message(view=self)

    async def _message(self, i: discord.Interaction):
        await i.response.send_modal(MessageOwnerModal(self))

    async def _back(self, i): await _home(self, i)


# ── user inspector ───────────────────────────────────────────────────────

class CoinsModal(discord.ui.Modal, title="Give or take coins"):
    def __init__(self, view: "UserInspectView"):
        super().__init__(timeout=PANEL_TIMEOUT)
        self.view = view
        self.guild = discord.ui.TextInput(label="Server (guild) ID", max_length=25,
                                          placeholder="Coins are kept per server")
        self.amount = discord.ui.TextInput(label="Amount (use - to take away)", max_length=14,
                                           placeholder="e.g. 500 or -200")
        self.clone = discord.ui.TextInput(label="Clone ID (leave empty for the main bot)", required=False,
                                          max_length=10)
        for it in (self.guild, self.amount, self.clone):
            self.add_item(it)

    async def on_submit(self, interaction: discord.Interaction):
        if INSPECT not in allowed_sections(interaction.user.id):
            await _denied(interaction)
            return
        guild_id = ai.parse_snowflake(self.guild.value)
        amount = ai.parse_coins(self.amount.value)
        ok, clone_id = ai.parse_clone(self.clone.value)
        if guild_id is None:
            await interaction.response.send_message("That doesn't look like a server ID.", ephemeral=True)
            return
        if amount is None:
            await interaction.response.send_message(
                f"Amount must be a whole number other than 0, up to {ai.MAX_COINS:,} either way.", ephemeral=True)
            return
        if not ok:
            await interaction.response.send_message("Clone ID must be a number, or empty for the main bot.",
                                                    ephemeral=True)
            return
        v = self.view
        try:
            balance = await db.adjust_economy_balance(
                guild_id, v.user_id, amount, f"admin_panel:{interaction.user.id}", clone_id)
        except Exception:
            logger.exception("[admin-panel] couldn't adjust coins")
            await interaction.response.send_message("Couldn't change the balance (database problem). "
                                                    "Nothing was changed.", ephemeral=True)
            return
        audit(interaction, "user.coins", user=v.user_id, guild=guild_id, clone=clone_id, amount=amount)
        word = "Gave" if amount > 0 else "Took"
        v.notice = f"✅ {word} {abs(amount):,} coins in `{guild_id}` — new balance there: {balance:,}."
        await v.load()
        await interaction.response.edit_message(view=v)


class ResetXpModal(discord.ui.Modal, title="Reset XP in one server"):
    def __init__(self, view: "UserInspectView"):
        super().__init__(timeout=PANEL_TIMEOUT)
        self.view = view
        self.guild = discord.ui.TextInput(label="Server (guild) ID", max_length=25)
        self.clone = discord.ui.TextInput(label="Clone ID (leave empty for the main bot)", required=False,
                                          max_length=10)
        self.add_item(self.guild)
        self.add_item(self.clone)

    async def on_submit(self, interaction: discord.Interaction):
        if INSPECT not in allowed_sections(interaction.user.id):
            await _denied(interaction)
            return
        guild_id = ai.parse_snowflake(self.guild.value)
        ok, clone_id = ai.parse_clone(self.clone.value)
        if guild_id is None:
            await interaction.response.send_message("That doesn't look like a server ID.", ephemeral=True)
            return
        if not ok:
            await interaction.response.send_message("Clone ID must be a number, or empty for the main bot.",
                                                    ephemeral=True)
            return
        v = self.view
        v.pending_reset = (guild_id, clone_id)
        v.notice = None
        v._build()
        await interaction.response.edit_message(view=v)


class UserInspectView(PanelView):
    title = "🧑 User inspector"

    def __init__(self, cog, owner_id, user_id: int, section=INSPECT):
        self.user_id = user_id
        self.card: dict = {}
        self.error = False
        self.notice: Optional[str] = None
        self.pending_reset: Optional[tuple] = None    # (guild_id, clone_id) waiting for Confirm
        super().__init__(cog, owner_id, section)

    async def load(self) -> None:
        try:
            self.card = await ai.user_card(self.user_id)
            self.error = False
        except Exception:
            logger.exception("[admin-panel] couldn't load user %s", self.user_id)
            self.card, self.error = {}, True
        self._build()

    def body(self):
        lines: List[str] = []
        if self.notice:
            lines.append(self.notice)
        if self.error:
            return lines + ["⚠️ Couldn't load that person right now."]
        c = self.card
        if not c:
            return lines + ["Loading…"]
        name = _clip(c.get("username"), 60) or "name not cached"
        lines.append(f"**{name}** · <@{self.user_id}> · `{self.user_id}`")
        if self.user_id in DISCORD_CLONE_ADMIN_IDS:
            lines.append("👑 Bot owner — can't be blocked.")
        blocked = c.get("blocked")
        if blocked:
            why = f" — {_clip(blocked.get('reason'), 80)}" if blocked.get("reason") else ""
            lines.append(f"**Blocked:** 🚫 since {_ts(blocked.get('created_at'))}{why}")
        else:
            lines.append("**Blocked:** no")

        entries: List[str] = []
        owned = c.get("owned") or []
        if owned:
            entries.append("**Owns servers:** " + "; ".join(
                f"{_clip(r.get('guild_name'), 30) or 'Unknown'} (`{r['guild_id']}`)" for r in owned[:5])
                + (f" +{len(owned) - 5} more" if len(owned) > 5 else ""))
        clones = c.get("clones") or []
        if clones:
            entries.append("**Clones:** " + ", ".join(
                f"#{r['clone_id']} @{r.get('bot_username') or 'unknown'} ({r['status']})" for r in clones[:5]))
        xp = c.get("xp") or []
        if xp:
            top = "; ".join(f"{_clip(r.get('guild_name'), 24) or r['guild_id']}: level {r['level']} "
                            f"({r['total_xp']:,} XP)" for r in xp[:3])
            entries.append(f"**XP:** in {c.get('xp_servers', 0)} server(s) — top: {top}")
        else:
            entries.append("**XP:** none recorded")
        entries.append(f"**Coins:** {c.get('coins_total', 0):,} across {c.get('coins_servers', 0)} server(s)")
        pays = c.get("payments") or []
        if pays:
            entries.append("**Payments:** " + ", ".join(
                f"{p['status']} {p['n']} (${float(p['total']):.2f})" for p in pays))
        else:
            entries.append("**Payments:** none")
        lines += _fit(entries, 2600)

        if self.pending_reset:
            g, cl = self.pending_reset
            where = f"clone #{cl}" if cl is not None else "the main bot"
            lines.append(f"⚠️ **Press Confirm to reset this person's XP and level in `{g}` ({where}).**")
        return lines

    def controls(self):
        S, D = discord.ButtonStyle.secondary, discord.ButtonStyle.danger
        blocked = bool((self.card or {}).get("blocked"))
        is_owner = self.user_id in DISCORD_CLONE_ADMIN_IDS
        items = [
            _btn("Look up another", S, self._lookup, "🔎"),
            _btn("Unblock" if blocked else "Block", S if blocked else D, self._block, "🚫",
                 disabled=is_owner and not blocked),
            _btn("Give / take coins", S, self._coins, "🪙"),
            _btn("Confirm XP reset" if self.pending_reset else "Reset XP", D, self._reset, "♻️"),
        ]
        if self.pending_reset:
            items.append(_btn("Cancel", S, self._cancel, "✖️"))
        items.append(_btn("Back", S, self._back, "⬅️"))
        return items

    async def _lookup(self, i: discord.Interaction):
        await i.response.send_modal(LookupModal(self.cog, "user"))

    async def _block(self, i: discord.Interaction):
        blocked = bool((self.card or {}).get("blocked"))
        if blocked:
            try:
                removed = await ac.remove_blacklist("user", self.user_id)
            except Exception:
                logger.exception("[admin-panel] couldn't unblock user")
                await i.response.send_message("Couldn't remove that (database problem). Nothing was changed.",
                                              ephemeral=True)
                return
            audit(i, "blacklist.remove", kind="user", target=self.user_id)
            self.notice = f"✅ Unblocked <@{self.user_id}>." if removed else "That entry was already gone."
        else:
            if self.user_id in DISCORD_CLONE_ADMIN_IDS:
                await i.response.send_message("You can't block a bot owner.", ephemeral=True)
                return
            try:
                await ac.add_blacklist("user", self.user_id, "Blocked from the user inspector", i.user.id)
            except Exception:
                logger.exception("[admin-panel] couldn't block user")
                await i.response.send_message("Couldn't save that (database problem). Nothing was changed.",
                                              ephemeral=True)
                return
            audit(i, "blacklist.add", kind="user", target=self.user_id, reason="user inspector")
            self.notice = f"✅ Blocked <@{self.user_id}>."
        self.pending_reset = None
        await self.load()
        await i.response.edit_message(view=self)

    async def _coins(self, i: discord.Interaction):
        await i.response.send_modal(CoinsModal(self))

    async def _reset(self, i: discord.Interaction):
        if not self.pending_reset:
            await i.response.send_modal(ResetXpModal(self))
            return
        guild_id, clone_id = self.pending_reset
        try:
            done = await ai.reset_xp(self.user_id, guild_id, clone_id)
        except Exception:
            logger.exception("[admin-panel] couldn't reset xp")
            await i.response.send_message("Couldn't reset that (database problem). Nothing was changed.",
                                          ephemeral=True)
            return
        audit(i, "user.reset_xp", user=self.user_id, guild=guild_id, clone=clone_id)
        self.notice = (f"✅ XP and level reset in `{guild_id}`." if done
                       else f"No XP record for this person in `{guild_id}` on that bot.")
        self.pending_reset = None
        await self.load()
        await i.response.edit_message(view=self)

    async def _cancel(self, i: discord.Interaction):
        self.pending_reset = None
        self.notice = None
        self._build()
        await i.response.edit_message(view=self)

    async def _back(self, i): await _home(self, i)
