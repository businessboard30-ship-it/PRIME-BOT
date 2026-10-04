"""
Owner control panel — buttons, select menus and modals only, no typed slash
arguments. Opened with `/admin panel`.

Design rules (see the plan in the PR description):

* The panel is a thin UI shell. Every action calls the SAME cog coroutine the
  matching `/admin ...` command calls (`cog.approvepayment(interaction, ...)`
  etc.), so auth checks, DB writes and replies are identical and there is no
  second copy of the logic to drift. The old slash commands stay mounted as a
  fallback until the panel covers everything.
* Only the person who opened a panel can use it, and access is re-checked on
  EVERY click (an allowlist edit takes effect immediately).
* Money / fan-out actions are two-step (button -> explicit Confirm).
* Every action writes one `[admin-panel-audit]` log line.
* Discord limits respected: <=5 buttons per row, <=25 select options, <=5
  modal fields, TextInput <=4000 chars.
"""

from __future__ import annotations

import logging
from typing import Callable, Dict, List, Optional

import discord
from discord import app_commands

from config import DISCORD_CLONE_ADMIN_IDS, DISCORD_OWNER_BROADCAST_IDS
from database import db

logger = logging.getLogger(__name__)

PANEL_TIMEOUT = 600  # seconds of inactivity before buttons stop working

PAYMENT_MODES = [
    ("split", "Split — Ghana via Paystack, others via Gumroad"),
    ("auto", "Paystack only"),
    ("gumroad", "Gumroad only"),
    ("inherit", "Follow main bot (clones only)"),
]


# ── access ───────────────────────────────────────────────────────────────

def allowed_sections(user_id: int) -> set:
    """Which panel sections this user may open. Mirrors the two allowlists
    the slash commands already use (DISCORD_CLONE_ADMIN_IDS for payments and
    servers/clones, DISCORD_OWNER_BROADCAST_IDS for broadcasts)."""
    out = set()
    if user_id in DISCORD_CLONE_ADMIN_IDS:
        out.add("payments")
    if user_id in DISCORD_OWNER_BROADCAST_IDS:
        out.add("broadcast")
    if user_id in DISCORD_CLONE_ADMIN_IDS:
        out.add("servers")  # servers / find / clones / monetize / commissions / subscribers
        out.add("bump")     # /admin bump ... (bump.py gates it on DISCORD_CLONE_ADMIN_IDS)
        out.add("catch")    # Catch server setup is available to the same owner allowlist
        out.add("system")   # submissions / envcheck / revenue / exportusers (admin.py, same list)
        out.update({"controls", "blacklist", "premium", "audit"})  # Batch 1 (_views_admin_panel_controls.py)
        out.update({"access", "logs", "config", "database"})       # Batch 2 (_views_admin_panel_ops.py)
        out.update({"watchlist", "reports", "status", "honeypot"})  # Batch 3 (_views_admin_panel_safety.py), owner-only
        out.add("money")                                            # Batch 4 (_views_admin_panel_money.py), owner-only
        out.add("scamshield")                                       # Scam Shield (_views_admin_panel_scamshield.py), owner-only, not grantable
        out.add("referral")                                         # Referral giveaway (_views_admin_panel_referral.py), owner-only, not grantable
        out.add("ads")                                              # Batch 5 (_views_admin_panel_ads.py), owner-only (same gate as /ad manage)
        out.update({"health", "inspect"})                           # Batch 6 (_views_admin_panel_inspect.py), owner-only, not grantable
    if user_id in DISCORD_OWNER_BROADCAST_IDS:
        out.add("feedback")  # /admin feedback (feedback.py gates it on DISCORD_OWNER_BROADCAST_IDS)
    # Helpers: extra people the owner let into SOME sections (only the grantable
    # ones, see modules/admin_controls.GRANTABLE). Reads an in-memory map.
    try:
        from modules import admin_controls
        out |= admin_controls.helper_sections(user_id)
    except Exception:
        logger.debug("[admin-panel] helper sections unavailable", exc_info=True)
    return out


