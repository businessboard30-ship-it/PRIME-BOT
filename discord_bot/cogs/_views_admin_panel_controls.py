"""
Owner panel, Batch 1 — Audit log, Kill switches, Blacklist and Premium.

Same rules as the other panel files (see _views_admin_panel.py): buttons,
selects and forms only (no new slash commands), only the person who opened the
panel can use it, access is re-checked on every click and modal submit, and
every action writes an audit line (which is now also stored in the database,
see modules/admin_controls.py).

Reversible, low-risk actions apply immediately (flipping one feature switch,
adding/removing a blacklist entry, extending premium). Anything that hits
everyone or can't be undone with one click is two-step (button -> Confirm):
turning maintenance mode ON and revoking premium.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import List, Optional

import discord

from config import DISCORD_CLONE_ADMIN_IDS
from database import db
from discord_bot.cogs import _views_admin_panel as main
from discord_bot.cogs._views_admin_panel import PANEL_TIMEOUT, PanelView, _btn, allowed_sections, audit
from modules import admin_controls as ac

logger = logging.getLogger(__name__)

CONTROLS = "controls"
BLACKLIST = "blacklist"
PREMIUM = "premium"
AUDIT = "audit"

AUDIT_LIMITS = (10, 15, 20)
TEXT_BUDGET = 3200          # a panel message holds ~4000 characters of text in total
GRANT_MAX_DAYS = 3650


async def _home(view: PanelView, i: discord.Interaction) -> None:
    await view.go(i, main.HomeView(view.cog, view.owner_id))


def _ts(dt: Optional[datetime], style: str = "R") -> str:
    if dt is None:
        return "unknown"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return f"<t:{int(dt.timestamp())}:{style}>"


def _clip(text: Optional[str], n: int) -> str:
    text = (text or "").replace("\n", " ").strip()
    return text if len(text) <= n else text[: n - 1] + "…"


def _fit(lines: List[str], budget: int = TEXT_BUDGET) -> List[str]:
    """Keep lines until the character budget is used up, then say how many were cut."""
    out, used = [], 0
    for idx, line in enumerate(lines):
        if used + len(line) + 1 > budget:
            out.append(f"-# …and {len(lines) - idx} more not shown.")
            break
        out.append(line)
        used += len(line) + 1
    return out


async def _denied(i: discord.Interaction) -> None:
    await i.response.send_message("You're no longer authorized for this.", ephemeral=True)


# ── audit log ────────────────────────────────────────────────────────────

class AuditView(PanelView):
    title = "📜 Audit log"

    def __init__(self, cog, owner_id, section=AUDIT):
        self.limit = AUDIT_LIMITS[0]
        self.rows: List[dict] = []
        self.error = False
        super().__init__(cog, owner_id, section)

    async def load(self) -> None:
        try:
            self.rows = await ac.recent_audit(self.limit)
            self.error = False
        except Exception:
            logger.exception("[admin-panel] couldn't load the audit log")
            self.rows, self.error = [], True
        self._build()

    def body(self):
        if self.error:
            return ["Couldn't load the audit log right now. Try Refresh in a moment."]
        if not self.rows:
            return ["No panel actions recorded yet. Actions you take from here will show up in this list."]
        lines = []
        for r in self.rows:
            where = f" · server `{r['guild_id']}`" if r.get("guild_id") else ""
            detail = f" — {_clip(r.get('details'), 70)}" if r.get("details") else ""
            lines.append(f"{_ts(r['created_at'])} <@{r['admin_id']}> `{r['action']}`{detail}{where}")
        return [f"Last **{len(self.rows)}** panel actions, newest first.", *_fit(lines)]

    def controls(self):
        sel = discord.ui.Select(
            placeholder="How many to show",
            options=[discord.SelectOption(label=f"Latest {n}", value=str(n), default=n == self.limit)
                     for n in AUDIT_LIMITS])
        sel.callback = self._limit
        S = discord.ButtonStyle.secondary
        return [sel, _btn("Refresh", S, self._refresh, "🔄"), _btn("Back", S, self._back, "⬅️")]

    async def _limit(self, i: discord.Interaction):
        self.limit = int(i.data["values"][0])
        await self.load()
        await i.response.edit_message(view=self)

    async def _refresh(self, i: discord.Interaction):
        await self.load()
        await i.response.edit_message(view=self)

    async def _back(self, i): await _home(self, i)


# ── kill switches ────────────────────────────────────────────────────────

class ControlsView(PanelView):
    title = "🎚️ Kill switches"

    def __init__(self, cog, owner_id, section=CONTROLS):
        self.engaged: set = set()
        self.error = False
        self._confirm = False      # maintenance ON is two-step
        super().__init__(cog, owner_id, section)

    async def load(self) -> None:
        try:
            self.engaged = await ac.get_engaged_switches()
            self.error = False
        except Exception:
            logger.exception("[admin-panel] couldn't load the kill switches")
            self.error = True
        self._build()

    def body(self):
        lines = ["Turn a feature's slash commands off instantly (and back on). Owners are never blocked. "
                 "Takes effect within seconds. **Clone registration** also stops the Build Bot button, "
                 "so nobody can register a new clone while it's off."]
        if self.error:
            lines.append("⚠️ Couldn't read the current state, so the buttons below may be out of date. Press Refresh.")
        for key, (label, _) in ac.FEATURES.items():
            lines.append(f"{'⛔ OFF' if key in self.engaged else '✅ on'} — {label}")
        if ac.MAINTENANCE in self.engaged:
            lines.append("🛠️ **Maintenance mode is ON** — every slash command is refused for non-owners.")
        elif self._confirm:
            lines.append("⚠️ **Press Confirm to turn maintenance mode ON. Every slash command will be refused "
                         "for everyone except owners until you turn it off.**")
        return lines

    def controls(self):
        S, G, D = discord.ButtonStyle.secondary, discord.ButtonStyle.success, discord.ButtonStyle.danger
        items = []
        for key, (label, _) in ac.FEATURES.items():
            off = key in self.engaged
            btn = _btn(f"{label}: {'OFF' if off else 'on'}"[:80], D if off else G,
                       self._make_toggle(key), "⛔" if off else "✅")
            items.append(btn)
        maint_on = ac.MAINTENANCE in self.engaged
        if maint_on:
            items.append(_btn("Turn maintenance OFF", G, self._maintenance_off, "🛠️"))
        else:
            items.append(_btn("Confirm maintenance ON" if self._confirm else "Maintenance mode",
                              D if self._confirm else S, self._maintenance_on,
                              "✅" if self._confirm else "🛠️"))
        items += [_btn("Refresh", S, self._refresh, "🔄"), _btn("Back", S, self._back, "⬅️")]
        return items

    def _make_toggle(self, key: str):
        async def cb(i: discord.Interaction):
            await self._set(i, key, key not in self.engaged)
        return cb

    async def _set(self, i: discord.Interaction, key: str, engaged: bool):
        try:
            await ac.set_switch(key, engaged, i.user.id)
        except Exception:
            logger.exception("[admin-panel] couldn't change switch %s", key)
            await i.response.send_message("Couldn't change that switch (database problem). Nothing was changed.",
                                          ephemeral=True)
            return
        audit(i, "controls.switch", switch=key, off=engaged)
        self._confirm = False
        await self.load()
        await i.response.edit_message(view=self)

    async def _maintenance_on(self, i: discord.Interaction):
        if not self._confirm:
            self._confirm = True
            self._build()
            await i.response.edit_message(view=self)
            return
        await self._set(i, ac.MAINTENANCE, True)

    async def _maintenance_off(self, i: discord.Interaction):
        await self._set(i, ac.MAINTENANCE, False)

    async def _refresh(self, i: discord.Interaction):
        self._confirm = False
        await self.load()
        await i.response.edit_message(view=self)

    async def _back(self, i): await _home(self, i)


# ── blacklist ────────────────────────────────────────────────────────────

class BlacklistAddModal(discord.ui.Modal):
    def __init__(self, cog, kind: str):
        super().__init__(title=f"Block a {'person' if kind == 'user' else 'server'}", timeout=PANEL_TIMEOUT)
        self.cog = cog
        self.kind = kind
        self.target = discord.ui.TextInput(
            label="User ID" if kind == "user" else "Server (guild) ID", max_length=25,
            placeholder="e.g. 123456789012345678")
        self.reason = discord.ui.TextInput(
            label="Reason (optional)", required=False, max_length=200, style=discord.TextStyle.paragraph)
        self.add_item(self.target)
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction):
        # Modals bypass the view's interaction_check, so re-check here.
        if BLACKLIST not in allowed_sections(interaction.user.id):
            await _denied(interaction)
            return
        raw = self.target.value.strip()
        if not raw.isdigit() or not (10 <= len(raw) <= 20):
            await interaction.response.send_message(
                "That doesn't look like a Discord ID. It should be a long number (enable Developer Mode, "
                "right-click, **Copy ID**).", ephemeral=True)
            return
        target = int(raw)
        if self.kind == "user" and target in DISCORD_CLONE_ADMIN_IDS:
            await interaction.response.send_message("You can't block a bot owner.", ephemeral=True)
            return
        reason = self.reason.value.strip()
        try:
            await ac.add_blacklist(self.kind, target, reason, interaction.user.id)
        except Exception:
            logger.exception("[admin-panel] couldn't add blacklist entry")
            await interaction.response.send_message("Couldn't save that (database problem). Nothing was changed.",
                                                    ephemeral=True)
            return
        audit(interaction, "blacklist.add", kind=self.kind, target=target, reason=reason)
        view = BlacklistView(self.cog, interaction.user.id)
        view.notice = f"✅ Blocked {self.kind} `{target}`."
        await view.load()
        await interaction.response.edit_message(view=view)


class BlacklistView(PanelView):
    title = "🚫 Blacklist"

    def __init__(self, cog, owner_id, section=BLACKLIST):
        self.rows: List[dict] = []
        self.selected: Optional[str] = None     # "user:123" / "guild:456"
        self.notice: Optional[str] = None
        self.error = False
        super().__init__(cog, owner_id, section)

    @staticmethod
    def _key(r: dict) -> str:
        return f"{r['kind']}:{r['target_id']}"

    async def load(self) -> None:
        try:
            self.rows = await ac.list_blacklist(25)
            self.error = False
        except Exception:
            logger.exception("[admin-panel] couldn't load the blacklist")
            self.rows, self.error = [], True
        if self.selected not in {self._key(r) for r in self.rows}:
            self.selected = None
        self._build()

    def body(self):
        lines = ["Blocked users and servers can't use any slash command, on the main bot or any clone. "
                 "Owners can't be blocked."]
        if self.notice:
            lines.append(self.notice)
        if self.error:
            lines.append("⚠️ Couldn't load the list right now.")
        elif not self.rows:
            lines.append("Nobody is blocked.")
        else:
            entries = []
            for r in self.rows:
                icon = "🧑" if r["kind"] == "user" else "🏠"
                why = f" — {_clip(r.get('reason'), 60)}" if r.get("reason") else ""
                entries.append(f"{icon} `{r['target_id']}`{why} · {_ts(r['created_at'])}")
            lines += _fit(entries)
        return lines

    def controls(self):
        S, D = discord.ButtonStyle.secondary, discord.ButtonStyle.danger
        items: list = []
        if self.rows:
            sel = discord.ui.Select(
                placeholder="Pick an entry to unblock",
                options=[discord.SelectOption(
                    label=f"{'User' if r['kind'] == 'user' else 'Server'} {r['target_id']}"[:100],
                    description=_clip(r.get("reason"), 100) or None,
                    value=self._key(r), default=self._key(r) == self.selected)
                    for r in self.rows])
            sel.callback = self._pick
            items.append(sel)
        items += [
            _btn("Block a person", D, self._add_user, "🧑"),
            _btn("Block a server", D, self._add_guild, "🏠"),
            _btn("Unblock selected", S, self._remove, "♻️", disabled=self.selected is None),
            _btn("Back", S, self._back, "⬅️"),
        ]
        return items

    async def _pick(self, i: discord.Interaction):
        self.selected = i.data["values"][0]
        self.notice = None
        self._build()
        await i.response.edit_message(view=self)

    async def _add_user(self, i: discord.Interaction):
        await i.response.send_modal(BlacklistAddModal(self.cog, "user"))

    async def _add_guild(self, i: discord.Interaction):
        await i.response.send_modal(BlacklistAddModal(self.cog, "guild"))

    async def _remove(self, i: discord.Interaction):
        if self.selected is None:
            await i.response.send_message("Pick an entry first.", ephemeral=True)
            return
        kind, _, raw = self.selected.partition(":")
        target = int(raw)
        try:
            removed = await ac.remove_blacklist(kind, target)
        except Exception:
            logger.exception("[admin-panel] couldn't remove blacklist entry")
            await i.response.send_message("Couldn't remove that (database problem). Nothing was changed.",
                                          ephemeral=True)
            return
        audit(i, "blacklist.remove", kind=kind, target=target)
        self.notice = f"✅ Unblocked {kind} `{target}`." if removed else "That entry was already gone."
        self.selected = None
        await self.load()
        await i.response.edit_message(view=self)

    async def _back(self, i): await _home(self, i)


# ── premium manager ──────────────────────────────────────────────────────

class GrantPremiumModal(discord.ui.Modal, title="Grant premium to a server"):
    def __init__(self, cog):
        super().__init__(timeout=PANEL_TIMEOUT)
        self.cog = cog
        self.guild = discord.ui.TextInput(label="Server (guild) ID", max_length=25,
                                          placeholder="e.g. 123456789012345678")
        self.days = discord.ui.TextInput(label="Days to add", max_length=4, placeholder="e.g. 30")
        self.clone = discord.ui.TextInput(label="Clone ID (leave empty for the main bot)", required=False,
                                          max_length=10)
        self.add_item(self.guild)
        self.add_item(self.days)
        self.add_item(self.clone)

    async def on_submit(self, interaction: discord.Interaction):
        # Modals bypass the view's interaction_check, so re-check here.
        if PREMIUM not in allowed_sections(interaction.user.id):
            await _denied(interaction)
            return
        g, d, c = self.guild.value.strip(), self.days.value.strip(), self.clone.value.strip()
        if not g.isdigit() or not (10 <= len(g) <= 20):
            await interaction.response.send_message("That doesn't look like a server ID.", ephemeral=True)
            return
        if not d.isdigit() or not (1 <= int(d) <= GRANT_MAX_DAYS):
            await interaction.response.send_message(
                f"Days must be a whole number from 1 to {GRANT_MAX_DAYS}.", ephemeral=True)
            return
        if c and not c.isdigit():
            await interaction.response.send_message("Clone ID must be a number, or empty for the main bot.",
                                                    ephemeral=True)
            return
        guild_id, days, clone_id = int(g), int(d), (int(c) if c else None)
        try:
            expires = await db.activate_guild_premium(guild_id, interaction.user.id, days, clone_id)
        except Exception:
            logger.exception("[admin-panel] couldn't grant premium")
            await interaction.response.send_message("Couldn't grant premium (database problem). Nothing was changed.",
                                                    ephemeral=True)
            return
        audit(interaction, "premium.grant", guild=guild_id, clone=clone_id, days=days)
        view = PremiumView(self.cog, interaction.user.id)
        view.notice = f"✅ Added {days} day(s) to `{guild_id}` — now runs until {_ts(expires, 'f')}."
        await view.load()
        await interaction.response.edit_message(view=view)


class PremiumView(PanelView):
    title = "💎 Premium manager"

    def __init__(self, cog, owner_id, section=PREMIUM):
        self.rows: List[dict] = []
        self.selected: Optional[str] = None     # "guild_id:clone_id" (clone 0 = main bot)
        self.notice: Optional[str] = None
        self.error = False
        self._confirm = False                   # revoke is two-step
        super().__init__(cog, owner_id, section)

    @staticmethod
    def _key(r: dict) -> str:
        return f"{r['guild_id']}:{r['clone_id'] or 0}"

    def _current(self) -> Optional[dict]:
        return next((r for r in self.rows if self._key(r) == self.selected), None)

    @staticmethod
    def _name(r: dict) -> str:
        return r.get("guild_name") or "Unknown server"

    @staticmethod
    def _bot(r: dict) -> str:
        return "main bot" if not r.get("clone_id") else f"clone #{r['clone_id']}"

    async def load(self) -> None:
        try:
            self.rows = await ac.list_premium(25)
            self.error = False
        except Exception:
            logger.exception("[admin-panel] couldn't load premium servers")
            self.rows, self.error = [], True
        if self._current() is None:
            self.selected, self._confirm = None, False
        self._build()

    def body(self):
        lines = ["Servers with premium, **soonest to expire first**."]
        if self.notice:
            lines.append(self.notice)
        if self.error:
            lines.append("⚠️ Couldn't load the list right now.")
        elif not self.rows:
            lines.append("No servers have active premium.")
        else:
            now = datetime.now(timezone.utc)
            entries = []
            for r in self.rows:
                exp = r["expires_at"] if r["expires_at"].tzinfo else r["expires_at"].replace(tzinfo=timezone.utc)
                state = "⚠️ in grace period" if exp < now else "expires"
                entries.append(f"**{_clip(self._name(r), 40)}** (`{r['guild_id']}`, {self._bot(r)}) — "
                               f"{state} {_ts(exp)}")
            lines += _fit(entries)
        cur = self._current()
        if cur is not None:
            lines.append(f"**Selected:** {_clip(self._name(cur), 40)} (`{cur['guild_id']}`)")
            if self._confirm:
                lines.append("⚠️ **Press Confirm to end this server's premium right now.** "
                             "It stops working immediately, with no grace period.")
        return lines

    def controls(self):
        S, G, D = discord.ButtonStyle.secondary, discord.ButtonStyle.success, discord.ButtonStyle.danger
        items: list = []
        if self.rows:
            sel = discord.ui.Select(
                placeholder="Pick a server",
                options=[discord.SelectOption(
                    label=_clip(f"{self._name(r)} — {self._bot(r)}", 100),
                    description=f"ID {r['guild_id']}", value=self._key(r), default=self._key(r) == self.selected)
                    for r in self.rows])
            sel.callback = self._pick
            items.append(sel)
        has = self._current() is not None
        items += [
            _btn("+30 days", G, self._make_extend(30), "➕", disabled=not has),
            _btn("+90 days", G, self._make_extend(90), "➕", disabled=not has),
            _btn("Confirm revoke" if self._confirm else "Revoke", D if self._confirm else S, self._revoke,
                 "✅" if self._confirm else "🛑", disabled=not has),
            _btn("Grant to a server…", S, self._grant, "🎁"),
            _btn("Back", S, self._back, "⬅️"),
        ]
        return items

    async def _pick(self, i: discord.Interaction):
        self.selected = i.data["values"][0]
        self._confirm = False
        self.notice = None
        self._build()
        await i.response.edit_message(view=self)

    def _make_extend(self, days: int):
        async def cb(i: discord.Interaction):
            cur = self._current()
            if cur is None:
                await i.response.send_message("Pick a server first.", ephemeral=True)
                return
            try:
                expires = await db.activate_guild_premium(
                    cur["guild_id"], i.user.id, days, cur["clone_id"] or None)
            except Exception:
                logger.exception("[admin-panel] couldn't extend premium")
                await i.response.send_message("Couldn't extend premium (database problem). Nothing was changed.",
                                              ephemeral=True)
                return
            audit(i, "premium.extend", guild=cur["guild_id"], clone=cur["clone_id"], days=days)
            self.notice = f"✅ Added {days} days to `{cur['guild_id']}` — now runs until {_ts(expires, 'f')}."
            self._confirm = False
            await self.load()
            await i.response.edit_message(view=self)
        return cb

    async def _revoke(self, i: discord.Interaction):
        cur = self._current()
        if cur is None:
            await i.response.send_message("Pick a server first.", ephemeral=True)
            return
        if not self._confirm:
            self._confirm = True
            self._build()
            await i.response.edit_message(view=self)
            return
        try:
            done = await ac.revoke_premium(cur["guild_id"], cur["clone_id"] or None)
        except Exception:
            logger.exception("[admin-panel] couldn't revoke premium")
            await i.response.send_message("Couldn't revoke premium (database problem). Nothing was changed.",
                                          ephemeral=True)
            return
        audit(i, "premium.revoke", guild=cur["guild_id"], clone=cur["clone_id"])
        self.notice = (f"✅ Ended premium for `{cur['guild_id']}`." if done
                       else "That server's subscription row wasn't found.")
        self.selected, self._confirm = None, False
        await self.load()
        await i.response.edit_message(view=self)

    async def _grant(self, i: discord.Interaction):
        await i.response.send_modal(GrantPremiumModal(self.cog))

    async def _back(self, i): await _home(self, i)
