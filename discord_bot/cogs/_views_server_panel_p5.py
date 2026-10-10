"""
Server Owners Panel — Phase 9 screens: Join gate & Scam Shield (opened from Moderation).

Same rules as every other panel screen: thin shell over modules/server_panel_protection.py,
access re-checked on every click and modal submit, only the opener can click, everything scoped by
(guild_id, clone_id), every change audited, removals two-step.
"""

from __future__ import annotations

import logging
from typing import List

import discord

from modules import join_gate as jg
from modules import server_panel_protection as spp
from discord_bot.cogs._views_server_panel import (
    ServerPanelView, ModerationView, _btn, _chan, _onoff, clone_id_of,
)
from discord_bot.cogs._views_server_panel_p2 import _GuardedModal
from discord_bot.cogs._views_server_panel_p4 import ConfirmView, _trim

logger = logging.getLogger(__name__)

P = discord.ButtonStyle.primary
S = discord.ButtonStyle.secondary
G = discord.ButtonStyle.success
D = discord.ButtonStyle.danger


# ── protection hub ───────────────────────────────────────────────────────

class ProtectionView(ServerPanelView):
    title = "🚪 Join gate & Scam Shield"

    @classmethod
    async def load(cls, interaction):
        import asyncio
        gid, cid = interaction.guild_id, clone_id_of(interaction)
        gate, scam = await asyncio.gather(spp.join_gate_state(gid, cid), spp.scam_state(gid, cid))
        return {"gate": gate, "scam": scam}

    def body(self) -> List[str]:
        gate, scam = self.data.get("gate", {}), self.data.get("scam", {})
        cfg = scam.get("cfg", {})
        return [
            f"Join gate: {_onoff(gate.get('enabled'))} · accounts under **{gate.get('min_age_days', 7)}** day(s)"
            + (" · default avatars" if gate.get("block_default_avatar") else "")
            + f" · {jg.ACTIONS.get(gate.get('action', 'alert'), 'Alert staff only')}",
            f"Caught by the join gate: **{gate.get('blocked_count', 0)}**",
            f"Scam Shield here: {_onoff(cfg.get('enabled', True))} · **{scam.get('caught', 0)}** scam message(s) caught"
            + (" · ⏸️ switched off for every server by the bot owner" if not scam.get("global_on", True) else ""),
            f"Allowed domains: **{len(cfg.get('allowed_domains') or [])}**",
        ]

    def controls(self):
        return [
            _btn("Join gate", P, self.nav_p5("JoinGateView"), "🚪"),
            _btn("Scam Shield", P, self.nav_p5("ScamShieldView"), "🛡️"),
            self.back_button(ModerationView),
        ]


# ── join gate ────────────────────────────────────────────────────────────

class MinAgeModal(_GuardedModal):
    days = discord.ui.TextInput(label="Minimum account age in days (0-365)", max_length=3)

    def __init__(self, current, opener_id: int):
        super().__init__("Minimum account age", opener_id)
        if current is not None:
            self.days.default = str(current)

    async def on_submit(self, interaction: discord.Interaction):
        if not await self.allowed(interaction):
            return
        days = jg.validate_min_age(self.days.value)
        if days is None:
            await interaction.response.send_message(
                f"Minimum account age must be a whole number of days from 0 to {jg.MAX_AGE_DAYS}.", ephemeral=True)
            return
        await interaction.response.defer()
        await spp.set_join_gate(interaction.guild_id, clone_id_of(interaction), interaction.user.id,
                                min_age_days=days)
        await interaction.edit_original_response(view=await JoinGateView.create(interaction))


