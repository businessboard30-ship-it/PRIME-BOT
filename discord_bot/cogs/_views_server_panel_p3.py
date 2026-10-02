"""
Server Owners Panel — Phase 3 screens (see SERVER_PANEL_PLAN.md).

Stats, change history, and a Help & tools hub (report a problem, reset a
feature to defaults, copy settings from another server). Same rules as
Phases 1 and 2: thin shell over modules/server_panel.py, access re-checked on
every click and modal submit, everything scoped by (guild_id, clone_id),
destructive actions are two-step (pick, then Confirm).
"""

from __future__ import annotations

import logging
from typing import List

import discord

from modules import server_panel as sp
from discord_bot.cogs._views_server_panel import ServerPanelView, _btn, clone_id_of, guard, read_only_ok

logger = logging.getLogger(__name__)

P, S, G, D = (discord.ButtonStyle.primary, discord.ButtonStyle.secondary,
              discord.ButtonStyle.success, discord.ButtonStyle.danger)


# ── stats ────────────────────────────────────────────────────────────────

class StatsView(ServerPanelView):
    title = "📊 Stats"

    @classmethod
    async def load(cls, interaction):
        return {"s": await sp.server_stats(interaction.guild, clone_id_of(interaction))}

    def body(self) -> List[str]:
        s = self.data.get("s", {})
        members, active = s.get("members"), s.get("active_7d")
        lines = [
            f"Members: **{members:,}**" if members else "Members: N/A",
            f"Active (7d): **{active:,}**" if active is not None else "Active (7d): N/A",
            f"Server age: {s.get('age', 'N/A')}",
            f"Text channels: {s.get('text', 0)} · Voice: {s.get('voice', 0)} · Roles: {s.get('roles', 0)}",
            f"Setup score: **{s.get('setup_done', 0)}/{s.get('setup_total', 8)}**",
        ]
        if active is not None and members:
            lines.append(f"About {active / members * 100:.0f}% of members were active in 7 days (XP-based estimate).")
        if s.get("tips"):
            lines.append("**Grow the server**")
            lines += s["tips"]
        return lines

    def controls(self):
        return [_btn("Refresh", S, self._refresh, "🔄"), self.back_button()]

    @read_only_ok
    async def _refresh(self, interaction: discord.Interaction):
        await interaction.response.defer()
        await self.reload(interaction)


# ── change history ───────────────────────────────────────────────────────

class HistoryView(ServerPanelView):
    title = "🕘 Change history"

    @classmethod
    async def load(cls, interaction):
        try:
            rows = await sp.recent_changes(interaction.guild_id, clone_id_of(interaction))
        except Exception:
            logger.debug("[server-panel] history read failed", exc_info=True)
            return {"rows": [], "error": True}
        return {"rows": rows}

    def body(self) -> List[str]:
        if self.data.get("error"):
            return ["History is unavailable right now. Try again in a moment."]
        lines = sp.format_history(self.data.get("rows", []))
        if not lines:
            return ["No changes made from the panel yet."]
        return [f"Last {len(lines)} change(s) made from this panel:", *lines]

    def controls(self):
        return [_btn("Refresh", S, self._refresh, "🔄"), self.back_button()]

    @read_only_ok
    async def _refresh(self, interaction: discord.Interaction):
        await interaction.response.defer()
        await self.reload(interaction)


# ── help & tools hub ─────────────────────────────────────────────────────

