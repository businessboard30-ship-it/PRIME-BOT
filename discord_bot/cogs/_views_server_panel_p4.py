"""
Server Owners Panel — Phase 8 screens: the Tools hub.

Tools hub -> Invites, Scheduled messages, Link buttons, plus a button that opens the
existing self-roles wizard. Same rules as every other panel screen: thin shell over
modules/server_panel_tools.py (which calls the same db functions the slash commands
use), access re-checked on every click and modal submit, only the opener can click,
everything scoped by (guild_id, clone_id), every change audited, removals two-step.
"""

from __future__ import annotations

import logging
from typing import Awaitable, Callable, List, Optional

import discord

from modules import server_panel_tools as spt
from discord_bot.cogs._views_server_panel import (
    ServerPanelView, HomeView, _btn, _chan, _onoff, clone_id_of, read_only_ok,
)
from discord_bot.cogs._views_server_panel_p2 import _GuardedModal, _chan_select

logger = logging.getLogger(__name__)

P = discord.ButtonStyle.primary
S = discord.ButtonStyle.secondary
G = discord.ButtonStyle.success
D = discord.ButtonStyle.danger

POST_PERMS = ("view_channel", "send_messages")


def _channel_problem(guild, channel_id) -> Optional[str]:
    """Reason the bot can't post in this channel, or None."""
    ch = guild.get_channel(channel_id) if guild is not None else None
    if ch is None:
        return "I can't see that channel."
    from discord_bot import perm_check
    return perm_check.channel_problem(ch, guild.me, POST_PERMS)


def _trim(text: str, n: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 1] + "…"


# ── generic two-step confirm ─────────────────────────────────────────────

class ConfirmView(ServerPanelView):
    """Two-step removal. data: prompt (str), confirm (async fn(interaction) -> str), back (view class)."""

    title = "Are you sure?"

    def body(self) -> List[str]:
        return [self.data.get("prompt", "This can't be undone.")]

    def controls(self):
        return [
            _btn("Yes, do it", D, self._yes),
            _btn("Cancel", S, self._no),
        ]

    async def _yes(self, interaction: discord.Interaction):
        await interaction.response.defer()
        note = await self.data["confirm"](interaction)
        view = await self.data["back"].create(self._ctx(interaction))
        await interaction.edit_original_response(view=view)
        if note:
            await interaction.followup.send(note, ephemeral=True)

    @read_only_ok
    async def _no(self, interaction: discord.Interaction):
        await interaction.response.defer()
        view = await self.data["back"].create(self._ctx(interaction))
        await interaction.edit_original_response(view=view)


# ── tools hub ────────────────────────────────────────────────────────────

class ToolsView(ServerPanelView):
    title = "🧰 Server tools"

    @classmethod
    async def load(cls, interaction):
        import asyncio
        gid, cid = interaction.guild_id, clone_id_of(interaction)
        inv, jobs, links = await asyncio.gather(
            spt.invites_state(gid, cid), spt.list_schedules(gid, cid), spt.list_links(gid))
        return {"inv": inv, "jobs": jobs, "links": links}

    def body(self) -> List[str]:
        cfg = self.data.get("inv", {}).get("cfg", {})
        jobs = self.data.get("jobs", [])
        live = sum(1 for j in jobs if j.get("enabled"))
        return [
            f"Invite tracker: {_onoff(cfg.get('enabled', True))} · announcements in {_chan(None, cfg.get('channel_id'))}",
            f"Scheduled messages: **{live}** active" + (f" ({len(jobs) - live} finished)" if len(jobs) > live else ""),
            f"Link buttons: **{len(self.data.get('links', []))}/{spt.MAX_LINK_BUTTONS}**",
            "Self-roles: members pick their own roles from a panel you post.",
        ]

    def controls(self):
        return [
            _btn("Invites", P, self.nav(InvitesView), "🔗"),
            _btn("Scheduled messages", P, self.nav(ScheduleView), "⏰"),
            _btn("Link buttons", P, self.nav(LinkButtonsView), "🔘"),
            _btn("Self-roles wizard", P, self._self_roles, "🎭"),
            self.back_button(HomeView),
        ]

    async def _self_roles(self, interaction: discord.Interaction):
        """The same wizard as /role setup (needs Manage Roles for you and for me)."""
        if not getattr(interaction.permissions, "manage_roles", False):
            await interaction.response.send_message(
                "You need the **Manage Roles** permission to use the self-roles wizard.", ephemeral=True)
            return
        me = interaction.guild.me
        if me is None or not me.guild_permissions.manage_roles:
            await interaction.response.send_message(
                "I need the **Manage Roles** permission myself before I can create or assign roles here.",
                ephemeral=True)
            return
        from discord_bot.cogs.role_setup import RoleSetupWizard
        wizard = RoleSetupWizard(interaction.user.id, guild=interaction.guild)
        await interaction.response.send_message(embed=wizard.build_embed(), view=wizard, ephemeral=True)