class JoinGateView(ServerPanelView):
    title = "🚪 Join gate"

    @classmethod
    async def load(cls, interaction):
        import asyncio
        gid, cid = interaction.guild_id, clone_id_of(interaction)
        gate = await spp.join_gate_state(gid, cid)
        log = None
        try:
            from database import db
            log = (await db.get_automod_config(gid, cid)).get("log_channel_id")
        except Exception:
            logger.debug("[server-panel] join gate log read failed", exc_info=True)
        return {"gate": gate, "log": log}

    def body(self) -> List[str]:
        g = self.data.get("gate", {})
        lines = [
            f"Status: {_onoff(g.get('enabled'))}",
            f"Minimum account age: **{g.get('min_age_days', 7)}** day(s)",
            f"Block default avatars: {_onoff(g.get('block_default_avatar'))}",
            f"When someone is caught: **{jg.ACTIONS.get(g.get('action', 'alert'), 'Alert staff only')}**",
            f"Alerts go to: {_chan(None, self.data.get('log'))} (the mod-log channel)",
            f"Caught so far: **{g.get('blocked_count', 0)}**",
        ]
        if not self.data.get("log"):
            lines.append("-# ⚠️ No mod-log channel is set, so nobody will be alerted — set one under Moderation.")
        if g.get("action") == "kick":
            lines.append("-# Kicked people can rejoin later once their account is old enough. Bots are never checked.")
        return lines

    def controls(self):
        g = self.data.get("gate", {})
        act = discord.ui.Select(
            placeholder="What to do with a caught member",
            options=[discord.SelectOption(label=label, value=key, default=key == g.get("action", "alert"))
                     for key, label in jg.ACTIONS.items()])
        act.callback = lambda i: self._action(i, act.values[0])
        days = discord.ui.Select(
            placeholder="Minimum account age",
            options=[discord.SelectOption(label=f"{d} day{'s' if d != 1 else ''}", value=str(d),
                                          default=d == g.get("min_age_days", 7)) for d in jg.MIN_AGE_CHOICES])
        days.callback = lambda i: self._days(i, int(days.values[0]))
        on = bool(g.get("enabled"))
        av = bool(g.get("block_default_avatar"))
        return [
            act, days,
            _btn("Turn off" if on else "Turn on", D if on else G, self._toggle),
            _btn("Default avatars: on" if av else "Default avatars: off", G if av else S, self._avatar),
            _btn("Custom age", P, self._custom_age, "✏️"),
            self.back_button(ProtectionView),
        ]

    async def _write(self, interaction, **fields):
        await interaction.response.defer()
        err = await spp.set_join_gate(interaction.guild_id, self.clone_id, interaction.user.id, **fields)
        await self.reload(interaction)
        if err:
            await interaction.followup.send(f"❌ {err}", ephemeral=True)

    async def _toggle(self, interaction):
        g = self.data.get("gate", {})
        turning_on = not g.get("enabled")
        if turning_on and not self.data.get("log"):
            await interaction.response.send_message(
                "Set a **mod-log channel** first (Moderation screen) — a catch with nowhere to report is useless.",
                ephemeral=True)
            return
        if turning_on and g.get("action") == "kick":
            me = interaction.guild.me
            if me is None or not me.guild_permissions.kick_members:
                await interaction.response.send_message(
                    "I need the **Kick Members** permission before the gate can kick anyone.", ephemeral=True)
                return
        await self._write(interaction, enabled=turning_on)

    async def _action(self, interaction, action):
        if action == "quarantine":
            from modules import server_panel_quarantine as spq
            st = await spq.quarantine_state(interaction.guild, self.clone_id)
            if not st.get("role") or st.get("problem"):
                await interaction.response.send_message(
                    "Set up a working **quarantine role** first (Moderation → Quarantine & lockdown)"
                    + (f" — {st['problem']}" if st.get("problem") else "."), ephemeral=True)
                return
        if action == "kick":
            me = interaction.guild.me
            if me is None or not me.guild_permissions.kick_members:
                await interaction.response.send_message(
                    "I need the **Kick Members** permission before the gate can kick anyone.", ephemeral=True)
                return
        await self._write(interaction, action=action)

    async def _days(self, interaction, days):
        await self._write(interaction, min_age_days=days)

    async def _avatar(self, interaction):
        await self._write(interaction, block_default_avatar=not self.data.get("gate", {}).get("block_default_avatar"))

    async def _custom_age(self, interaction):
        await interaction.response.send_modal(MinAgeModal(self.data.get("gate", {}).get("min_age_days"),
                                                          interaction.user.id))


# ── scam shield (this server) ────────────────────────────────────────────

class AllowDomainsModal(_GuardedModal):
    domains = discord.ui.TextInput(label="Domains to allow (one per line)", style=discord.TextStyle.paragraph,
                                   max_length=1000, placeholder="example.com\nmy-site.org")

    def __init__(self, opener_id: int):
        super().__init__("Allow domains", opener_id)

    async def on_submit(self, interaction: discord.Interaction):
        if not await self.allowed(interaction):
            return
        from database import db
        cid = clone_id_of(interaction)
        await interaction.response.defer()
        current = (await db.get_scam_shield_guild(interaction.guild_id, clone_id=cid)).get("allowed_domains") or []
        added, rejected, final = spp.clean_domains(str(self.domains.value), current)
        if added:
            await spp.set_allowed_domains(interaction.guild_id, cid, interaction.user.id, final)
        await interaction.edit_original_response(view=await ScamShieldView.create(interaction))
        note = []
        if added:
            note.append("✅ Allowed: " + ", ".join(f"`{d}`" for d in added))
        if rejected:
            note.append("⚠️ Skipped (not a valid domain, or the 50-domain limit): " + ", ".join(f"`{r}`" for r in rejected))
        if note:
            await interaction.followup.send("\n".join(note), ephemeral=True)


