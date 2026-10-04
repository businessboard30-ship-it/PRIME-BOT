"""
Server Owners Panel — Phase 10 screens: Features hub and Permission check.

Features hub -> Bump (status, pause/resume, opens the existing wizard), Custom role (on/off),
Autopost (on/off, channel, interval). Permission check is read-only. Same rules as every other
panel screen: thin shell over modules/server_panel_features.py (which calls the same db
functions the slash commands use), access re-checked on every click and modal submit, only the
opener can click, everything scoped by (guild_id, clone_id), every change audited.
"""

from __future__ import annotations

import logging
from typing import List

import discord

from modules import server_panel_features as spf
from discord_bot.cogs._views_server_panel import (
    ServerPanelView, HomeView, _btn, _chan, _onoff, clone_id_of,
)
from discord_bot.cogs._views_server_panel_p2 import _GuardedModal, _chan_select

logger = logging.getLogger(__name__)

P = discord.ButtonStyle.primary
S = discord.ButtonStyle.secondary
G = discord.ButtonStyle.success
D = discord.ButtonStyle.danger

DEFAULT_AUTOPOST_HOURS = 24


# ── features hub ─────────────────────────────────────────────────────────

class FeaturesView(ServerPanelView):
    title = "✨ More features"

    @classmethod
    async def load(cls, interaction):
        import asyncio
        gid, cid = interaction.guild_id, clone_id_of(interaction)
        bump, role, auto = await asyncio.gather(
            spf.bump_state(gid, cid), spf.custom_role_state(gid, cid), spf.autopost_state(gid, cid))
        return {"bump": bump, "role": role, "auto": auto}

    def body(self) -> List[str]:
        bump = self.data.get("bump", {})
        role = self.data.get("role", {})
        cfg = self.data.get("auto", {}).get("cfg", {})
        if spf.bump_is_set_up(bump):
            bump_line = f"Bump: {_chan(None, bump.get('bump_channel_id'))} · " + (
                "receiving bumps" if bump.get("receives_bumps", True) else "⏸️ paused")
        else:
            bump_line = "Bump: not set up"
        return [
            bump_line,
            f"Custom role perk: {_onoff(not role.get('disabled'))}",
            f"Autopost: {_onoff(cfg.get('enabled'))}" + (
                f" · {_chan(None, cfg.get('channel_id'))} every {cfg.get('interval_hours')}h"
                if cfg.get("enabled") else ""),
        ]

    def controls(self):
        return [
            _btn("Bump", P, self.nav_p6("BumpView"), "📣"),
            _btn("Custom role", P, self.nav_p6("CustomRoleView"), "🎨"),
            _btn("Autopost", P, self.nav_p6("AutopostView"), "📰"),
            _btn("Permission check", P, self.nav_p6("HealthView"), "🩺"),
            self.back_button(HomeView),
        ]


# ── bump ─────────────────────────────────────────────────────────────────