def can_open_panel(user_id: int) -> bool:
    return bool(allowed_sections(user_id))


async def refresh_access(force: bool = False) -> None:
    """Reload helper accounts from the DB (cheap: TTL-gated, never raises)."""
    try:
        from modules import admin_controls
        await admin_controls.refresh_helpers(force=force)
    except Exception:
        logger.debug("[admin-panel] helper refresh skipped", exc_info=True)


_audit_tasks: set = set()   # keeps fire-and-forget DB writes from being garbage-collected mid-flight


def audit(interaction: discord.Interaction, action: str, **details) -> None:
    detail_text = " ".join(f"{k}={v!r}" for k, v in details.items())
    logger.info("[admin-panel-audit] user=%s guild=%s action=%s %s",
                interaction.user.id, interaction.guild_id, action, detail_text)
    # Also keep a copy in the database for the panel's Audit log screen. Best
    # effort: the log line above is the source of truth if this can't be saved.
    try:
        import asyncio
        from modules import admin_controls
        task = asyncio.get_running_loop().create_task(
            admin_controls.record_audit(interaction.user.id, action, interaction.guild_id, detail_text))
        _audit_tasks.add(task)
        task.add_done_callback(_audit_tasks.discard)
    except Exception:
        logger.debug("[admin-panel] audit row not scheduled", exc_info=True)


async def call_cmd(owner_cog, name: str, interaction: discord.Interaction, **kwargs):
    """Run an existing command of `owner_cog` exactly as the slash command
    would. Methods mounted via mount_admin_command are plain coroutines;
    `@admin.command(...)` ones are app_commands.Command objects whose
    `.callback` needs the cog as first argument."""
    attr = getattr(owner_cog, name)
    if isinstance(attr, app_commands.Command):
        return await attr.callback(getattr(attr, "binding", None) or owner_cog, interaction, **kwargs)
    return await attr(interaction, **kwargs)


# ── base view ────────────────────────────────────────────────────────────

# A select must sit alone in its row. ChannelSelect/RoleSelect/UserSelect/MentionableSelect are NOT
# subclasses of discord.ui.Select, so list them all or they get packed in with buttons (Discord 400).
_SELECTS = (discord.ui.Select, discord.ui.ChannelSelect, discord.ui.RoleSelect,
            discord.ui.UserSelect, discord.ui.MentionableSelect)


class PanelView(discord.ui.LayoutView):
    """One screen of the panel. Subclasses fill `body()` and `controls()`."""

    title = "Owner panel"
    accent = discord.Color.blurple()

    def __init__(self, cog, owner_id: int, section: Optional[str] = None):
        super().__init__(timeout=PANEL_TIMEOUT)
        self.cog = cog                # AdminPanelCog
        self.owner_id = owner_id
        self.section = section        # None = home; else needs allowed_sections()
        self._build()

    # subclasses override
    def body(self) -> List[str]:
        return []

    def controls(self) -> List[discord.ui.Item]:
        """Items (buttons/selects). Selects take a full row; buttons are
        packed 5 per row."""
        return []

    def _build(self) -> None:
        self.clear_items()
        children: list = [discord.ui.TextDisplay("\n".join([f"### {self.title}", *self.body()]))]
        items = self.controls()
        if items:
            children.append(discord.ui.Separator())
            row: list = []
            for it in items:
                if isinstance(it, _SELECTS):
                    if row:
                        children.append(discord.ui.ActionRow(*row)); row = []
                    children.append(discord.ui.ActionRow(it))
                else:
                    row.append(it)
                    if len(row) == 5:
                        children.append(discord.ui.ActionRow(*row)); row = []
            if row:
                children.append(discord.ui.ActionRow(*row))
        self.add_item(discord.ui.Container(*children, accent_colour=self.accent))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("This panel belongs to someone else.", ephemeral=True)
            return False
        await refresh_access()
        allowed = allowed_sections(interaction.user.id)
        if not allowed or (self.section and self.section not in allowed):
            await interaction.response.send_message("You're no longer authorized for this.", ephemeral=True)
            return False
        return True

    async def on_timeout(self) -> None:
        for child in self.walk_children():
            if hasattr(child, "disabled"):
                child.disabled = True

    async def soft_refresh(self, interaction: discord.Interaction) -> None:
        """Re-render this panel AFTER a cog method has already used the
        interaction's response (so edit_message is no longer allowed). Best
        effort: a failure here must never hide the action's real result."""
        try:
            self._build()
            if interaction.message is not None:
                await interaction.followup.edit_message(interaction.message.id, view=self)
        except Exception:
            logger.debug("[admin-panel] soft refresh skipped", exc_info=True)

    # navigation helper
    async def go(self, interaction: discord.Interaction, view: "PanelView") -> None:
        await interaction.response.edit_message(view=view)