class ScamShieldView(ServerPanelView):
    title = "🛡️ Scam Shield"

    @classmethod
    async def load(cls, interaction):
        return await spp.scam_state(interaction.guild_id, clone_id_of(interaction))

    def body(self) -> List[str]:
        cfg = self.data.get("cfg", {})
        allowed = cfg.get("allowed_domains") or []
        lines = [
            f"Status in this server: {_onoff(cfg.get('enabled', True))}",
            f"Caught here so far: **{self.data.get('caught', 0)}**",
            "Scam Shield deletes known scam messages (casino/giveaway bait, fake payout screenshots) "
            "and posts a flag in your mod-log channel. Staff are never checked.",
        ]
        if not self.data.get("global_on", True):
            lines.append("-# ⏸️ The bot owner has switched Scam Shield off for every server for now.")
        lines += self._ai_lines()
        if allowed:
            lines.append("**Allowed domains** (never treated as scams here)")
            lines += [f"• `{d}`" for d in allowed[:15]]
            if len(allowed) > 15:
                lines.append(f"-# …and {len(allowed) - 15} more")
        else:
            lines.append("No allowed domains. Add one if a legitimate link keeps getting deleted.")
        return lines

    def _ai_lines(self) -> List[str]:
        ai = self.data.get("ai") or {}
        if not ai.get("on"):
            return ["🤖 **AI image check:** not available right now. Known scam pictures are still caught."]
        cap, used = int(ai.get("cap", 0)), int(ai.get("used", 0))
        tier = "⭐ Premium server" if ai.get("premium") else "Free server"
        lines = [
            "🤖 **AI image check:** new scam pictures that aren't in the known list (fake casino withdrawals, "
            "fake giveaways, phishing screens) are read by AI and deleted if they are scams. "
            f"{tier}: up to **{cap}** AI checks per day.",
            f"Used today: **{used}/{cap}**. Resets daily at 00:00 UTC. When the limit is used up, only the "
            "known-scam rules keep working until it resets.",
        ]
        if not ai.get("premium"):
            lines.append("-# ⭐ Premium servers get a much bigger daily AI limit.")
        return lines

    def controls(self):
        cfg = self.data.get("cfg", {})
        allowed = cfg.get("allowed_domains") or []
        on = bool(cfg.get("enabled", True))
        out: list = []
        if allowed:
            sel = discord.ui.Select(
                placeholder="Remove an allowed domain…",
                options=[discord.SelectOption(label=_trim(d, 100), value=d) for d in allowed[:25]])
            sel.callback = lambda i: self._remove(i, sel.values[0])
            out.append(sel)
        out += [
            _btn("Turn off here" if on else "Turn on here", D if on else G, self._toggle),
            _btn("Allow domains", P, self._allow, "➕"),
            self.back_button(ProtectionView),
        ]
        return out

    async def _toggle(self, interaction):
        await interaction.response.defer()
        await spp.set_scam_enabled(interaction.guild_id, self.clone_id, interaction.user.id,
                                   not bool(self.data.get("cfg", {}).get("enabled", True)))
        await self.reload(interaction)

    async def _allow(self, interaction):
        await interaction.response.send_modal(AllowDomainsModal(interaction.user.id))

    async def _remove(self, interaction, domain: str):
        allowed = list(self.data.get("cfg", {}).get("allowed_domains") or [])
        if domain not in allowed:
            await interaction.response.send_message("That domain is already gone.", ephemeral=True)
            return
        gid, cid, uid = interaction.guild_id, self.clone_id, interaction.user.id

        async def confirm(i):
            from database import db
            current = list((await db.get_scam_shield_guild(gid, clone_id=cid)).get("allowed_domains") or [])
            await spp.set_allowed_domains(gid, cid, uid, [d for d in current if d != domain])
            return None

        await interaction.response.defer()
        view = ConfirmView(self.guild_id, self.clone_id, self.opener_id, {
            "prompt": f"Stop allowing `{domain}`? Links to it can be deleted by Scam Shield again.",
            "confirm": confirm, "back": ScamShieldView}, inspect=self.inspect)
        await interaction.edit_original_response(view=view)
