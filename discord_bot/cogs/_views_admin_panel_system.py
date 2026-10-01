"""
Owner panel, Phase 4 — Bump, Feedback and System.

Covers: bump cooldown / server list / review queue / reminder cleanup, the
feedback viewer, and the System screen (anime submissions, hosting channel,
env check, revenue, user export).

Same rules as Phase 1 (see _views_admin_panel.py): this is a thin UI shell.
Every action runs the SAME coroutine the matching `/admin ...` slash command
runs (via `call_cmd`), so auth checks, DB reads/writes and replies are
identical and there is no second copy of the logic. Only the person who opened
the panel can use it and access is re-checked on every click and modal submit.

Two-step (button -> explicit Confirm) actions: reminder cleanup (deletes
messages), hosting channel (re-points where uploads are stored) and user
export (posts every user record).
"""

from __future__ import annotations

import logging
from typing import List, Optional

import discord

from discord_bot.cogs import _views_admin_panel as main
from discord_bot.cogs._views_admin_panel import (
    PANEL_TIMEOUT,
    PanelView,
    _btn,
    allowed_sections,
    audit,
    call_cmd,
)

logger = logging.getLogger(__name__)

BUMP = "bump"
FEEDBACK = "feedback"
SYSTEM = "system"

FEEDBACK_LIMITS = (5, 10, 25)   # /admin feedback accepts 1..25


async def _module_missing(i: discord.Interaction, what: str) -> None:
    msg = f"{what} isn't loaded right now."
    if i.response.is_done():
        await i.followup.send(msg, ephemeral=True)
    else:
        await i.response.send_message(msg, ephemeral=True)


async def _home(view: PanelView, i: discord.Interaction) -> None:
    await view.go(i, main.HomeView(view.cog, view.owner_id))


# ── bump ─────────────────────────────────────────────────────────────────

class CooldownModal(discord.ui.Modal, title="Set bump cooldown"):
    def __init__(self, cog):
        super().__init__(timeout=PANEL_TIMEOUT)
        self.cog = cog
        self.minutes = discord.ui.TextInput(
            label="Cooldown in minutes", max_length=6, placeholder="e.g. 120")
        self.add_item(self.minutes)

    async def on_submit(self, interaction: discord.Interaction):
        # Modals bypass the view's interaction_check, so re-check here.
        if BUMP not in allowed_sections(interaction.user.id):
            await interaction.response.send_message("You're no longer authorized for this.", ephemeral=True)
            return
        raw = self.minutes.value.strip()
        if not raw.isdigit() or int(raw) < 1:
            await interaction.response.send_message(
                "Enter a whole number of minutes, 1 or more (e.g. `120`).", ephemeral=True)
            return
        bump = self.cog.bump
        if bump is None:
            await _module_missing(interaction, "Bump")
            return
        minutes = int(raw)
        audit(interaction, "bump.cooldown_set", minutes=minutes)
        await call_cmd(bump, "bumpadmin_cooldown", interaction, minutes=minutes)


class BumpHubView(PanelView):
    title = "📡 Bump network"

    def __init__(self, cog, owner_id, section=BUMP):
        self._confirm = False
        super().__init__(cog, owner_id, section)

    def body(self):
        lines = ["Cooldown, the servers on the network, and the bot-listing review queue.",
                 "-# Each button runs the same command as its `/admin bump ...` slash command."]
        if self._confirm:
            lines.append("⚠️ **Press Confirm to delete the old reminder burst.** It purges the bot's own "
                         "\"cooldown's reset\" messages (last 100 per bump channel) in every configured server.")
        return lines

    def controls(self):
        P, S, G, D = (discord.ButtonStyle.primary, discord.ButtonStyle.secondary,
                      discord.ButtonStyle.success, discord.ButtonStyle.danger)
        return [
            _btn("View cooldown", S, self._view_cd, "⏱️"),
            _btn("Set cooldown", P, self._set_cd, "✏️"),
            _btn("Configured servers", P, self._list, "📡"),
            _btn("Review queue", P, self._review, "📝"),
            _btn("Confirm cleanup" if self._confirm else "Clean up reminders",
                 G if self._confirm else D, self._cleanup, "✅" if self._confirm else "🧹"),
            _btn("Back", S, self._back, "⬅️"),
        ]

    async def _cmd(self, i: discord.Interaction, name: str, action: str, **kwargs):
        bump = self.cog.bump
        if bump is None:
            await _module_missing(i, "Bump")
            return
        audit(i, action)
        await call_cmd(bump, name, i, **kwargs)

    async def _view_cd(self, i): await self._cmd(i, "bumpadmin_cooldown", "bump.cooldown_view")
    async def _list(self, i): await self._cmd(i, "bumpadmin_list", "bump.list")
    async def _review(self, i): await self._cmd(i, "bumpadmin_review", "bump.review")

    async def _set_cd(self, i: discord.Interaction):
        await i.response.send_modal(CooldownModal(self.cog))

    async def _cleanup(self, i: discord.Interaction):
        if not self._confirm:
            self._confirm = True
            self._build()
            await i.response.edit_message(view=self)
            return
        self._confirm = False
        await self._cmd(i, "bumpadmin_cleanup_reminders", "bump.cleanup_reminders")  # defers + replies via followup
        await self.soft_refresh(i)                                                   # reset the Confirm button

    async def _back(self, i): await _home(self, i)


# ── feedback ─────────────────────────────────────────────────────────────