def _btn(label: str, style: discord.ButtonStyle, cb: Callable, emoji: str = None,
         disabled: bool = False) -> discord.ui.Button:
    b = discord.ui.Button(label=label, style=style, emoji=emoji, disabled=disabled)
    b.callback = cb
    return b


# ── home ─────────────────────────────────────────────────────────────────

class HomeView(PanelView):
    title = "🛠️ Owner panel"

    def body(self) -> List[str]:
        a = allowed_sections(self.owner_id)
        lines = ["Pick an area. Everything here uses buttons and forms — no typed commands."]
        if "payments" not in a:
            lines.append("-# Payments: not available to your account.")
        if "broadcast" not in a:
            lines.append("-# Broadcast: not available to your account.")
        if "servers" not in a:
            lines.append("-# Servers & clones: not available to your account.")
        if "bump" not in a:
            lines.append("-# Bump: not available to your account.")
        if "catch" not in a:
            lines.append("-# Catch setup: not available to your account.")
        if "feedback" not in a:
            lines.append("-# Feedback: not available to your account.")
        if "system" not in a:
            lines.append("-# System: not available to your account.")
        return lines

    def controls(self):
        a = allowed_sections(self.owner_id)
        P, S = discord.ButtonStyle.primary, discord.ButtonStyle.secondary
        return [
            _btn("Payments", P, self._payments, "💳", disabled="payments" not in a),
            _btn("Broadcast", P, self._broadcast, "📢", disabled="broadcast" not in a),
            _btn("Servers & clones", P, self._servers, "🏠", disabled="servers" not in a),
            _btn("Bump", P, self._bump, "📡", disabled="bump" not in a),
            _btn("Catch setup", P, self._catch, "C", disabled="catch" not in a),
            _btn("Feedback", P, self._feedback, "📬", disabled="feedback" not in a),
            _btn("System", P, self._system, "⚙️", disabled="system" not in a),
            _btn("Kill switches", P, self._controls, "🎚️", disabled="controls" not in a),
            _btn("Blacklist", P, self._blacklist, "🚫", disabled="blacklist" not in a),
            _btn("Premium", P, self._premium, "💎", disabled="premium" not in a),
            _btn("Audit log", P, self._audit_log, "📜", disabled="audit" not in a),
            _btn("Panel access", P, self._access, "🔑", disabled="access" not in a),
            _btn("Log tail", P, self._logs, "📋", disabled="logs" not in a),
            _btn("Config", P, self._config, "🧾", disabled="config" not in a),
            _btn("Database", P, self._database, "🗄️", disabled="database" not in a),
            _btn("Watchlist", P, self._watchlist, "🕵️", disabled="watchlist" not in a),
            _btn("Reports", P, self._reports, "🚩", disabled="reports" not in a),
            _btn("Status", P, self._status, "🎭", disabled="status" not in a),
            _btn("Honeypot", P, self._honeypot, "🍯", disabled="honeypot" not in a),
            _btn("Money", P, self._money, "💰", disabled="money" not in a),
            _btn("Ads", P, self._ads, "📣", disabled="ads" not in a),
            _btn("Referral giveaway", P, self._referral, "🎁", disabled="referral" not in a),
            _btn("Health", P, self._health, "🩺", disabled="health" not in a),
            _btn("Inspect server", P, self._inspect_server, "🔍", disabled="inspect" not in a),
            _btn("Inspect user", P, self._inspect_user, "🧑", disabled="inspect" not in a),
            _btn("Close", S, self._close, "✖️"),
        ]

    async def _payments(self, i: discord.Interaction):
        await self.go(i, PaymentsView(self.cog, self.owner_id, "payments"))

    async def _broadcast(self, i: discord.Interaction):
        view = BroadcastView(self.cog, self.owner_id, "broadcast", in_dm=i.guild_id is None)
        await view._ensure_clones()
        view._build()
        await self.go(i, view)

    async def _servers(self, i: discord.Interaction):
        from discord_bot.cogs._views_admin_panel_servers import ServersHubView  # lazy: avoids import cycle
        await self.go(i, ServersHubView(self.cog, self.owner_id, "servers"))

    async def _bump(self, i: discord.Interaction):
        from discord_bot.cogs._views_admin_panel_system import BumpHubView  # lazy: avoids import cycle
        await self.go(i, BumpHubView(self.cog, self.owner_id, "bump"))

    async def _catch(self, i: discord.Interaction):
        from discord_bot.cogs.catch import CatchSetupView, build_setup_embed
        from modules.catch_setup import load_setup

        await i.response.defer()
        guild_id = i.guild_id
        setup = await load_setup(guild_id) if guild_id is not None else None
        view = CatchSetupView(setup, guild_id=guild_id)
        await i.edit_original_response(embed=build_setup_embed(view.setup), view=view)

    async def _feedback(self, i: discord.Interaction):
        from discord_bot.cogs._views_admin_panel_system import FeedbackView
        await self.go(i, FeedbackView(self.cog, self.owner_id, "feedback"))

    async def _system(self, i: discord.Interaction):
        from discord_bot.cogs._views_admin_panel_system import SystemView
        await self.go(i, SystemView(self.cog, self.owner_id, "system"))

    async def _controls(self, i: discord.Interaction):
        from discord_bot.cogs._views_admin_panel_controls import ControlsView  # lazy: avoids import cycle
        view = ControlsView(self.cog, self.owner_id)
        await view.load()
        await self.go(i, view)

    async def _blacklist(self, i: discord.Interaction):
        from discord_bot.cogs._views_admin_panel_controls import BlacklistView
        view = BlacklistView(self.cog, self.owner_id)
        await view.load()
        await self.go(i, view)

    async def _premium(self, i: discord.Interaction):
        from discord_bot.cogs._views_admin_panel_controls import PremiumView
        view = PremiumView(self.cog, self.owner_id)
        await view.load()
        await self.go(i, view)

    async def _audit_log(self, i: discord.Interaction):
        from discord_bot.cogs._views_admin_panel_controls import AuditView
        view = AuditView(self.cog, self.owner_id)
        await view.load()
        await self.go(i, view)

    async def _access(self, i: discord.Interaction):
        from discord_bot.cogs._views_admin_panel_ops import AccessView
        view = AccessView(self.cog, self.owner_id)
        await view.load()
        await self.go(i, view)

    async def _logs(self, i: discord.Interaction):
        from discord_bot.cogs._views_admin_panel_ops import LogsView
        await self.go(i, LogsView(self.cog, self.owner_id))

    async def _config(self, i: discord.Interaction):
        from discord_bot.cogs._views_admin_panel_ops import ConfigView
        audit(i, "config.view")
        await self.go(i, ConfigView(self.cog, self.owner_id))

    async def _database(self, i: discord.Interaction):
        from discord_bot.cogs._views_admin_panel_ops import DatabaseView
        # Counting rows can outlast Discord's 3-second reply window on a big or
        # remote database, so acknowledge first and edit once the numbers are in.
        await i.response.defer()
        view = DatabaseView(self.cog, self.owner_id)
        await view.load()
        await i.edit_original_response(view=view)

    async def _watchlist(self, i: discord.Interaction):
        from discord_bot.cogs._views_admin_panel_safety import WatchlistView
        view = WatchlistView(self.cog, self.owner_id)
        await view.load()
        await self.go(i, view)

    async def _reports(self, i: discord.Interaction):
        from discord_bot.cogs._views_admin_panel_safety import ReportsView
        view = ReportsView(self.cog, self.owner_id)
        await view.load()
        await self.go(i, view)

    async def _status(self, i: discord.Interaction):
        from discord_bot.cogs._views_admin_panel_safety import StatusView
        view = StatusView(self.cog, self.owner_id)
        await view.load()
        await self.go(i, view)

    async def _honeypot(self, i: discord.Interaction):
        from discord_bot.cogs._views_admin_panel_safety import HoneypotView
        view = HoneypotView(self.cog, self.owner_id)
        await view.load()
        await self.go(i, view)

    async def _money(self, i: discord.Interaction):
        from discord_bot.cogs._views_admin_panel_money import MoneyHubView
        await self.go(i, MoneyHubView(self.cog, self.owner_id))

    async def _referral(self, i: discord.Interaction):
        from discord_bot.cogs._views_admin_panel_referral import ReferralGiveawayView
        view = ReferralGiveawayView(self.cog, self.owner_id)
        await view.load()
        await self.go(i, view)

    async def _ads(self, i: discord.Interaction):
        from discord_bot.cogs._views_admin_panel_ads import AdsView
        view = AdsView(self.cog, self.owner_id)
        await view.load()
        await self.go(i, view)

    async def _health(self, i: discord.Interaction):
        from discord_bot.cogs._views_admin_panel_inspect import HealthView
        view = HealthView(self.cog, self.owner_id)
        await view.load()
        await self.go(i, view)

    async def _inspect_server(self, i: discord.Interaction):
        from discord_bot.cogs._views_admin_panel_inspect import LookupModal
        await i.response.send_modal(LookupModal(self.cog, "server"))

    async def _inspect_user(self, i: discord.Interaction):
        from discord_bot.cogs._views_admin_panel_inspect import LookupModal
        await i.response.send_modal(LookupModal(self.cog, "user"))

    async def _close(self, i: discord.Interaction):
        self.stop()
        await i.response.edit_message(content="Panel closed.", view=None)