class BumpView(ServerPanelView):
    title = "📣 Bump network"

    @classmethod
    async def load(cls, interaction):
        return {"cfg": await spf.bump_state(interaction.guild_id, clone_id_of(interaction))}

    def body(self) -> List[str]:
        cfg = self.data.get("cfg", {})
        if not spf.bump_is_set_up(cfg):
            return ["Bump isn't set up yet. Open the setup wizard to pick a channel, language and frequency."]
        try:
            from discord_bot.cogs.bump_setup import INTENSITY_LABELS, LANGUAGE_OPTIONS
            freq = INTENSITY_LABELS.get(cfg.get("intensity_level"), str(cfg.get("intensity_level")))
            lang = dict(LANGUAGE_OPTIONS).get(cfg.get("language"), cfg.get("language"))
        except Exception:
            freq, lang = cfg.get("intensity_level"), cfg.get("language")
        lines = [
            f"Bump channel: {_chan(None, cfg.get('bump_channel_id'))}",
            f"Receiving bumps: {_onoff(cfg.get('receives_bumps', True))}",
            f"Language filter: **{lang}** · frequency: **{freq}** · NSFW: {_onoff(cfg.get('nsfw_opt_in'))}",
        ]
        if cfg.get("channel_recreate_declined"):
            lines.append("⚠️ The bump channel was deleted and recreating it was declined — "
                         "open the wizard and pick a channel to start again.")
        return lines

    def controls(self):
        cfg = self.data.get("cfg", {})
        out: list = [_btn("Open setup wizard", P, self._wizard, "🧙")]
        if spf.bump_is_set_up(cfg):
            on = bool(cfg.get("receives_bumps", True))
            blocked = (not on) and bool(cfg.get("channel_recreate_declined"))
            out.append(_btn("Pause bumps" if on else "Resume bumps", D if on else G, self._toggle,
                            disabled=blocked))
        out.append(self.back_button(FeaturesView))
        return out

    async def _wizard(self, interaction: discord.Interaction):
        """Same wizard as /bumpsetup."""
        from database import db
        from discord_bot.cogs.bump_setup import BumpWizardView
        await interaction.response.defer(ephemeral=True)
        current = await db.bump_get_guild_config(interaction.guild_id, clone_id=self.clone_id) or {}
        wizard = BumpWizardView(interaction.user.id, current)
        await interaction.followup.send(embed=wizard.build_embed(), view=wizard, ephemeral=True)

    async def _toggle(self, interaction: discord.Interaction):
        await interaction.response.defer()
        on = bool(self.data.get("cfg", {}).get("receives_bumps", True))
        ok = await spf.set_bump_receiving(interaction.guild_id, self.clone_id, interaction.user.id, not on)
        await self.reload(interaction)
        if not ok:
            await interaction.followup.send("Set bump up with the wizard first.", ephemeral=True)


# ── custom role ──────────────────────────────────────────────────────────

class CustomRoleView(ServerPanelView):
    title = "🎨 Custom role perk"

    @classmethod
    async def load(cls, interaction):
        return await spf.custom_role_state(interaction.guild_id, clone_id_of(interaction))

    def body(self) -> List[str]:
        panel = self.data.get("panel", {})
        return [
            f"Members can make their own role: {_onoff(not self.data.get('disabled'))}",
            f"Custom-role panel: {_chan(None, panel.get('panel_channel_id'))}",
            "-# Turning this off stops new custom roles; it doesn't delete roles people already made.",
        ]

    def controls(self):
        enabled = not self.data.get("disabled")
        return [
            _btn("Turn off" if enabled else "Turn on", D if enabled else G, self._toggle),
            self.back_button(FeaturesView),
        ]

    async def _toggle(self, interaction: discord.Interaction):
        await interaction.response.defer()
        await spf.set_custom_role_enabled(interaction.guild_id, self.clone_id, interaction.user.id,
                                          bool(self.data.get("disabled")))
        await self.reload(interaction)


# ── autopost ─────────────────────────────────────────────────────────────

def _autopost_problem(guild, channel_id):
    from discord_bot import perm_check
    ch = guild.get_channel(channel_id) if guild is not None else None
    if ch is None:
        return "I can't see that channel."
    return perm_check.channel_problem(ch, guild.me, spf.AUTOPOST_PERMS)


class AutopostIntervalModal(_GuardedModal):
    hours = discord.ui.TextInput(label="Hours between posts (1-720)", max_length=3)

    def __init__(self, current, opener_id: int):
        super().__init__("Autopost interval", opener_id)
        if current:
            self.hours.default = str(current)

    async def on_submit(self, interaction: discord.Interaction):
        if not await self.allowed(interaction):
            return
        value, err = spf.validate_interval(self.hours.value)
        if err:
            await interaction.response.send_message(err, ephemeral=True)
            return
        await interaction.response.defer()
        cfg = (await spf.autopost_state(interaction.guild_id, clone_id_of(interaction)))["cfg"]
        if not cfg.get("channel_id"):
            await interaction.followup.send("Pick a channel first.", ephemeral=True)
            return
        problem = _autopost_problem(interaction.guild, cfg["channel_id"])
        if problem:
            await interaction.followup.send(problem, ephemeral=True)
            return
        note = await spf.set_autopost(interaction.guild_id, clone_id_of(interaction), interaction.user.id,
                                      cfg["channel_id"], value)
        await interaction.edit_original_response(view=await AutopostView.create(interaction))
        if note:
            await interaction.followup.send(f"❌ {note}", ephemeral=True)