# ── invites ──────────────────────────────────────────────────────────────

class InvitesView(ServerPanelView):
    title = "🔗 Invite tracker"

    @classmethod
    async def load(cls, interaction):
        return await spt.invites_state(interaction.guild_id, clone_id_of(interaction))

    @staticmethod
    def _lb_line(cfg: dict) -> str:
        v = cfg.get("leaderboard_autopost_channel_id")
        if v == -1:
            return "off"
        return _chan(None, v) if v else "same as the announcement channel"

    def body(self) -> List[str]:
        cfg, top = self.data.get("cfg", {}), self.data.get("top", [])
        lines = [
            f"Tracking and announcements: {_onoff(cfg.get('enabled', True))}",
            f"Announcement channel: {_chan(None, cfg.get('channel_id'))}",
            f"Daily leaderboard post: {self._lb_line(cfg)}",
        ]
        if top:
            lines.append("**Top inviters** (still in the server / total joins)")
            lines += [f"{i}. <@{uid}> — **{net}** / {joins}" for i, (uid, joins, net) in enumerate(top, 1)]
        else:
            lines.append("No tracked invites yet — they appear as people join.")
        lines.append("-# The bot needs **Manage Server** to see invite use counts.")
        return lines

    def controls(self):
        cfg = self.data.get("cfg", {})
        on = bool(cfg.get("enabled", True))
        out: list = [
            _chan_select("Announcement channel", self._announce),
            _chan_select("Leaderboard channel", self._leaderboard),
            _btn("Turn off" if on else "Turn on", D if on else G, self._toggle),
        ]
        if cfg.get("leaderboard_autopost_channel_id") != -1:
            out.append(_btn("Leaderboard off", S, self._leaderboard_off))
        else:
            out.append(_btn("Leaderboard: use default", S, self._leaderboard_default))
        out.append(self.back_button(ToolsView))
        return out

    async def _announce(self, interaction, channel_id):
        problem = _channel_problem(interaction.guild, channel_id)
        if problem:
            await interaction.response.send_message(problem, ephemeral=True)
            return
        await interaction.response.defer()
        await spt.set_invites(interaction.guild_id, self.clone_id, interaction.user.id,
                              channel_id=channel_id, channel_auto_created=False)
        await self.reload(interaction)

    async def _leaderboard(self, interaction, channel_id):
        problem = _channel_problem(interaction.guild, channel_id)
        if problem:
            await interaction.response.send_message(problem, ephemeral=True)
            return
        await interaction.response.defer()
        await spt.set_invite_leaderboard_channel(interaction.guild_id, self.clone_id, interaction.user.id, channel_id)
        await self.reload(interaction)

    async def _leaderboard_off(self, interaction):
        await interaction.response.defer()
        await spt.set_invite_leaderboard_channel(interaction.guild_id, self.clone_id, interaction.user.id, -1)
        await self.reload(interaction)

    async def _leaderboard_default(self, interaction):
        await interaction.response.defer()
        await spt.set_invite_leaderboard_channel(interaction.guild_id, self.clone_id, interaction.user.id, None)
        await self.reload(interaction)

    async def _toggle(self, interaction):
        await interaction.response.defer()
        cfg = self.data.get("cfg", {})
        await spt.set_invites(interaction.guild_id, self.clone_id, interaction.user.id,
                              enabled=not bool(cfg.get("enabled", True)))
        await self.reload(interaction)


