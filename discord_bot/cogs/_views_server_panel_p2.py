"""
Server Owners Panel — Phase 2 screens (see SERVER_PANEL_PLAN.md).

Community hub (leveling, level roles, voice XP, starboard, suggestions,
giveaways, reaction roles), Tickets hub, and Channels & logs hub. Same rules
as Phase 1: thin shell over the existing db helpers/cogs, access re-checked on
every click and modal submit, everything scoped by (guild_id, clone_id), every
change audited, removals are two-step.
"""

from __future__ import annotations

import logging
from typing import Callable

import discord

from modules import server_panel as sp
from discord_bot.cogs._views_server_panel import (
    ServerPanelView, _btn, _onoff, _chan, guard, clone_id_of,
)

logger = logging.getLogger(__name__)

P = discord.ButtonStyle.primary
S = discord.ButtonStyle.secondary
G = discord.ButtonStyle.success
D = discord.ButtonStyle.danger


def _chan_select(placeholder: str, on_pick: Callable, types=None) -> discord.ui.ChannelSelect:
    """Channel picker whose callback receives (interaction, channel_id). The
    select is captured per instance, so several can live on one screen."""
    sel = discord.ui.ChannelSelect(
        channel_types=types or [discord.ChannelType.text], placeholder=placeholder,
        min_values=1, max_values=1)

    async def cb(interaction: discord.Interaction):
        await on_pick(interaction, sel.values[0].id)
    sel.callback = cb
    return sel


