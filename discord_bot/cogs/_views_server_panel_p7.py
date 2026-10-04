"""
Server Owners Panel — Phase 11 screens: Quarantine & lockdown (opened from Moderation).

Hub -> Quarantine (role, quarantine a member, one-tap release, hide channels from the role, appeal
channel) and Lock & slow mode (lock/unlock a text channel, slow-mode presets).

Same rules as every other panel screen: thin shell over modules/server_panel_quarantine.py, access
re-checked on every click, only the opener can click, everything scoped by (guild_id, clone_id),
every change audited, bulk actions two-step.
"""

from __future__ import annotations

import logging
from typing import List

import discord

from modules import server_panel_quarantine as spq
from discord_bot.cogs._views_server_panel import (
    ServerPanelView, ModerationView, _btn, clone_id_of,
)
from discord_bot.cogs._views_server_panel_p4 import ConfirmView, _trim

logger = logging.getLogger(__name__)

P = discord.ButtonStyle.primary
S = discord.ButtonStyle.secondary
G = discord.ButtonStyle.success
D = discord.ButtonStyle.danger

SHOWN_MEMBERS = 10


# ── hub ──────────────────────────────────────────────────────────────────

class ResponseView(ServerPanelView):
    title = "🚨 Quarantine & lockdown"

    @classmethod
    async def load(cls, interaction):
        import asyncio
        g, cid = interaction.guild, clone_id_of(interaction)
        quar, locks = await asyncio.gather(spq.quarantine_state(g, cid), spq.lock_state(g, cid))
        return {"quar": quar, "locks": locks}

    def body(self) -> List[str]:
        q = self.data.get("quar", {})
        role = q.get("role")
        if q.get("role_id") and role is None:
            qline = "Quarantine: ⚠️ the role was deleted — pick a new one"
        elif role is None:
            qline = "Quarantine: not set up"
        else:
            qline = f"Quarantine: {role.mention} · **{len(q.get('members') or [])}** member(s) quarantined"
        locks = self.data.get("locks") or []
        return [qline, f"Locked channels: **{len(locks)}**",
                "-# Quarantine moves a member to one role and gives their roles back with one tap. "
                "Anti-raid (server-wide lockdown) is under Moderation → Anti-raid."]

    def controls(self):
        return [
            _btn("Quarantine", P, self.nav_p7("QuarantineView"), "🔒"),
            _btn("Lock & slow mode", P, self.nav_p7("LockdownView"), "🐢"),
            self.back_button(ModerationView),
        ]


# ── quarantine ───────────────────────────────────────────────────────────