class FeedbackView(PanelView):
    title = "📬 Feedback"

    def __init__(self, cog, owner_id, section=FEEDBACK):
        self.limit = 10
        super().__init__(cog, owner_id, section)

    def body(self):
        return ["Read what users have sent through `/feedback`, newest first.",
                f"**Show the latest:** {self.limit}"]

    def controls(self):
        sel = discord.ui.Select(
            placeholder="How many to show?",
            options=[discord.SelectOption(label=f"Latest {n}", value=str(n), default=n == self.limit)
                     for n in FEEDBACK_LIMITS])
        sel.callback = self._pick
        return [sel,
                _btn("View feedback", discord.ButtonStyle.primary, self._view, "📋"),
                _btn("Back", discord.ButtonStyle.secondary, self._back, "⬅️")]

    async def _pick(self, i: discord.Interaction):
        self.limit = int(i.data["values"][0])
        self._build()
        await i.response.edit_message(view=self)

    async def _view(self, i: discord.Interaction):
        fb = self.cog.feedback_cog
        if fb is None:
            await _module_missing(i, "Feedback")
            return
        audit(i, "feedback.view", limit=self.limit)
        await call_cmd(fb, "viewfeedback", i, limit=self.limit)

    async def _back(self, i): await _home(self, i)


# ── system ───────────────────────────────────────────────────────────────

class SystemView(PanelView):
    title = "⚙️ System"

    def __init__(self, cog, owner_id, section=SYSTEM):
        self._confirm: Optional[str] = None   # None | "hosting" | "export"
        super().__init__(cog, owner_id, section)

    def body(self):
        lines = ["Analytics, submissions, revenue, pending checkouts, AI/env health checks, uploads hosting and data export.",
                 "-# Each button runs the same command as its `/admin ...` slash command."]
        if self._confirm == "hosting":
            lines.append("⚠️ **Press Confirm to use THIS channel for hosting uploaded welcome backgrounds.** "
                         "Open the panel in the private channel you want, then confirm.")
        elif self._confirm == "export":
            lines.append("⚠️ **Press Confirm to export every user record as a CSV.** It contains personal data.")
        if main.DISCORD_OWNER_BROADCAST_IDS and self.owner_id not in main.DISCORD_OWNER_BROADCAST_IDS:
            lines.append("-# Hosting channel: not available to your account.")
        return lines

    def controls(self):
        P, S, G, D = (discord.ButtonStyle.primary, discord.ButtonStyle.secondary,
                      discord.ButtonStyle.success, discord.ButtonStyle.danger)
        hosting_ok = self.owner_id in main.DISCORD_OWNER_BROADCAST_IDS
        hosting_on = self._confirm == "hosting"
        export_on = self._confirm == "export"
        return [
            _btn("Stats", P, self._stats, "📈"),
            _btn("Submissions", P, self._submissions, "🗂️"),
            _btn("Revenue", P, self._revenue, "💵"),
            _btn("Pending checkouts", P, self._pending, "⏳"),
            _btn("Env check", S, self._envcheck, "🩺"),
            _btn("AI debug", S, self._aidebug, "🤖"),
            _btn("Confirm hosting channel" if hosting_on else "Set hosting channel",
                 G if hosting_on else S, self._hosting, "✅" if hosting_on else "🖼️", disabled=not hosting_ok),
            _btn("Confirm export" if export_on else "Export users",
                 G if export_on else D, self._export, "✅" if export_on else "📁"),
            _btn("Back", S, self._back, "⬅️"),
        ]

    async def _plain(self, i: discord.Interaction, owner_cog, label: str, name: str, action: str):
        if owner_cog is None:
            await _module_missing(i, label)
            return
        audit(i, action)
        await call_cmd(owner_cog, name, i)

    async def _stats(self, i): await self._plain(i, self.cog.admin_cog, "Admin module", "stats", "system.stats")
    async def _pending(self, i): await self._plain(i, self.cog.admin_cog, "Admin module", "pending", "system.pending")
    async def _aidebug(self, i): await self._plain(i, self.cog.admin_cog, "Admin module", "aidebug", "system.aidebug")
    async def _submissions(self, i): await self._plain(i, self.cog.admin_cog, "Admin module", "submissions", "system.submissions")
    async def _revenue(self, i): await self._plain(i, self.cog.admin_cog, "Admin module", "revenue", "system.revenue")
    async def _envcheck(self, i): await self._plain(i, self.cog.admin_cog, "Admin module", "envcheck", "system.envcheck")

    async def _two_step(self, i: discord.Interaction, key: str, owner_cog, label: str, name: str, action: str, **audit_kw):
        if self._confirm != key:
            self._confirm = key
            self._build()
            await i.response.edit_message(view=self)
            return
        self._confirm = None
        if owner_cog is None:
            await _module_missing(i, label)
            return
        audit(i, action, **audit_kw)
        await call_cmd(owner_cog, name, i)   # defers/replies itself
        await self.soft_refresh(i)           # reset the Confirm button

    async def _hosting(self, i: discord.Interaction):
        await self._two_step(i, "hosting", self.cog.welcome, "Welcome module", "hostingchannel",
                             "system.hostingchannel", channel=i.channel_id)

    async def _export(self, i: discord.Interaction):
        await self._two_step(i, "export", self.cog.admin_cog, "Admin module", "exportusers", "system.exportusers")

    async def _back(self, i): await _home(self, i)