# ── payments ─────────────────────────────────────────────────────────────

class ReferenceModal(discord.ui.Modal):
    """Generic 'enter a payment reference (+ optional extra)' form used by
    Approve / Reject / Assign."""

    def __init__(self, clone_admin, kind: str):
        """`clone_admin` is the CloneAdminCog (or None if it isn't loaded)."""
        titles = {"approve": "Approve payment", "reject": "Reject payment", "assign": "Assign payment to a server"}
        super().__init__(title=titles[kind], timeout=PANEL_TIMEOUT)
        self.clone_admin, self.kind = clone_admin, kind
        self.reference = discord.ui.TextInput(label="Payment reference", max_length=200)
        self.add_item(self.reference)
        self.amount = self.server_id = None
        if kind == "approve":
            self.amount = discord.ui.TextInput(
                label="Amount paid in GHS (only if required)", required=False, max_length=20,
                placeholder="Leave blank for Paystack/Gumroad rows")
            self.add_item(self.amount)
        elif kind == "assign":
            self.server_id = discord.ui.TextInput(label="Server (guild) ID", max_length=25)
            self.add_item(self.server_id)

    async def on_submit(self, interaction: discord.Interaction):
        clone = self.clone_admin
        if clone is None:
            await interaction.response.send_message("Payments module isn't loaded.", ephemeral=True)
            return
        ref = self.reference.value.strip()
        if self.kind == "approve":
            amount = None
            raw = (self.amount.value or "").strip().replace(",", "")
            if raw:
                try:
                    amount = float(raw)
                except ValueError:
                    await interaction.response.send_message("Amount must be a number, e.g. `150` or `149.50`.", ephemeral=True)
                    return
            audit(interaction, "payment.approve", reference=ref, amount=amount)
            await clone.approvepayment(interaction, reference=ref, amount=amount)
        elif self.kind == "reject":
            audit(interaction, "payment.reject", reference=ref)
            await clone.rejectpayment(interaction, reference=ref)
        else:
            audit(interaction, "payment.assign", reference=ref, server_id=self.server_id.value.strip())
            await clone.assignpayment(interaction, reference=ref, server_id=self.server_id.value)


