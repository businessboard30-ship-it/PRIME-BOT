# path: discord_bot/cogs/antinuke.py

"""
Anti-nuke v1 -- detects one actor wiping a server (channel/role deletes, bans, kicks) and contains them.

Two classes, on purpose:
  * AntiNukeCog       -- a normal cog (add_cog) that only holds the audit-log listener. No slash commands.
  * AntiNukeCommands  -- AI-only commands (enable / disable / status), registered with register_hidden_cog like
                         ai_server_ops.py, so they use no slash-command slot. Needs Manage Server.
OFF by default. Opt-in per server. Needs the bot to have View Audit Log (+ Manage Roles for containment).

Safety: the server owner and the bot are never actioned; only roles the bot can actually edit are removed; the only
mention the bot sends is none at all; one wave = one containment (cooldown in modules/antinuke.py).
"""

import logging
import time
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from database import db
from modules import antinuke as an
from modules import moderation_extra as modx

logger = logging.getLogger(__name__)
NO_PINGS = discord.AllowedMentions.none()
_CFG_TTL = 60.0
_ACTION_KIND = {
    discord.AuditLogAction.channel_delete: "channel_delete",
    discord.AuditLogAction.role_delete: "role_delete",
    discord.AuditLogAction.ban: "member_ban",
    discord.AuditLogAction.kick: "member_kick",
}


class AntiNukeCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._cfg = {}

    @property
    def _clone_id(self):
        return getattr(self.bot, "clone_id", None)

    async def _config(self, guild_id: int) -> dict:
        hit = self._cfg.get(guild_id)
        if hit and time.monotonic() - hit[0] < _CFG_TTL:
            return hit[1]
        cfg = await db.get_antinuke_config(guild_id, self._clone_id)
        self._cfg[guild_id] = (time.monotonic(), cfg)
        return cfg

    def invalidate(self, guild_id: int):
        self._cfg.pop(guild_id, None)

    @commands.Cog.listener()
    async def on_audit_log_entry_create(self, entry: discord.AuditLogEntry):
        try:
            kind = _ACTION_KIND.get(entry.action)
            guild = entry.guild
            if kind is None or guild is None:
                return
            cfg = await self._config(guild.id)
            if not cfg.get("enabled"):
                return
            actor = entry.user
            if an.exempt(guild.owner_id, getattr(actor, "id", None), getattr(self.bot.user, "id", None)):
                return
            trip = an.record(guild.id, actor.id, kind, threshold=cfg.get("threshold"), window=cfg.get("window_seconds"))
            if trip is not None:
                await self._contain(guild, actor, trip, cfg)
        except Exception:
            logger.exception("[antinuke] audit-log handler failed")

    async def _contain(self, guild: discord.Guild, actor, trip: dict, cfg: dict):
        member = guild.get_member(actor.id)
        removed, failed, note = [], [], ""
        if cfg.get("action") == "contain":
            if member is None:
                note = "the actor is no longer in the server, so there was nothing to remove"
            elif not guild.me.guild_permissions.manage_roles:
                note = "I lack Manage Roles, so I could not remove their roles"
            else:
                for role in an.strippable_roles(member.roles, guild.me.top_role.position):
                    try:
                        await member.remove_roles(role, reason=f"Anti-nuke: {an.describe(trip)}")
                        removed.append(role.name)
                    except discord.HTTPException:
                        failed.append(role.name)
        lines = [f"🛡️ **Anti-nuke triggered** by <@{actor.id}> (`{actor.id}`): {an.describe(trip)}."]
        if cfg.get("action") == "contain":
            lines.append("Removed dangerous roles: " + (", ".join(removed) if removed else "none") + ".")
            if failed:
                lines.append("Could not remove: " + ", ".join(failed) + ".")
            if note:
                lines.append(f"Note: {note}.")
            lines.append("Deleted channels/roles are NOT recreated automatically; review the audit log.")
        else:
            lines.append("Alert-only mode: no roles were changed.")
        await db.antinuke_mark_triggered(guild.id, self._clone_id)
        await self._alert(guild, cfg, "\n".join(lines))
        try:
            await modx.log_action(guild.id, "antinuke_triggered", actor.id, reason=an.describe(trip))
        except Exception:
            logger.exception("[antinuke] audit log failed")

    async def _alert(self, guild: discord.Guild, cfg: dict, text: str):
        ch = guild.get_channel(cfg.get("log_channel_id") or 0) or guild.system_channel
        if ch is None:
            logger.warning("[antinuke] no channel to alert in guild %s", guild.id)
            return
        try:
            await ch.send(text[:1990], allowed_mentions=NO_PINGS)
        except discord.HTTPException:
            logger.exception("[antinuke] could not post the alert")