class _GuardedModal(discord.ui.Modal):
    """Modal that re-checks access on submit (modals are where writes happen)."""

    def __init__(self, title: str, opener_id: int):
        super().__init__(title=title)
        self.opener_id = opener_id

    async def allowed(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.opener_id:
            await interaction.response.send_message("This panel belongs to someone else.", ephemeral=True)
            return False
        return await guard(interaction)


# ── community hub ────────────────────────────────────────────────────────

class CommunityView(ServerPanelView):
    title = "🌱 Community"

    @classmethod
    async def load(cls, interaction):
        from database import db
        gid, cid = interaction.guild_id, clone_id_of(interaction)
        return {
            "lv": await db.get_leveling_config(gid, cid),
            "sb": await db.get_starboard_config(gid, cid),
            "sg": await db.get_suggestion_config(gid, cid),
            "rr": await db.get_reaction_role_panels_for_guild(gid, cid),
            "lr": await db.get_level_roles(gid, cid),
        }

    def body(self):
        d = self.data
        panels = {r["message_id"] for r in d.get("rr", [])}
        return [
            f"Leveling: level-ups in {_chan(None, d.get('lv', {}).get('announce_channel_id'))} "
            f"· {len(d.get('lr', []))} role reward(s)",
            f"Starboard: {_onoff(d.get('sb', {}).get('channel_id'))} "
            f"· {d.get('sb', {}).get('threshold', 5)} {d.get('sb', {}).get('emoji', '⭐')}",
            f"Suggestions log: {_chan(None, d.get('sg', {}).get('approved_log_channel_id'))}",
            f"Reaction-role panels: {len(panels)}",
        ]

    def controls(self):
        return [
            _btn("Leveling", P, self.nav_p2("LevelingView"), "📈"),
            _btn("Level roles", P, self.nav_p2("LevelRolesView"), "🏅"),
            _btn("Starboard", P, self.nav_p2("StarboardView"), "⭐"),
            _btn("Suggestions", P, self.nav_p2("SuggestionsView"), "💡"),
            _btn("Giveaways", P, self.nav_p2("GiveawaysView"), "🎁"),
            _btn("Reaction roles", P, self.nav_p2("ReactionRolesView"), "🎭"),
            self.back_button(),
        ]


# ── leveling ─────────────────────────────────────────────────────────────

class LevelingView(ServerPanelView):
    title = "📈 Leveling"

    @classmethod
    async def load(cls, interaction):
        from database import db
        gid, cid = interaction.guild_id, clone_id_of(interaction)
        return {"lv": await db.get_leveling_config(gid, cid), "vx": await db.get_voice_xp_config(gid, cid)}

    def body(self):
        lv, vx = self.data.get("lv", {}), self.data.get("vx", {})
        return [
            "Members earn XP as they chat — leveling is always on.",
            f"Level-up announcements: {_chan(None, lv.get('announce_channel_id'))}",
            f"XP rate: `{lv.get('xp_rate', 'default')}`",
            f"Voice XP: {_onoff(vx.get('enabled'))} · AFK channel excluded: {_onoff(vx.get('afk_channel_excluded'))}",
        ]

    def controls(self):
        lv, vx = self.data.get("lv", {}), self.data.get("vx", {})
        labels = {"slow": "slow (0.5x)", "default": "default (1x)", "fast": "fast (1.5x)"}
        rate = discord.ui.Select(
            placeholder="XP rate",
            options=[discord.SelectOption(label=labels[r], value=r, default=r == lv.get("xp_rate", "default"))
                     for r in sp.XP_RATES])
        rate.callback = lambda i: self._rate(i, rate.values[0])
        return [
            _chan_select("Level-up announcement channel", self._announce),
            rate,
            _btn("Voice XP: on" if vx.get("enabled") else "Voice XP: off", G if vx.get("enabled") else S,
                 self._voice, "🎙️"),
            _btn("AFK excluded: yes" if vx.get("afk_channel_excluded") else "AFK excluded: no",
                 G if vx.get("afk_channel_excluded") else S, self._afk),
            _btn("Level roles", P, self.nav_p2("LevelRolesView"), "🏅"),
            self.back_button(CommunityView),
        ]

    async def _announce(self, interaction, channel_id):
        await interaction.response.defer()
        await sp.set_leveling(interaction.guild_id, self.clone_id, interaction.user.id,
                              announce_channel_id=channel_id, announce_auto_created=False)
        await self.reload(interaction)

    async def _rate(self, interaction, value):
        await interaction.response.defer()
        await sp.set_leveling(interaction.guild_id, self.clone_id, interaction.user.id, xp_rate=value)
        await self.reload(interaction)

    async def _voice(self, interaction):
        await interaction.response.defer()
        await sp.set_voice_xp(interaction.guild_id, self.clone_id, interaction.user.id,
                              enabled=not self.data.get("vx", {}).get("enabled"))
        await self.reload(interaction)

    async def _afk(self, interaction):
        await interaction.response.defer()
        await sp.set_voice_xp(interaction.guild_id, self.clone_id, interaction.user.id,
                              afk_channel_excluded=not self.data.get("vx", {}).get("afk_channel_excluded"))
        await self.reload(interaction)


# ── level roles ──────────────────────────────────────────────────────────

class LevelModal(_GuardedModal):
    level = discord.ui.TextInput(label="Level (1-1000)", max_length=4)

    def __init__(self, role_id: int, opener_id: int):
        super().__init__("Grant role at level", opener_id)
        self.role_id = role_id

    async def on_submit(self, interaction: discord.Interaction):
        if not await self.allowed(interaction):
            return
        try:
            level = int(str(self.level.value).strip())
            if not 1 <= level <= 1000:
                raise ValueError
        except ValueError:
            await interaction.response.send_message("Level must be a whole number from 1 to 1000.", ephemeral=True)
            return
        role = interaction.guild.get_role(self.role_id)
        reason = sp.role_blocked_reason(interaction.guild, role)
        if reason:
            await interaction.response.send_message(reason, ephemeral=True)
            return
        await interaction.response.defer()
        ok = await sp.add_level_role(interaction.guild_id, clone_id_of(interaction), interaction.user.id,
                                     level, role.id)
        if not ok:
            await interaction.followup.send("❌ Couldn't save that.", ephemeral=True)
        await interaction.edit_original_response(view=await LevelRolesView.create(interaction))


class LevelRolesView(ServerPanelView):
    title = "🏅 Level roles"
    pending_role = None      # discord.Role chosen, waiting for a level
    pending_remove = None    # level whose reward is waiting for Confirm

    @classmethod
    async def load(cls, interaction):
        from database import db
        return {"roles": await db.get_level_roles(interaction.guild_id, clone_id_of(interaction))}

    def body(self):
        roles = self.data.get("roles", [])
        lines = [f"Level {r['level']} → <@&{r['role_id']}>" for r in roles[:15]] or ["No role rewards yet."]
        if len(roles) > 15:
            lines.append(f"…and {len(roles) - 15} more")
        if self.pending_role is not None:
            lines.append(f"Selected role: {self.pending_role.mention} — now tap **Add at level…**")
        if self.pending_remove is not None:
            lines.append(f"⚠️ Remove the reward for level **{self.pending_remove}**? Tap Confirm.")
        return lines

    def controls(self):
        roles = self.data.get("roles", [])
        pick = discord.ui.RoleSelect(placeholder="Pick a reward role", min_values=1, max_values=1)
        pick.callback = lambda i: self._pick_role(i, pick.values[0])
        out: list = [pick]
        if roles:
            rem = discord.ui.Select(
                placeholder="Remove a reward…",
                options=[discord.SelectOption(label=f"Level {r['level']}", value=str(r["level"]))
                         for r in roles[:25]])
            rem.callback = lambda i: self._pick_remove(i, int(rem.values[0]))
            out.append(rem)
        out.append(_btn("Add at level…", G, self._add, "➕", disabled=self.pending_role is None))
        if self.pending_remove is not None:
            out.append(_btn(f"Confirm remove Lv {self.pending_remove}", D, self._confirm_remove, "🗑️"))
            out.append(_btn("Cancel", S, self._cancel))
        out.append(self.back_button(LevelingView))
        return out

    async def _rerender(self, interaction):
        self._build()
        await interaction.response.edit_message(view=self)

    async def _pick_role(self, interaction, role):
        self.pending_role = role
        await self._rerender(interaction)

    async def _pick_remove(self, interaction, level):
        self.pending_remove = level   # first step does nothing destructive
        await self._rerender(interaction)

    async def _cancel(self, interaction):
        self.pending_remove = None
        await self._rerender(interaction)

    async def _add(self, interaction):
        reason = sp.role_blocked_reason(interaction.guild, self.pending_role)
        if reason:
            await interaction.response.send_message(reason, ephemeral=True)
            return
        await interaction.response.send_modal(LevelModal(self.pending_role.id, interaction.user.id))

    async def _confirm_remove(self, interaction):
        level = self.pending_remove
        if level is None:
            await interaction.response.defer()
            return
        await interaction.response.defer()
        await sp.remove_level_role(interaction.guild_id, self.clone_id, interaction.user.id, level)
        await self.reload(interaction)


# ── starboard ────────────────────────────────────────────────────────────

class StarEmojiModal(_GuardedModal):
    emoji = discord.ui.TextInput(label="Emoji (e.g. ⭐ or a custom emoji)", max_length=64)

    async def on_submit(self, interaction: discord.Interaction):
        if not await self.allowed(interaction):
            return
        value = str(self.emoji.value).strip()
        if not value:
            await interaction.response.send_message("Type an emoji.", ephemeral=True)
            return
        await interaction.response.defer()
        await sp.set_starboard(interaction.guild_id, clone_id_of(interaction), interaction.user.id, emoji=value)
        await interaction.edit_original_response(view=await StarboardView.create(interaction))


class StarboardView(ServerPanelView):
    title = "⭐ Starboard"

    @classmethod
    async def load(cls, interaction):
        from database import db
        return {"sb": await db.get_starboard_config(interaction.guild_id, clone_id_of(interaction))}

    def body(self):
        c = self.data.get("sb", {})
        return [
            f"Starboard: {_onoff(c.get('channel_id'))} · {_chan(None, c.get('channel_id'))}",
            f"Needs **{c.get('threshold', 5)}** {c.get('emoji', '⭐')} to be featured.",
        ]

    def controls(self):
        c = self.data.get("sb", {})
        thr = discord.ui.Select(
            placeholder="Stars needed",
            options=[discord.SelectOption(label=f"{n} star{'s' if n != 1 else ''}", value=str(n),
                                          default=n == c.get("threshold", 5)) for n in sp.STAR_THRESHOLDS])
        thr.callback = lambda i: self._threshold(i, int(thr.values[0]))
        out = [_chan_select("Starboard channel", self._channel), thr,
               _btn("Change emoji", P, self._emoji, "✏️")]
        if c.get("channel_id"):
            out.append(_btn("Disable", D, self._disable))
        out.append(self.back_button(CommunityView))
        return out

    async def _channel(self, interaction, channel_id):
        await interaction.response.defer()
        await sp.set_starboard(interaction.guild_id, self.clone_id, interaction.user.id, channel_id=channel_id)
        await self.reload(interaction)

    async def _threshold(self, interaction, n):
        await interaction.response.defer()
        await sp.set_starboard(interaction.guild_id, self.clone_id, interaction.user.id, threshold=n)
        await self.reload(interaction)

    async def _emoji(self, interaction):
        await interaction.response.send_modal(StarEmojiModal("Starboard emoji", interaction.user.id))

    async def _disable(self, interaction):
        await interaction.response.defer()
        await sp.set_starboard(interaction.guild_id, self.clone_id, interaction.user.id, channel_id=None)
        await self.reload(interaction)


# ── suggestions ──────────────────────────────────────────────────────────

class SuggestionsView(ServerPanelView):
    title = "💡 Suggestions"

    @classmethod
    async def load(cls, interaction):
        from database import db
        return {"sg": await db.get_suggestion_config(interaction.guild_id, clone_id_of(interaction))}

    def body(self):
        ch = self.data.get("sg", {}).get("approved_log_channel_id")
        return ["`/suggest` works with no setup.",
                f"Approved suggestions are logged to: {_chan(None, ch)}"]

    def controls(self):
        out = [_chan_select("Approved-suggestions log channel", self._pick),
               _btn("Create suggestions channel", P, self._enable, "➕")]
        if self.data.get("sg", {}).get("approved_log_channel_id"):
            out.append(_btn("Clear log channel", D, self._clear))
        out.append(self.back_button(CommunityView))
        return out

    async def _pick(self, interaction, channel_id):
        await interaction.response.defer()
        await sp.set_suggestions(interaction.guild_id, self.clone_id, interaction.user.id, channel_id)
        await self.reload(interaction)

    async def _clear(self, interaction):
        await interaction.response.defer()
        await sp.set_suggestions(interaction.guild_id, self.clone_id, interaction.user.id, None)
        await self.reload(interaction)

    async def _enable(self, interaction):
        from discord_bot.cogs._views_join_dm import _enable_suggestions
        await interaction.response.defer(ephemeral=True)
        ok, msg = await _enable_suggestions(interaction, interaction.guild, self.clone_id)
        if msg:
            await interaction.followup.send(msg, ephemeral=True)
        await sp.record_change(interaction.guild_id, self.clone_id, interaction.user.id,
                               "suggestions.enable", None, bool(ok))


# ── giveaways (launches the existing wizard) ─────────────────────────────

class GiveawaysView(ServerPanelView):
    title = "🎁 Giveaways"

    def body(self):
        return ["The guided giveaway wizard posts its setup message in **this channel**, "
                "so open the panel in the channel you want to use.",
                "💎 Premium unlocks extra giveaway options inside the wizard."]

    def controls(self):
        return [_btn("Open giveaway wizard", G, self._open, "🎁"), self.back_button(CommunityView)]

    async def _open(self, interaction):
        cog = interaction.client.get_cog("GiveawayCog")
        if cog is None:
            await interaction.response.send_message("Giveaways aren't available right now.", ephemeral=True)
            return
        await sp.record_change(interaction.guild_id, self.clone_id, interaction.user.id,
                               "giveaway.wizard_opened", None, interaction.channel_id)
        await cog.setup_wizard.callback(cog, interaction)


# ── reaction roles ───────────────────────────────────────────────────────

class ReactionPanelModal(_GuardedModal):
    panel_title = discord.ui.TextInput(label="Panel title", max_length=100)
    panel_desc = discord.ui.TextInput(label="Description", style=discord.TextStyle.paragraph,
                                      max_length=300, required=False,
                                      default="Tap a button below to get a role.")

    async def on_submit(self, interaction: discord.Interaction):
        if not await self.allowed(interaction):
            return
        cog = interaction.client.get_cog("ReactionRolesCog")
        if cog is None:
            await interaction.response.send_message("Reaction roles aren't available right now.", ephemeral=True)
            return
        await sp.record_change(interaction.guild_id, clone_id_of(interaction), interaction.user.id,
                               "reactionrole.panel_created", None, str(self.panel_title.value)[:100])
        # Same coroutine the slash command runs: it checks Manage Roles and replies itself.
        await cog.create.callback(cog, interaction, title=str(self.panel_title.value).strip(),
                                  description=str(self.panel_desc.value).strip() or "Tap a button below to get a role.")


class ReactionRolesView(ServerPanelView):
    title = "🎭 Reaction roles"

    @classmethod
    async def load(cls, interaction):
        from database import db
        return {"rows": await db.get_reaction_role_panels_for_guild(interaction.guild_id, clone_id_of(interaction))}

    def body(self):
        panels: dict = {}
        for r in self.data.get("rows", []):
            panels.setdefault((r["channel_id"], r["message_id"]), []).append(r["role_id"])
        lines = [f"<#{ch}> — {len(roles)} role button(s)" for (ch, _m), roles in list(panels.items())[:10]]
        lines = lines or ["No panels yet."]
        lines.append("Add role buttons to a panel with `/reactionrole add`.")
        return lines

    def controls(self):
        return [_btn("Create panel in this channel", G, self._create, "➕"), self.back_button(CommunityView)]

    async def _create(self, interaction):
        await interaction.response.send_modal(ReactionPanelModal("New reaction-role panel", interaction.user.id))


# ── tickets ──────────────────────────────────────────────────────────────

class TicketGreetingModal(_GuardedModal):
    text = discord.ui.TextInput(label="Message shown in each new ticket", style=discord.TextStyle.paragraph,
                                max_length=500)

    def __init__(self, current: str, opener_id: int):
        super().__init__("Ticket greeting", opener_id)
        self.text.default = current

    async def on_submit(self, interaction: discord.Interaction):
        if not await self.allowed(interaction):
            return
        await interaction.response.defer()
        await sp.set_tickets(interaction.guild_id, clone_id_of(interaction), interaction.user.id,
                             welcome_message=str(self.text.value).strip())
        await interaction.edit_original_response(view=await TicketsView.create(interaction))


class TicketsView(ServerPanelView):
    title = "🎫 Tickets"

    @classmethod
    async def load(cls, interaction):
        from database import db
        return {"tk": await db.get_ticket_config(interaction.guild_id, clone_id_of(interaction))}

    def body(self):
        c = self.data.get("tk", {})
        role = f"<@&{c['support_role_id']}>" if c.get("support_role_id") else "not set"
        cat = f"<#{c['category_id']}>" if c.get("category_id") else "not set"
        return [
            f"Ticket panel: {_chan(None, c.get('panel_channel_id'))}",
            f"Category for new tickets: {cat}",
            f"Staff role: {role}",
            f"Greeting: `{(c.get('welcome_message') or 'default')[:100]}`",
        ]

    def controls(self):
        role = discord.ui.RoleSelect(placeholder="Staff role", min_values=1, max_values=1)
        role.callback = lambda i: self._role(i, role.values[0].id)
        return [
            _chan_select("Ticket category", self._category, [discord.ChannelType.category]),
            role,
            _btn("Create ticket panel", G, self._panel, "➕"),
            _btn("Edit greeting", P, self._greeting, "✏️"),
            self.back_button(),
        ]

    async def _category(self, interaction, channel_id):
        await interaction.response.defer()
        await sp.set_tickets(interaction.guild_id, self.clone_id, interaction.user.id, category_id=channel_id)
        await self.reload(interaction)

    async def _role(self, interaction, role_id):
        await interaction.response.defer()
        await sp.set_tickets(interaction.guild_id, self.clone_id, interaction.user.id, support_role_id=role_id)
        await self.reload(interaction)

    async def _greeting(self, interaction):
        cur = self.data.get("tk", {}).get("welcome_message") or ""
        await interaction.response.send_modal(TicketGreetingModal(cur, interaction.user.id))

    async def _panel(self, interaction):
        from discord_bot.cogs._views_join_dm import _enable_tickets
        await interaction.response.defer(ephemeral=True)
        ok, msg = await _enable_tickets(interaction, interaction.guild, self.clone_id)
        if msg:
            await interaction.followup.send(msg, ephemeral=True)
        await sp.record_change(interaction.guild_id, self.clone_id, interaction.user.id,
                               "tickets.enable", None, bool(ok))


# ── channels & logs ──────────────────────────────────────────────────────

LOG_LABELS = {
    "server": "Server changes", "channels": "Channels", "roles": "Roles", "members": "Members",
    "moderation": "Moderation", "voice": "Voice", "invites": "Invites",
}


class ChannelsLogsView(ServerPanelView):
    title = "📚 Channels & logs"

    @classmethod
    async def load(cls, interaction):
        from database import db
        gid, cid = interaction.guild_id, clone_id_of(interaction)
        return {"am": await db.get_automod_config(gid, cid), "lv": await db.get_leveling_config(gid, cid)}

    def body(self):
        am, lv = self.data.get("am", {}), self.data.get("lv", {})
        on = [LOG_LABELS[c] for c, col in sp.MODLOG_CATEGORY_FIELDS.items() if am.get(col)]
        return [
            f"Server log channel: {_chan(None, am.get('log_channel_id'))}",
            f"Logging: {', '.join(on) if on else 'nothing yet'}",
            f"Announcements (level-ups): {_chan(None, lv.get('announce_channel_id'))}",
            f"Daily leaderboard autopost: {_chan(None, lv.get('leaderboard_autopost_channel_id'))}",
        ]

    def controls(self):
        am, lv = self.data.get("am", {}), self.data.get("lv", {})
        cats = discord.ui.Select(
            placeholder="What to log", min_values=0, max_values=len(sp.MODLOG_CATEGORY_FIELDS),
            options=[discord.SelectOption(label=LOG_LABELS[c], value=c, default=bool(am.get(col)))
                     for c, col in sp.MODLOG_CATEGORY_FIELDS.items()])
        cats.callback = lambda i: self._categories(i, set(cats.values))
        out = [
            _chan_select("Server log channel", self._log_channel),
            cats,
            _chan_select("Announcement channel", self._announce),
            _chan_select("Leaderboard autopost channel", self._autopost),
            _btn("Create missing channels", G, self._missing, "➕"),
        ]
        if lv.get("leaderboard_autopost_channel_id"):
            out.append(_btn("Autopost off", D, self._autopost_off))
        out.append(self.back_button())
        return out

    async def _log_channel(self, interaction, channel_id):
        await interaction.response.defer()
        await sp.set_automod(interaction.guild_id, self.clone_id, interaction.user.id, log_channel_id=channel_id)
        await self.reload(interaction)

    async def _categories(self, interaction, selected):
        await interaction.response.defer()
        await sp.set_modlog_categories(interaction.guild_id, self.clone_id, interaction.user.id, selected)
        await self.reload(interaction)

    async def _announce(self, interaction, channel_id):
        await interaction.response.defer()
        await sp.set_leveling(interaction.guild_id, self.clone_id, interaction.user.id,
                              announce_channel_id=channel_id, announce_auto_created=False)
        await self.reload(interaction)

    async def _autopost(self, interaction, channel_id):
        await interaction.response.defer()
        await sp.set_leveling(interaction.guild_id, self.clone_id, interaction.user.id,
                              leaderboard_autopost_channel_id=channel_id)
        await self.reload(interaction)

    async def _autopost_off(self, interaction):
        await interaction.response.defer()
        await sp.set_leveling(interaction.guild_id, self.clone_id, interaction.user.id,
                              leaderboard_autopost_channel_id=None)
        await self.reload(interaction)

    async def _missing(self, interaction):
        if not getattr(interaction.permissions, "manage_channels", False):
            await interaction.response.send_message(
                "You need the **Manage Channels** permission to create channels.", ephemeral=True)
            return
        from discord_bot.cogs.setup_channels import (
            SetupSuggestView, build_suggestions_embed, scan_missing_channels)
        await interaction.response.defer(ephemeral=True)
        missing = await scan_missing_channels(interaction.guild, self.clone_id)
        embed = build_suggestions_embed(interaction.guild, missing)
        if missing:
            await interaction.followup.send(embed=embed, view=SetupSuggestView(interaction.guild_id, missing),
                                            ephemeral=True)
        else:
            await interaction.followup.send(embed=embed, ephemeral=True)