class PaymentsView(PanelView):
    title = "💳 Payments"

    def body(self):
        return ["Review the queue, or act on a payment by reference.",
                "-# Approve/Reject keep their per-payment approver check; Assign and Mode are owner-only."]

    def controls(self):
        P, S, G, D = (discord.ButtonStyle.primary, discord.ButtonStyle.secondary,
                      discord.ButtonStyle.success, discord.ButtonStyle.danger)
        return [
            _btn("Pending queue", P, self._pending, "📋"),
            _btn("Approve", G, self._approve, "✅"),
            _btn("Reject", D, self._reject, "❌"),
            _btn("Assign to server", S, self._assign, "🔗"),
            _btn("Payment mode", S, self._mode, "⚙️"),
            _btn("Back", S, self._back, "⬅️"),
        ]

    async def _pending(self, i: discord.Interaction):
        clone = self.cog.clone_admin
        if clone is None:
            await i.response.send_message("Payments module isn't loaded.", ephemeral=True)
            return
        audit(i, "payment.pending_view")
        # Opens the existing paged approve/reject queue as its own ephemeral
        # message (it is a separate Components-v2 view with its own buttons).
        await clone.pendingpayments(i)

    async def _approve(self, i): await i.response.send_modal(ReferenceModal(self.cog.clone_admin, "approve"))
    async def _reject(self, i): await i.response.send_modal(ReferenceModal(self.cog.clone_admin, "reject"))
    async def _assign(self, i): await i.response.send_modal(ReferenceModal(self.cog.clone_admin, "assign"))

    async def _mode(self, i: discord.Interaction):
        clones = await db.list_active_discord_clones()
        await self.go(i, PaymentModeView(self.cog, self.owner_id, "payments", clones))

    async def _back(self, i): await self.go(i, HomeView(self.cog, self.owner_id))