class ReportModal(discord.ui.Modal):
    text = discord.ui.TextInput(label="What happened or what do you want?",
                                style=discord.TextStyle.paragraph, max_length=sp.REPORT_MAX)

    def __init__(self, kind: str, opener_id: int):
        super().__init__(title=sp.REPORT_KINDS[kind])
        self.kind = kind
        self.opener_id = opener_id

    async def on_submit(self, interaction: discord.Interaction):
        if interaction.user.id != self.opener_id:
            await interaction.response.send_message("This panel belongs to someone else.", ephemeral=True)
            return
        if not await guard(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        message = sp.format_report(self.kind, str(self.text.value))
        cog = interaction.client.get_cog("Feedback")
        if cog is not None:
            await cog.ai_feedback(interaction, message)   # same validation, storage and owner DM as /feedback
        else:
            from database import db
            await db.add_discord_user_feedback(interaction.user.id, interaction.guild_id, message)
            await interaction.followup.send("✅ Thanks — your message has been sent to the team.", ephemeral=True)
        await sp.record_change(interaction.guild_id, clone_id_of(interaction), interaction.user.id,
                               f"report.{self.kind}", None, "sent")


class HelpToolsView(ServerPanelView):
    title = "🧰 Help & tools"

    def body(self) -> List[str]:
        return [
            "Send the team a problem report or an idea, put one feature back to its defaults, "
            "or copy settings from another server you manage.",
        ]

    def controls(self):
        return [
            _btn("Report a problem", P, self._report("problem"), "🐞"),
            _btn("Suggest a feature", P, self._report("idea"), "💡"),
            _btn("Reset a feature", D, self.nav(ResetView), "♻️"),
            _btn("Copy settings", S, self.nav(CopyView), "📋"),
            self.back_button(),
        ]

    def _report(self, kind: str):
        async def cb(interaction: discord.Interaction):
            await interaction.response.send_modal(ReportModal(kind, interaction.user.id))
        return cb


# ── reset to defaults (two-step) ─────────────────────────────────────────

class ResetView(ServerPanelView):
    title = "♻️ Reset a feature"
    accent = discord.Color.red()
    pending_key = None
    notice = None

    def body(self) -> List[str]:
        lines = ["Pick a feature to put back to its default settings. Nothing changes until you tap Confirm."]
        if self.pending_key:
            lines.append(f"⚠️ Reset **{sp.RESET_GROUPS[self.pending_key]}** to defaults?")
            if self.pending_key in sp.RESET_NOTES:
                lines.append(sp.RESET_NOTES[self.pending_key])
        if self.notice:
            lines.append(self.notice)
        return lines

    def controls(self):
        sel = discord.ui.Select(
            placeholder="Feature to reset…",
            options=[discord.SelectOption(label=label, value=key, default=key == self.pending_key)
                     for key, label in sp.RESET_GROUPS.items()])
        sel.callback = lambda i: self._pick(i, sel.values[0])
        out: list = [sel]
        if self.pending_key:
            out.append(_btn("Confirm reset", D, self._confirm, "♻️"))
            out.append(_btn("Cancel", S, self._cancel))
        out.append(self.back_button(HelpToolsView))
        return out

    async def _rerender(self, interaction):
        self._build()
        await interaction.response.edit_message(view=self)

    async def _pick(self, interaction, key):
        self.pending_key, self.notice = key, None   # first step changes nothing
        await self._rerender(interaction)

    async def _cancel(self, interaction):
        self.pending_key = None
        await self._rerender(interaction)

    async def _confirm(self, interaction):
        key = self.pending_key
        if key is None:
            await interaction.response.defer()
            return
        await interaction.response.defer()
        try:
            await sp.reset_feature(interaction.guild_id, self.clone_id, interaction.user.id, key,
                                   bot=interaction.client)
            self.notice = f"✅ {sp.RESET_GROUPS[key]} is back to defaults."
        except Exception:
            logger.exception("[server-panel] reset %s failed", key)
            self.notice = f"❌ Couldn't reset {sp.RESET_GROUPS[key]}. Nothing more was changed."
        self.pending_key = None
        self._build()
        await interaction.edit_original_response(view=self)


# ── copy settings from another server (two-step, re-verified) ────────────

class CopyView(ServerPanelView):
    title = "📋 Copy settings"
    pending_src = None
    notice = None

    @classmethod
    async def load(cls, interaction):
        return {"servers": sp.copyable_guilds(interaction.client, interaction.guild, interaction.user.id)}

    def body(self) -> List[str]:
        servers = self.data.get("servers", [])
        lines = [
            "Copies from another server you own or manage (where this bot is too) into **this** server. "
            "Channels and roles are matched by name; anything without a match is left alone and listed for you to pick.",
            f"Copied: {', '.join(sp.RESET_GROUPS[f] for f in sp.COPY_FEATURES)}, level-role rewards.",
            f"Not copied: {sp.COPY_EXCLUDED}.",
        ]
        if not servers:
            lines.append("No other server found where you own or manage this bot's server.")
        if self.pending_src:
            name = dict(servers).get(self.pending_src, "that server")
            lines.append(f"⚠️ Replace this server's settings with **{name}**'s? Tap Confirm.")
        if self.notice:
            lines.append(self.notice)
        return lines

    def controls(self):
        servers = self.data.get("servers", [])
        out: list = []
        if servers:
            sel = discord.ui.Select(
                placeholder="Copy from which server?",
                options=[discord.SelectOption(label=name[:100], value=str(gid), default=gid == self.pending_src)
                         for gid, name in servers])
            sel.callback = lambda i: self._pick(i, int(sel.values[0]))
            out.append(sel)
        if self.pending_src:
            out.append(_btn("Confirm copy", D, self._confirm, "📋"))
            out.append(_btn("Cancel", S, self._cancel))
        out.append(self.back_button(HelpToolsView))
        return out

    async def _rerender(self, interaction):
        self._build()
        await interaction.response.edit_message(view=self)

    async def _pick(self, interaction, gid):
        self.pending_src, self.notice = gid, None   # first step changes nothing
        await self._rerender(interaction)

    async def _cancel(self, interaction):
        self.pending_src = None
        await self._rerender(interaction)

    async def _confirm(self, interaction):
        src_id = self.pending_src
        if src_id is None:
            await interaction.response.defer()
            return
        await interaction.response.defer()
        src = interaction.client.get_guild(src_id)
        uid = interaction.user.id
        # Ownership/Manage Server of BOTH servers is checked now, at click time.
        ok = (src is not None and src.id != interaction.guild_id
              and await sp.manages_guild(src, uid)
              and await sp.manages_guild(interaction.guild, uid, getattr(interaction, "permissions", None)))
        if not ok:
            self.notice = "❌ You no longer own or manage both servers, so nothing was copied."
            self.pending_src = None
            self._build()
            await interaction.edit_original_response(view=self)
            return
        rep = await sp.copy_settings(src, interaction.guild, self.clone_id, uid)
        lines = [f"✅ Copied from **{src.name}**: {', '.join(rep.copied) or 'nothing'}."]
        if rep.repick:
            lines.append("Needs your attention:")
            lines += [f"• {r}" for r in rep.repick[:10]]
            if len(rep.repick) > 10:
                lines.append(f"…and {len(rep.repick) - 10} more")
        self.notice = "\n".join(lines)
        self.pending_src = None
        self._build()
        await interaction.edit_original_response(view=self)