# ── scheduled messages ───────────────────────────────────────────────────

_KIND_LABELS = {
    "once": ("Post once", "Delay (e.g. 2h, 30m, 1d)", "2h"),
    "recurring": ("Repeat", "Every (e.g. 1d, 12h, 30m)", "1d"),
    "daily": ("Post daily", "UTC time (24h, e.g. 09:00)", "09:00"),
}


class ScheduleModal(_GuardedModal):
    when = discord.ui.TextInput(label="When", max_length=40)
    text = discord.ui.TextInput(label="Message", style=discord.TextStyle.paragraph, max_length=spt.MAX_TEXT)

    def __init__(self, kind: str, channel_id: int, opener_id: int):
        title, label, placeholder = _KIND_LABELS[kind]
        super().__init__(title, opener_id)
        self.kind, self.channel_id = kind, channel_id
        self.when.label, self.when.placeholder = label, placeholder

    async def on_submit(self, interaction: discord.Interaction):
        if not await self.allowed(interaction):
            return
        problem = _channel_problem(interaction.guild, self.channel_id)
        if problem:
            await interaction.response.send_message(problem, ephemeral=True)
            return
        await interaction.response.defer()
        job, err = await spt.add_schedule(
            interaction.guild_id, clone_id_of(interaction), interaction.user.id,
            self.channel_id, self.kind, str(self.when.value), str(self.text.value))
        await interaction.edit_original_response(view=await ScheduleView.create(interaction))
        if err:
            await interaction.followup.send(f"❌ {err}", ephemeral=True)
        else:
            await interaction.followup.send(
                f"✅ Scheduled `#{job['id']}` in <#{self.channel_id}> — first post "
                f"<t:{int(job['next_run_at'].timestamp())}:R>.", ephemeral=True)


class ScheduleView(ServerPanelView):
    title = "⏰ Scheduled messages"

    def __init__(self, *a, **kw):
        self._channel_id: Optional[int] = None  # picked on screen, used by the three add buttons
        super().__init__(*a, **kw)

    @classmethod
    async def load(cls, interaction):
        return {"jobs": await spt.list_schedules(interaction.guild_id, clone_id_of(interaction))}

    def body(self) -> List[str]:
        jobs = self.data.get("jobs", [])
        lines = [f"Posting to: {_chan(None, self._channel_id) if self._channel_id else '**pick a channel below first**'}"]
        if not jobs:
            lines.append("Nothing scheduled yet.")
        for j in jobs[:10]:
            state = "🟢" if j.get("enabled") else "⚪ done"
            lines.append(f"{state} `#{j['id']}` <#{j['channel_id']}> · {spt.describe_interval(j.get('interval_seconds'))} "
                         f"· next <t:{int(j['next_run_at'].timestamp())}:R>\n> {_trim(j['content'], 60)}")
        if len(jobs) > 10:
            lines.append(f"-# …and {len(jobs) - 10} more (cancel from the menu below).")
        return lines

    def controls(self):
        jobs = self.data.get("jobs", [])
        out: list = [_chan_select("Channel to post in", self._pick_channel)]
        if jobs:
            sel = discord.ui.Select(
                placeholder="Cancel a scheduled message…",
                options=[discord.SelectOption(label=f"#{j['id']} · {_trim(j['content'], 80)}"[:100], value=str(j["id"]),
                                              description=f"{spt.describe_interval(j.get('interval_seconds'))}"[:100])
                         for j in jobs[:spt.MAX_SCHEDULES]])
            sel.callback = lambda i: self._cancel(i, int(sel.values[0]))
            out.append(sel)
        out += [
            _btn("Post once", P, self._add("once"), "⏱️"),
            _btn("Repeat", P, self._add("recurring"), "🔁"),
            _btn("Post daily", P, self._add("daily"), "📅"),
            self.back_button(ToolsView),
        ]
        return out

    async def _pick_channel(self, interaction, channel_id):
        problem = _channel_problem(interaction.guild, channel_id)
        if problem:
            await interaction.response.send_message(problem, ephemeral=True)
            return
        self._channel_id = channel_id
        await interaction.response.defer()
        self._build()
        await interaction.edit_original_response(view=self)

    def _add(self, kind: str) -> Callable:
        async def cb(interaction: discord.Interaction):
            if not self._channel_id:
                await interaction.response.send_message("Pick the channel to post in first.", ephemeral=True)
                return
            await interaction.response.send_modal(ScheduleModal(kind, self._channel_id, interaction.user.id))
        return cb

    async def _cancel(self, interaction, schedule_id: int):
        job = next((j for j in self.data.get("jobs", []) if j["id"] == schedule_id), None)
        if job is None:
            await interaction.response.send_message("That schedule no longer exists.", ephemeral=True)
            return
        gid, cid, uid = interaction.guild_id, self.clone_id, interaction.user.id

        async def confirm(i):
            ok = await spt.cancel_schedule(gid, cid, uid, schedule_id)
            return None if ok else f"No schedule `#{schedule_id}` found here."

        await interaction.response.defer()
        view = ConfirmView(self.guild_id, self.clone_id, self.opener_id, {
            "prompt": f"Cancel `#{schedule_id}`?\n> {_trim(job['content'], 100)}",
            "confirm": confirm, "back": ScheduleView}, inspect=self.inspect)
        await interaction.edit_original_response(view=view)