class AutopostView(ServerPanelView):
    title = "📰 Autopost"

    @classmethod
    async def load(cls, interaction):
        return await spf.autopost_state(interaction.guild_id, clone_id_of(interaction))

    def body(self) -> List[str]:
        cfg = self.data.get("cfg", {})
        lines = [
            "The bot posts a short feature tip in a channel of your choice at a set interval.",
            f"Autopost: {_onoff(cfg.get('enabled'))}",
            f"Channel: {_chan(None, cfg.get('channel_id'))} · every **{cfg.get('interval_hours', DEFAULT_AUTOPOST_HOURS)}h**",
        ]
        if cfg.get("last_posted_at"):
            lines.append(f"Last post: <t:{int(cfg['last_posted_at'].timestamp())}:R>")
        if not self.data.get("content_count"):
            lines.append("⚠️ There's no autopost content available yet, so it can't be turned on.")
        return lines

    def controls(self):
        cfg = self.data.get("cfg", {})
        out: list = [_chan_select("Pick the channel (turns autopost on)", self._channel)]
        if cfg.get("channel_id"):
            out.append(_btn("Change interval", S, self._interval, "⏱️"))
        if cfg.get("enabled"):
            out.append(_btn("Turn off", D, self._off))
        out.append(self.back_button(FeaturesView))
        return out

    async def _channel(self, interaction, channel_id):
        problem = _autopost_problem(interaction.guild, channel_id)
        if problem:
            await interaction.response.send_message(problem, ephemeral=True)
            return
        await interaction.response.defer()
        cfg = self.data.get("cfg", {})
        err = await spf.set_autopost(interaction.guild_id, self.clone_id, interaction.user.id, channel_id,
                                     cfg.get("interval_hours") or DEFAULT_AUTOPOST_HOURS)
        await self.reload(interaction)
        if err:
            await interaction.followup.send(f"❌ {err}", ephemeral=True)

    async def _interval(self, interaction: discord.Interaction):
        await interaction.response.send_modal(
            AutopostIntervalModal(self.data.get("cfg", {}).get("interval_hours"), interaction.user.id))

    async def _off(self, interaction: discord.Interaction):
        await interaction.response.defer()
        await spf.disable_autopost(interaction.guild_id, self.clone_id, interaction.user.id)
        await self.reload(interaction)


# ── permission check (read-only) ─────────────────────────────────────────

class HealthView(ServerPanelView):
    title = "🩺 Permission check"

    @classmethod
    async def load(cls, interaction):
        return {"report": await spf.health_report(interaction.guild, clone_id_of(interaction))}

    def body(self) -> List[str]:
        rep = self.data.get("report", {})
        if spf.health_ok(rep):
            return ["✅ I have every permission your configured features need."]
        lines: List[str] = []
        for feature, miss in rep.get("guild", []):
            lines.append(f"⚠️ **{feature}** — missing {', '.join(miss)} (server-wide)")
        for label, cid, miss in rep.get("channels", []):
            if miss:
                lines.append(f"⚠️ **{label}** <#{cid}> — missing {miss}")
        for label in rep.get("unreachable", []):
            lines.append(f"⚠️ **{label}** points at a channel I can't see (deleted or hidden)")
        lines.append("-# Fix these in Server Settings → Roles (my role) or in the channel's permissions, "
                     "then press Re-check.")
        return lines

    def controls(self):
        return [
            _btn("Re-check", S, self.nav_p6("HealthView"), "🔄"),
            self.back_button(FeaturesView),
        ]