class PaymentModeView(PanelView):
    title = "⚙️ Payment mode"

    def __init__(self, cog, owner_id, section, clones: List[dict]):
        self.clones = clones[:23]  # 25 select options minus "main" and "all"
        self.mode: Optional[str] = None
        self.scope: str = "main"     # "main" | "all" | "clone:<id>"
        self._confirm = False
        super().__init__(cog, owner_id, section)

    def body(self):
        mode = dict(PAYMENT_MODES).get(self.mode, "— not chosen —")
        if self.scope == "main":
            scope = "Main bot"
        elif self.scope == "all":
            scope = "Every active clone (not the main bot)"
        else:
            scope = f"Clone `#{self.scope.split(':')[1]}`"
        lines = [f"**Mode:** {mode}", f"**Applies to:** {scope}"]
        if len(self.clones) < 1:
            lines.append("-# No active clones found.")
        if self._confirm:
            lines.append("⚠️ **Press Confirm to apply.** This changes how every purchase is routed, immediately.")
        return lines

    def controls(self):
        mode_sel = discord.ui.Select(
            placeholder="Choose payment mode",
            options=[discord.SelectOption(label=label[:100], value=v, default=(v == self.mode))
                     for v, label in PAYMENT_MODES])
        mode_sel.callback = self._pick_mode
        scope_opts = [discord.SelectOption(label="Main bot", value="main", default=self.scope == "main"),
                      discord.SelectOption(label="All active clones", value="all", default=self.scope == "all")]
        for c in self.clones:
            v = f"clone:{c['clone_id']}"
            scope_opts.append(discord.SelectOption(
                label=f"Clone #{c['clone_id']} — {c['bot_username']}"[:100], value=v, default=self.scope == v))
        scope_sel = discord.ui.Select(placeholder="Apply to…", options=scope_opts)
        scope_sel.callback = self._pick_scope
        S, G = discord.ButtonStyle.secondary, discord.ButtonStyle.success
        return [
            mode_sel, scope_sel,
            _btn("Confirm" if self._confirm else "Apply", G if self._confirm else discord.ButtonStyle.primary,
                 self._apply, "✅", disabled=self.mode is None),
            _btn("Back", S, self._back, "⬅️"),
        ]

    async def _refresh(self, i):
        self._confirm = False
        self._build()
        await i.response.edit_message(view=self)

    async def _pick_mode(self, i: discord.Interaction):
        self.mode = i.data["values"][0]
        await self._refresh(i)

    async def _pick_scope(self, i: discord.Interaction):
        self.scope = i.data["values"][0]
        await self._refresh(i)

    async def _apply(self, i: discord.Interaction):
        if not self._confirm:
            self._confirm = True
            self._build()
            await i.response.edit_message(view=self)
            return
        clone = self.cog.clone_admin
        label = dict(PAYMENT_MODES)[self.mode]
        choice = app_commands.Choice(name=label, value=self.mode)
        audit(i, "payment.mode", mode=self.mode, scope=self.scope)
        kwargs = {}
        if self.scope == "all":
            kwargs["all_clones"] = True
        elif self.scope.startswith("clone:"):
            kwargs["clone_id"] = int(self.scope.split(":")[1])
        self._confirm = False
        await clone.paymentmode(i, mode=choice, **kwargs)  # defers + replies via followup
        await self.soft_refresh(i)                          # reset the Confirm button

    async def _back(self, i): await self.go(i, PaymentsView(self.cog, self.owner_id, "payments"))