async def _say(i, text: str):
    if i.response.is_done():
        await i.followup.send(text[:1990], ephemeral=True, allowed_mentions=NO_PINGS)
    else:
        await i.response.send_message(text[:1990], ephemeral=True, allowed_mentions=NO_PINGS)


def _listener(bot) -> Optional[AntiNukeCog]:
    return bot.get_cog("AntiNukeCog")


class AntiNukeCommands(commands.Cog):
    """AI-only. Never add_cog'd (see module docstring)."""
    AI_CONFIRM_PROMPTS = {"antinuke_enable": "ai_enable_prompt"}
    AI_CONFIRMED_HANDLERS = {"antinuke_enable": "ai_enable"}

    def __init__(self, bot):
        self.bot = bot

    g = app_commands.guild_only()(app_commands.Group(name="antinuke", description="Anti-nuke protection"))

    def _bust(self, guild_id):
        c = _listener(self.bot)
        if c:
            c.invalidate(guild_id)

    async def ai_enable_prompt(self, interaction, threshold=None, window=None, action=None, **_):
        t, w = an.clamp_threshold(threshold), an.clamp_window(window)
        a = action if action in an.ACTIONS else "contain"
        what = ("remove the dangerous roles (admin, manage server/roles/channels, ban/kick, webhooks) from them" if a == "contain"
                else "only alert staff")
        return True, (f"🛡️ Turn on anti-nuke: if one person (never the server owner) deletes channels/roles or bans/kicks "
                      f"**{t}** times within **{w}s**, I'll {what} and post an alert. I need View Audit Log"
                      f"{' and Manage Roles' if a == 'contain' else ''}. Confirm?")

    async def ai_enable(self, interaction, threshold=None, window=None, action=None, **_):
        cid = getattr(self.bot, "clone_id", None)
        a = action if action in an.ACTIONS else "contain"
        cfg = await db.set_antinuke_config(interaction.guild_id, cid, enabled=True, threshold=an.clamp_threshold(threshold),
                                           window_seconds=an.clamp_window(window), action=a)
        self._bust(interaction.guild_id)
        me = interaction.guild.me
        missing = [n for n, ok in (("View Audit Log", me.guild_permissions.view_audit_log),
                                   ("Manage Roles", me.guild_permissions.manage_roles or a == "alert")) if not ok]
        warn = f" ⚠️ I'm missing: {', '.join(missing)} — it won't work fully until that's granted." if missing else ""
        await _say(interaction, f"✅ Anti-nuke is **on** ({cfg['threshold']} actions / {cfg['window_seconds']}s, {cfg['action']}).{warn}")

    @g.command(name="enable", description="Turn on anti-nuke (channel/role delete and mass ban/kick protection)")
    @app_commands.describe(threshold="Actions that trigger it (2-20, default 3)", window="Within this many seconds (5-120, default 10)",
                           action="contain (remove their dangerous roles + alert) or alert (alert only)")
    async def enable(self, interaction: discord.Interaction, threshold: Optional[int] = None, window: Optional[int] = None,
                     action: Optional[str] = None):
        _, text = await self.ai_enable_prompt(interaction, threshold, window, action)
        from discord_bot.cogs._views_moderation import ConfirmActionView

        async def go(ci):
            await self.ai_enable(ci, threshold, window, action)
        await interaction.response.send_message(
            text, ephemeral=True, view=ConfirmActionView(interaction.user.id, go, confirm_label="Turn on", danger=True))

    @g.command(name="disable", description="Turn off anti-nuke")
    async def disable(self, interaction: discord.Interaction):
        await db.set_antinuke_config(interaction.guild_id, getattr(self.bot, "clone_id", None), enabled=False)
        self._bust(interaction.guild_id)
        an.reset(interaction.guild_id)
        await _say(interaction, "✅ Anti-nuke is **off**.")

    @g.command(name="status", description="Show anti-nuke settings and how often it has triggered")
    async def status(self, interaction: discord.Interaction):
        c = await db.get_antinuke_config(interaction.guild_id, getattr(self.bot, "clone_id", None))
        me = interaction.guild.me.guild_permissions
        await _say(interaction, f"🛡️ Anti-nuke: **{'on' if c['enabled'] else 'off'}** — {c['threshold']} actions / "
                                f"{c['window_seconds']}s, {c['action']}. Triggered {c['triggered_count']} time(s). "
                                f"View Audit Log: {'✅' if me.view_audit_log else '❌'} · Manage Roles: {'✅' if me.manage_roles else '❌'}")


async def setup(bot: commands.Bot):
    await bot.add_cog(AntiNukeCog(bot))
    from modules.ai_command_guard import register_hidden_cog
    n = register_hidden_cog(AntiNukeCommands(bot))
    logger.info("[antinuke] listener loaded; %d AI-only command(s) registered (not on the slash tree)", n)
