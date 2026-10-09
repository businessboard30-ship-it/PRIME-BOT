"""
Server Owners Panel — welcome extras screen (Welcome -> Goodbye & roles).

Goodbye message (channel, on/off, custom text, test post) and the auto-roles for
new members and for bots. Same rules as every other panel screen: access is
re-checked on each click, only the opener can click, one audit row per change,
everything scoped by (guild_id, clone_id). Settings are stored in
discord_welcome_extras; the listeners live in cogs/welcome_extras.py.
"""

from __future__ import annotations

from typing import Callable

import discord

from modules import server_panel as sp
from discord_bot.cogs._views_server_panel import (
    ServerPanelView, WelcomeView, _btn, _onoff, clone_id_of, guard,
)
from discord_bot.cogs import welcome_extras as we


class GoodbyeMessageModal(discord.ui.Modal, title="Edit goodbye message"):
    template = discord.ui.TextInput(
        label="Message ({name} {member} {guild} {count})",
        style=discord.TextStyle.paragraph, max_length=we.GOODBYE_MAX)

    def __init__(self, current: str, opener_id: int):
        super().__init__()
        self.opener_id = opener_id
        self.template.default = current

    async def on_submit(self, interaction: discord.Interaction):
        if interaction.user.id != self.opener_id or not await guard(interaction):
            return
        text = str(self.template.value).strip()
        if not text:
            await interaction.response.send_message("The goodbye message can't be empty.", ephemeral=True)
            return
        await interaction.response.defer()
        await sp.set_welcome_extras(interaction.guild_id, clone_id_of(interaction), interaction.user.id,
                                    goodbye_message=text)
        view = await WelcomeExtrasView.create(interaction)
        await interaction.edit_original_response(view=view)


def _role_line(guild, role_id) -> str:
    if not role_id:
        return "not set"
    issue = we.role_issue(guild, role_id) if guild is not None else None
    return f"<@&{role_id}>" + (f" ⚠️ {issue}" if issue else "")


class WelcomeExtrasView(ServerPanelView):
    title = "👋 Goodbye & auto-roles"

    @classmethod
    async def load(cls, interaction):
        from database import db
        cid = clone_id_of(interaction)
        extras = await db.get_welcome_extras(interaction.guild_id, cid)
        vc = None
        try:
            vc = await db.get_verification_config(interaction.guild_id, cid)
        except Exception:
            pass
        return {"x": extras, "gate": bool(vc and vc.get("enabled"))}

    def _guild(self):
        return self.inspect.guild if self.inspect is not None else None

    def body(self):
        x = self.data.get("x", {})
        guild = self._guild()
        lines = [
            f"Goodbye message: {_onoff(x.get('goodbye_enabled'))}",
            f"Goodbye channel: {'<#%s>' % x['goodbye_channel_id'] if x.get('goodbye_channel_id') else 'not set'}",
            f"Message: `{(x.get('goodbye_message') or we.DEFAULT_GOODBYE)[:120]}`",
            f"New members get: {_role_line(guild, x.get('member_role_id'))}",
            f"New bots get: {_role_line(guild, x.get('bot_role_id'))}",
        ]
        if self.data.get("gate") and (x.get("member_role_id")):
            lines.append("-# ⚠️ Your verification gate is on — members get the auto-role immediately, "
                         "before they verify. Use the gate's own role instead if you want them locked out until then.")
        lines.append("-# Placeholders: {name} {member} {guild} {count} {count_ordinal} {date}. Bots leaving never post a goodbye.")
        return lines

    def controls(self):
        x = self.data.get("x", {})
        goodbye_ch = discord.ui.ChannelSelect(
            channel_types=[discord.ChannelType.text], placeholder="Goodbye channel",
            min_values=1, max_values=1)
        goodbye_ch.callback = self._pick_channel(goodbye_ch)
        member_role = discord.ui.RoleSelect(placeholder="Role for new members", min_values=1, max_values=1)
        member_role.callback = self._pick_role(member_role, "member_role_id")
        bot_role = discord.ui.RoleSelect(placeholder="Role for new bots", min_values=1, max_values=1)
        bot_role.callback = self._pick_role(bot_role, "bot_role_id")
        on = bool(x.get("goodbye_enabled"))
        out: list = [
            goodbye_ch, member_role, bot_role,
            _btn("Turn goodbye off" if on else "Turn goodbye on",
                 discord.ButtonStyle.danger if on else discord.ButtonStyle.success, self._toggle),
            _btn("Edit message", discord.ButtonStyle.primary, self._edit, "✏️"),
            _btn("Test goodbye", discord.ButtonStyle.primary, self._test, "🧪"),
        ]
        if x.get("member_role_id"):
            out.append(_btn("Clear member role", discord.ButtonStyle.secondary, self._clear("member_role_id")))
        if x.get("bot_role_id"):
            out.append(_btn("Clear bot role", discord.ButtonStyle.secondary, self._clear("bot_role_id")))
        out.append(self.back_button(WelcomeView))
        return out

    # callbacks ----------------------------------------------------------
    def _pick_channel(self, select: discord.ui.ChannelSelect) -> Callable:
        async def cb(interaction: discord.Interaction):
            ch = interaction.guild.get_channel(select.values[0].id)
            from discord_bot import perm_check
            problem = perm_check.channel_problem(ch, interaction.guild.me,
                                                 ("view_channel", "send_messages", "embed_links")) if ch else None
            if problem:
                await interaction.response.send_message(problem, ephemeral=True)
                return
            await interaction.response.defer()
            await sp.set_welcome_extras(interaction.guild_id, self.clone_id, interaction.user.id,
                                        goodbye_channel_id=select.values[0].id)
            await self.reload(interaction)
        return cb

    def _pick_role(self, select: discord.ui.RoleSelect, field: str) -> Callable:
        async def cb(interaction: discord.Interaction):
            role = interaction.guild.get_role(select.values[0].id)
            reason = sp.role_blocked_reason(interaction.guild, role)
            if reason:
                await interaction.response.send_message(reason, ephemeral=True)
                return
            await interaction.response.defer()
            await sp.set_welcome_extras(interaction.guild_id, self.clone_id, interaction.user.id,
                                        **{field: role.id})
            await self.reload(interaction)
        return cb

    def _clear(self, field: str) -> Callable:
        async def cb(interaction: discord.Interaction):
            await interaction.response.defer()
            await sp.set_welcome_extras(interaction.guild_id, self.clone_id, interaction.user.id, **{field: None})
            await self.reload(interaction)
        return cb

    async def _toggle(self, interaction: discord.Interaction):
        x = self.data.get("x", {})
        turning_on = not x.get("goodbye_enabled")
        if turning_on and not x.get("goodbye_channel_id"):
            await interaction.response.send_message("Pick a goodbye channel first.", ephemeral=True)
            return
        await interaction.response.defer()
        await sp.set_welcome_extras(interaction.guild_id, self.clone_id, interaction.user.id,
                                    goodbye_enabled=turning_on)
        await self.reload(interaction)

    async def _edit(self, interaction: discord.Interaction):
        cur = self.data.get("x", {}).get("goodbye_message") or we.DEFAULT_GOODBYE
        await interaction.response.send_modal(GoodbyeMessageModal(cur, interaction.user.id))

    async def _test(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        member = interaction.guild.get_member(interaction.user.id) or interaction.user
        ok, msg = await we.send_test_goodbye(interaction.client, interaction.guild, member)
        await interaction.followup.send(("✅ " if ok else "⚠️ ") + msg, ephemeral=True)