class QuarantineView(ServerPanelView):
    title = "🔒 Quarantine"

    @classmethod
    async def load(cls, interaction):
        return {"quar": await spq.quarantine_state(interaction.guild, clone_id_of(interaction))}

    def body(self) -> List[str]:
        q = self.data.get("quar", {})
        role = q.get("role")
        if not q.get("role_id"):
            return ["No quarantine role yet. Pick one below — a plain role with no permissions works best. "
                    "Then use **Hide channels from this role** so a quarantined member can't see the server."]
        lines = [f"Quarantine role: {role.mention}" if role else "Quarantine role: ⚠️ deleted"]
        if q.get("problem"):
            lines.append(f"⚠️ {q['problem']}")
        if q.get("visible") is not None:
            lines.append(f"Channels this role can still see: **{q['visible']}**"
                         + ("" if q["visible"] == 0 else " — use **Hide channels** unless that's on purpose"))
        members = q.get("members") or []
        if members:
            lines.append(f"**Quarantined now ({len(members)})**")
            lines += [f"• <@{m['user_id']}>" + (f" — {_trim(m['reason'], 60)}" if m.get("reason") else "")
                      for m in members[:SHOWN_MEMBERS]]
            if len(members) > SHOWN_MEMBERS:
                lines.append(f"-# …and {len(members) - SHOWN_MEMBERS} more (use the release menu)")
        else:
            lines.append("Nobody is quarantined.")
        lines.append("-# Staff, bots, the owner and people ranked above me or you are never quarantined. "
                     "If someone leaves and rejoins while quarantined, they're put straight back.")
        return lines

    def controls(self):
        q = self.data.get("quar", {})
        role, ready = q.get("role"), bool(q.get("role")) and not q.get("problem")
        pick = discord.ui.RoleSelect(placeholder="Quarantine role", min_values=1, max_values=1)
        pick.callback = lambda i: self._pick_role(i, pick.values[0])
        out: list = [pick]
        if ready:
            who = discord.ui.UserSelect(placeholder="Quarantine a member…", min_values=1, max_values=1)
            who.callback = lambda i: self._quarantine(i, who.values[0])
            out.append(who)
        members = q.get("members") or []
        if members:
            rel = discord.ui.Select(
                placeholder="Release a member (one tap)…",
                options=[discord.SelectOption(label=self._name(m["user_id"]), value=str(m["user_id"]))
                         for m in members[:spq.LIST_LIMIT]])
            rel.callback = lambda i: self._release(i, int(rel.values[0]))
            out.append(rel)
        if role is not None:
            appeal = discord.ui.ChannelSelect(channel_types=[discord.ChannelType.text],
                                              placeholder="Appeal channel the role may use…",
                                              min_values=1, max_values=1)
            appeal.callback = lambda i: self._appeal(i, appeal.values[0].id)
            out.append(appeal)
            out.append(_btn("Hide channels from this role", P, self._hide, "🙈"))
        if members:
            out.append(_btn("Release everyone", D, self._release_all, "🔓"))
        out.append(self.back_button(ResponseView))
        return out

    def _name(self, user_id) -> str:
        # Names are looked up at build time from the cached member list; fall back to the ID.
        return _trim(self.data.get("names", {}).get(int(user_id), str(user_id)), 100)

    @classmethod
    async def create(cls, interaction):
        view = await super().create(interaction)
        guild = interaction.guild
        names = {}
        for m in view.data.get("quar", {}).get("members") or []:
            member = guild.get_member(int(m["user_id"])) if guild is not None else None
            names[int(m["user_id"])] = member.display_name if member is not None else f"(left) {m['user_id']}"
        view.data["names"] = names
        view._build()
        return view

    async def _pick_role(self, interaction, role):
        await interaction.response.defer()
        err = await spq.set_quarantine_role(interaction.guild, self.clone_id, interaction.user.id, role)
        await self.reload(interaction)
        if err:
            await interaction.followup.send(f"❌ {err}", ephemeral=True)

    async def _quarantine(self, interaction, picked):
        await interaction.response.defer()
        guild = interaction.guild
        member = guild.get_member(picked.id)
        actor = guild.get_member(interaction.user.id) or interaction.user
        ok, msg = await spq.quarantine_member(guild, self.clone_id, actor, member)
        await self.reload(interaction)
        await interaction.followup.send(msg if ok else f"❌ {msg}", ephemeral=True,
                                        allowed_mentions=discord.AllowedMentions.none())

    async def _release(self, interaction, user_id: int):
        await interaction.response.defer()
        ok, msg = await spq.release_member(interaction.guild, self.clone_id, interaction.user.id, user_id)
        await self.reload(interaction)
        await interaction.followup.send(msg if ok else f"❌ {msg}", ephemeral=True,
                                        allowed_mentions=discord.AllowedMentions.none())

    async def _release_all(self, interaction):
        guild, cid, uid = interaction.guild, self.clone_id, interaction.user.id

        async def confirm(i):
            return await spq.release_all(guild, cid, uid)

        await interaction.response.defer()
        view = ConfirmView(self.guild_id, self.clone_id, self.opener_id, {
            "prompt": "Release **everyone** on the quarantine list and give their roles back?",
            "confirm": confirm, "back": QuarantineView}, inspect=self.inspect)
        await interaction.edit_original_response(view=view)

    async def _hide(self, interaction):
        role = self.data.get("quar", {}).get("role")
        if role is None:
            await interaction.response.send_message("Pick a quarantine role first.", ephemeral=True)
            return
        guild, cid, uid = interaction.guild, self.clone_id, interaction.user.id

        async def confirm(i):
            current = guild.get_role(role.id)
            if current is None:
                return "❌ That role no longer exists."
            changed, failed = await spq.hide_channels(guild, cid, uid, current)
            return (f"🙈 Hid {changed} channel(s) from {current.mention}."
                    + (f" {failed} could not be changed (check my Manage Permissions)." if failed else ""))

        await interaction.response.defer()
        view = ConfirmView(self.guild_id, self.clone_id, self.opener_id, {
            "prompt": f"Deny **View Channel** for {role.mention} on every channel it can still see "
                      f"({self.data.get('quar', {}).get('visible', '?')})? Use the appeal-channel menu afterwards "
                      "to give it one place to talk.",
            "confirm": confirm, "back": QuarantineView}, inspect=self.inspect)
        await interaction.edit_original_response(view=view)

    async def _appeal(self, interaction, channel_id: int):
        role = self.data.get("quar", {}).get("role")
        channel = interaction.guild.get_channel(channel_id)
        if role is None or channel is None:
            await interaction.response.send_message("Pick a quarantine role first.", ephemeral=True)
            return
        await interaction.response.defer()
        err = await spq.allow_appeal_channel(interaction.guild, self.clone_id, interaction.user.id, role, channel)
        await self.reload(interaction)
        await interaction.followup.send(f"❌ {err}" if err else f"✅ {role.mention} can now see and write in {channel.mention}.",
                                        ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


# ── lock & slow mode ─────────────────────────────────────────────────────

class LockdownView(ServerPanelView):
    title = "🐢 Lock & slow mode"
    picked = None          # channel id chosen in this screen (not stored anywhere)

    @classmethod
    async def load(cls, interaction):
        return {"locks": await spq.lock_state(interaction.guild, clone_id_of(interaction))}

    def body(self) -> List[str]:
        locks = self.data.get("locks") or []
        lines = ["Pick a channel, then lock it or set a slow-mode delay. Locking only changes **@everyone's** "
                 "Send Messages there and puts it back exactly as it was when you unlock."]
        lines.append(f"Selected channel: <#{self.picked}>" if self.picked else "Selected channel: none yet")
        if locks:
            lines.append(f"**Locked now ({len(locks)})**")
            lines += [f"• <#{r['channel_id']}>" for r in locks[:15]]
            if len(locks) > 15:
                lines.append(f"-# …and {len(locks) - 15} more")
        else:
            lines.append("No channels are locked.")
        return lines

    def controls(self):
        locks = self.data.get("locks") or []
        pick = discord.ui.ChannelSelect(channel_types=[discord.ChannelType.text], placeholder="Pick a channel…",
                                        min_values=1, max_values=1)
        pick.callback = lambda i: self._pick(i, pick.values[0].id)
        slow = discord.ui.Select(
            placeholder="Slow mode for the selected channel…",
            options=[discord.SelectOption(label=label, value=str(secs)) for label, secs in spq.SLOWMODE_CHOICES])
        slow.callback = lambda i: self._slow(i, int(slow.values[0]))
        locked_ids = {int(r["channel_id"]) for r in locks}
        out: list = [pick, slow]
        if self.picked and self.picked in locked_ids:
            out.append(_btn("Unlock selected", G, self._unlock_selected, "🔓"))
        else:
            out.append(_btn("Lock selected", D, self._lock_selected, "🔒", disabled=not self.picked))
        if locks:
            out.append(_btn("Unlock all", G, self._unlock_all, "🔓"))
        out.append(self.back_button(ResponseView))
        return out

    async def _pick(self, interaction, channel_id: int):
        self.picked = channel_id
        await interaction.response.defer()
        self._build()
        await interaction.edit_original_response(view=self)

    def _channel(self, interaction):
        return interaction.guild.get_channel(self.picked) if self.picked else None

    async def _say(self, interaction, ok: bool, msg: str):
        await self.reload_keep(interaction)
        await interaction.followup.send(msg if ok else f"❌ {msg}", ephemeral=True,
                                        allowed_mentions=discord.AllowedMentions.none())

    async def reload_keep(self, interaction):
        """Refresh the locked list but keep the selected channel."""
        self.data["locks"] = await spq.lock_state(interaction.guild, self.clone_id)
        self._build()
        await interaction.edit_original_response(view=self)

    async def _lock_selected(self, interaction):
        channel = self._channel(interaction)
        if channel is None:
            await interaction.response.send_message("Pick a channel first.", ephemeral=True)
            return
        await interaction.response.defer()
        ok, msg = await spq.lock_channel(interaction.guild, self.clone_id, interaction.user.id, channel)
        await self._say(interaction, ok, msg)

    async def _unlock_selected(self, interaction):
        if not self.picked:
            await interaction.response.send_message("Pick a channel first.", ephemeral=True)
            return
        await interaction.response.defer()
        ok, msg = await spq.unlock_channel(interaction.guild, self.clone_id, interaction.user.id, self.picked)
        await self._say(interaction, ok, msg)

    async def _unlock_all(self, interaction):
        guild, cid, uid = interaction.guild, self.clone_id, interaction.user.id

        async def confirm(i):
            return await spq.unlock_all(guild, cid, uid)

        await interaction.response.defer()
        view = ConfirmView(self.guild_id, self.clone_id, self.opener_id, {
            "prompt": "Unlock **every** channel the panel locked and put @everyone's permissions back?",
            "confirm": confirm, "back": LockdownView}, inspect=self.inspect)
        await interaction.edit_original_response(view=view)

    async def _slow(self, interaction, seconds: int):
        channel = self._channel(interaction)
        if channel is None:
            await interaction.response.send_message("Pick a channel first.", ephemeral=True)
            return
        await interaction.response.defer()
        ok, msg = await spq.set_slowmode(interaction.guild, self.clone_id, interaction.user.id, channel, seconds)
        await self._say(interaction, ok, msg)