# ── broadcast ────────────────────────────────────────────────────────────

BROADCAST_TARGETS = [
    ("users", "Users — everyone across main bot + clones"),
    ("admins", "Admins — clone owners/operators only"),
    ("servers", "Server owners — owner of every server"),
    ("modlogs", "Mod-log channels — post into each server's mod-log"),
]


class BroadcastComposeModal(discord.ui.Modal, title="Compose broadcast"):
    def __init__(self, view: "BroadcastView"):
        super().__init__(timeout=PANEL_TIMEOUT)
        self.bview = view
        self.message = discord.ui.TextInput(
            label="Announcement text", style=discord.TextStyle.paragraph, max_length=2000,
            default=view.message or None)
        self.add_item(self.message)
        self.upload = discord.ui.FileUpload(required=False, min_values=0, max_values=1)
        self.add_item(discord.ui.Label(text="Attachment (optional)", component=self.upload))

    async def on_submit(self, interaction: discord.Interaction):
        self.bview.message = self.message.value
        files = list(getattr(self.upload, "values", []) or [])
        self.bview.attachment = files[0] if files else self.bview.attachment
        self.bview._confirm = False
        self.bview._build()
        await interaction.response.edit_message(view=self.bview)


class BroadcastView(PanelView):
    title = "📢 Broadcast"

    def __init__(self, cog, owner_id, section, in_dm: bool):
        self.in_dm = in_dm
        self.message: Optional[str] = None
        self.attachment: Optional[discord.Attachment] = None
        self.target: str = "users"
        self.clone_id: Optional[int] = None
        self.clones: List[dict] = []
        self._confirm = False
        super().__init__(cog, owner_id, section)

    def body(self):
        if not self.in_dm:
            return ["Broadcasts can only be sent from a **DM with the bot** (a safety rule carried over from "
                    "the slash command). Run `/admin panel` in a DM to use this screen.",
                    "-# You can still check delivery status below."]
        preview = (self.message[:600] + ("…" if len(self.message) > 600 else "")) if self.message else "— no text yet —"
        tgt = dict(BROADCAST_TARGETS)[self.target]
        scope = f"clone `#{self.clone_id}` only" if self.clone_id else "main bot + all clones"
        lines = [f"**Target:** {tgt}", f"**Scope:** {scope}",
                 f"**Attachment:** {self.attachment.filename if self.attachment else 'none'}",
                 "**Message:**", f"> {preview}".replace("\n", "\n> ")]
        if self.target == "admins" and self.clone_id:
            lines.append("⚠️ The clone filter doesn't apply to *Admins* — pick another target or clear the clone.")
        if self._confirm:
            lines.append("⚠️ **Press Confirm to queue this broadcast.** It goes to real users.")
        return lines

    def controls(self):
        S, P, G, D = (discord.ButtonStyle.secondary, discord.ButtonStyle.primary,
                      discord.ButtonStyle.success, discord.ButtonStyle.danger)
        items: list = []
        if self.in_dm:
            tsel = discord.ui.Select(
                placeholder="Who receives it?",
                options=[discord.SelectOption(label=l[:100], value=v, default=v == self.target)
                         for v, l in BROADCAST_TARGETS])
            tsel.callback = self._pick_target
            items.append(tsel)
            copts = [discord.SelectOption(label="Main bot + all clones", value="none", default=self.clone_id is None)]
            for c in self.clones[:24]:
                copts.append(discord.SelectOption(
                    label=f"Clone #{c['clone_id']} — {c['bot_username']}"[:100], value=str(c["clone_id"]),
                    default=self.clone_id == c["clone_id"]))
            csel = discord.ui.Select(placeholder="Restrict to a clone?", options=copts)
            csel.callback = self._pick_clone
            items.append(csel)
            ready = bool(self.message) and not (self.target == "admins" and self.clone_id)
            items += [
                _btn("Write / edit message", P, self._compose, "✍️"),
                _btn("Confirm & send" if self._confirm else "Send…", G if self._confirm else P,
                     self._send, "📤", disabled=not ready),
            ]
        items += [_btn("Delivery status", S, self._status, "📊"), _btn("Back", S, self._back, "⬅️")]
        return items

    async def _ensure_clones(self):
        if not self.clones:
            try:
                self.clones = await db.list_active_discord_clones()
            except Exception:
                logger.exception("[admin-panel] couldn't load clones")

    async def _refresh(self, i):
        self._confirm = False
        self._build()
        await i.response.edit_message(view=self)

    async def _pick_target(self, i):
        self.target = i.data["values"][0]
        await self._refresh(i)

    async def _pick_clone(self, i):
        v = i.data["values"][0]
        self.clone_id = None if v == "none" else int(v)
        await self._refresh(i)

    async def _compose(self, i: discord.Interaction):
        await i.response.send_modal(BroadcastComposeModal(self))

    async def _send(self, i: discord.Interaction):
        if not self.message:
            # Draft was already consumed (double-click / stale button): never re-send.
            await i.response.send_message("Nothing to send — that broadcast was already queued. "
                                          "Write a new message to send another.", ephemeral=True)
            return
        if not self._confirm:
            self._confirm = True
            self._build()
            await i.response.edit_message(view=self)
            return
        clone = self.cog.clone_admin
        choice = app_commands.Choice(name=dict(BROADCAST_TARGETS)[self.target], value=self.target)
        audit(i, "broadcast.send", target=self.target, clone=self.clone_id,
              chars=len(self.message), attachment=bool(self.attachment))
        message, attachment = self.message, self.attachment
        # Consume the draft BEFORE the slow call so a double-click can't re-send.
        self.message, self.attachment, self._confirm = None, None, False
        await clone.ownerbroadcast(
            i, message=message, target=choice, attachment=attachment,
            clone=str(self.clone_id) if self.clone_id else None)
        await self.soft_refresh(i)

    async def _status(self, i: discord.Interaction):
        audit(i, "broadcast.status")
        await self.cog.clone_admin.broadcaststatus(i)

    async def _back(self, i): await self.go(i, HomeView(self.cog, self.owner_id))


async def open_home(cog, interaction: discord.Interaction) -> None:
    view = HomeView(cog, interaction.user.id)
    await interaction.response.send_message(view=view, ephemeral=True)