# ── link buttons ─────────────────────────────────────────────────────────

class LinkButtonModal(_GuardedModal):
    label = discord.ui.TextInput(label="Button text", max_length=80, placeholder="Join our channel")
    url = discord.ui.TextInput(label="Link (https://…)", max_length=512)

    def __init__(self, opener_id: int):
        super().__init__("Add link button", opener_id)

    async def on_submit(self, interaction: discord.Interaction):
        if not await self.allowed(interaction):
            return
        await interaction.response.defer()
        err = await spt.add_link(interaction.guild_id, clone_id_of(interaction), interaction.user.id,
                                 str(self.label.value), str(self.url.value))
        await interaction.edit_original_response(view=await LinkButtonsView.create(interaction))
        if err:
            await interaction.followup.send(f"❌ {err}", ephemeral=True)


class LinkButtonsView(ServerPanelView):
    title = "🔘 Link buttons"

    @classmethod
    async def load(cls, interaction):
        return {"links": await spt.list_links(interaction.guild_id)}

    def body(self) -> List[str]:
        links = self.data.get("links", [])
        lines = [f"**{len(links)}/{spt.MAX_LINK_BUTTONS}** buttons"]
        lines += [f"{i}. **{_trim(b['label'], 60)}** → {_trim(b['url'], 80)}" for i, b in enumerate(links, 1)] \
            or ["No link buttons yet."]
        lines.append("-# Post them as a row with `/linkbutton panel`. Adding the same text again updates its link.")
        return lines

    def controls(self):
        links = self.data.get("links", [])
        out: list = []
        if links:
            sel = discord.ui.Select(
                placeholder="Remove a button…",
                options=[discord.SelectOption(label=_trim(b["label"], 100), value=str(i))
                         for i, b in enumerate(links[:spt.MAX_LINK_BUTTONS])])
            sel.callback = lambda i: self._remove(i, int(sel.values[0]))
            out.append(sel)
        out.append(_btn("Add button", G, self._add, "➕", disabled=len(links) >= spt.MAX_LINK_BUTTONS))
        out.append(self.back_button(ToolsView))
        return out

    async def _add(self, interaction: discord.Interaction):
        await interaction.response.send_modal(LinkButtonModal(interaction.user.id))

    async def _remove(self, interaction, index: int):
        links = self.data.get("links", [])
        if not 0 <= index < len(links):
            await interaction.response.send_message("That button no longer exists.", ephemeral=True)
            return
        label = links[index]["label"]
        gid, cid, uid = interaction.guild_id, self.clone_id, interaction.user.id

        async def confirm(i):
            ok = await spt.remove_link(gid, cid, uid, label)
            return None if ok else "That button was already gone."

        await interaction.response.defer()
        view = ConfirmView(self.guild_id, self.clone_id, self.opener_id, {
            "prompt": f"Remove the **{_trim(label, 60)}** button?",
            "confirm": confirm, "back": LinkButtonsView}, inspect=self.inspect)
        await interaction.edit_original_response(view=view)
