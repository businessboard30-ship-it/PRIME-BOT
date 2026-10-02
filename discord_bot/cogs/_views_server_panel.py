"""
Server Owners Panel — Phase 1 screens (see SERVER_PANEL_PLAN.md).

Opened from /serversetup (its default screen), so it costs no new global
slash-command slot. Same rules as the owner panel:

* Thin shell: reads and writes go through modules/server_panel.py, which calls
  the same db.get_*/set_*_config functions the slash commands use.
* Access (server owner or Manage Server, plus the _perm_guard lockout) is
  re-checked on EVERY click and every modal submit.
* Only the person who opened it can click. Ephemeral. Buttons, selects and
  modals only: <=5 buttons per row, <=25 select options, <40 components.
* Every change writes one audit line plus a row in server_panel_audit.
* Everything is scoped by (guild_id, clone_id).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Awaitable, Callable, List, Optional

import discord

from modules import server_panel as sp

logger = logging.getLogger(__name__)

PANEL_TIMEOUT = 600  # seconds idle; Discord resets it on every interaction
HINT = "-# Panel closes after 10 idle minutes — reopen with `/serversetup`."
MAX_COMPONENTS = 40

AUTOMOD_FILTERS = [
    ("word_filter_enabled", "Word filter"),
    ("anti_invite_enabled", "Invite filter"),
    ("anti_mention_enabled", "Mention spam"),
    ("spam_enabled", "Flood spam"),
]
SELECT_TYPES = (discord.ui.Select, discord.ui.ChannelSelect, discord.ui.RoleSelect)
AUTOMOD_ACTIONS = ["delete", "warn", "timeout", "kick"]  # automod.VALID_ACTIONS


def clone_id_of(interaction: discord.Interaction) -> Optional[int]:
    return getattr(interaction.client, "clone_id", None)


def _btn(label: str, style: discord.ButtonStyle, cb: Callable, emoji: str = None,
         disabled: bool = False) -> discord.ui.Button:
    b = discord.ui.Button(label=label, style=style, emoji=emoji, disabled=disabled)
    b.callback = cb
    return b


def _onoff(v) -> str:
    return "✅ on" if v else "❌ off"


def _chan(guild: Optional[discord.Guild], channel_id) -> str:
    return f"<#{channel_id}>" if channel_id else "not set"


async def guard(interaction: discord.Interaction) -> bool:
    """Access re-check usable from views and modals. Replies and returns False
    when the user is no longer allowed."""
    reason = sp.access_denied_reason(interaction.guild, interaction.user.id,
                                     getattr(interaction, "permissions", None))
    if reason:
        if interaction.response.is_done():
            await interaction.followup.send(reason, ephemeral=True)
        else:
            await interaction.response.send_message(reason, ephemeral=True)
        return False
    return True


async def _no_switches() -> set:
    return set()


def read_only_ok(fn: Callable) -> Callable:
    """Mark a callback as safe in read-only mode (navigation and refresh only)."""
    fn.read_only_ok = True
    return fn


@dataclass
class InspectContext:
    """Owner inspection of one server's panel (Phase 4). `back` returns the
    owner to the Server inspector in the owner panel."""
    guild: "discord.Guild"
    back: Callable[[discord.Interaction], Awaitable[None]]


class InspectInteraction:
    """The owner's real interaction, answering as the inspected server (guild,
    guild_id) so every existing screen loads that server's data unchanged."""

    def __init__(self, real: discord.Interaction, ctx: InspectContext):
        self._real = real
        self.inspect = ctx
        self.guild = ctx.guild
        self.guild_id = ctx.guild.id

    def __getattr__(self, name):
        return getattr(self._real, name)


# ── base view ────────────────────────────────────────────────────────────

class ServerPanelView(discord.ui.LayoutView):
    """One screen. Subclasses override load() (async data fetch), body() and
    controls()."""

    title = "Server panel"
    accent = discord.Color.blurple()

    switch_key: Optional[str] = None      # owner kill switch that makes this whole screen read-only
    banner_keys: tuple = ()               # extra switches to mention on this screen (hubs)

    def __init__(self, guild_id: int, clone_id: Optional[int], opener_id: int, data: Optional[dict] = None,
                 inspect: Optional[InspectContext] = None):
        super().__init__(timeout=PANEL_TIMEOUT)
        self.guild_id = guild_id
        self.clone_id = clone_id
        self.opener_id = opener_id
        self.data = data or {}
        self.inspect = inspect
        self._allowed_ids: set = set()
        self._build()

    # kill switches (owner panel) ----------------------------------------
    def off(self, key: str) -> bool:
        """True when the bot owner has switched this feature off."""
        return key in self.data.get("_engaged", ())

    @property
    def locked(self) -> bool:
        return bool(self.switch_key) and self.off(self.switch_key)

    @property
    def read_only(self) -> bool:
        return self.inspect is not None or self.locked

    def _ctx(self, interaction: discord.Interaction):
        """The interaction to load the next screen with (keeps inspect mode)."""
        return InspectInteraction(interaction, self.inspect) if self.inspect is not None else interaction

    @classmethod
    async def load(cls, interaction: discord.Interaction) -> dict:
        return {}

    @classmethod
    async def create(cls, interaction: discord.Interaction) -> "ServerPanelView":
        ctx = interaction.inspect if isinstance(interaction, InspectInteraction) else None
        import asyncio
        loaded, engaged = await asyncio.gather(
            cls.load(interaction),
            _no_switches() if ctx is not None else sp.engaged_features(interaction.user.id),
        )
        data = dict(loaded or {})
        data["_engaged"] = engaged
        return cls(interaction.guild_id, clone_id_of(interaction), interaction.user.id, data, inspect=ctx)

    def body(self) -> List[str]:
        return []

    def controls(self) -> List[discord.ui.Item]:
        return []

    def _build(self) -> None:
        self.clear_items()
        notes = self._notes()
        footer = (f"-# 🔍 Read-only view of **{self.inspect.guild.name}** — nothing here can be changed."
                  if self.inspect is not None else HINT)
        children: list = [discord.ui.TextDisplay("\n".join([f"### {self.title}", *notes, *self.body(), footer]))]
        items = self.controls()
        if self.read_only:
            # Navigation and refresh only: no writes, no modals, no selects.
            items = [it for it in items if isinstance(it, discord.ui.Button)
                     and getattr(it.callback, "read_only_ok", False)]
        self._allowed_ids = {getattr(it, "custom_id", None) for it in items}
        if items:
            children.append(discord.ui.Separator())
            row: list = []
            for it in items:
                if isinstance(it, SELECT_TYPES):
                    if row:
                        children.append(discord.ui.ActionRow(*row)); row = []
                    children.append(discord.ui.ActionRow(it))
                else:
                    row.append(it)
                    if len(row) == 5:
                        children.append(discord.ui.ActionRow(*row)); row = []
            if row:
                children.append(discord.ui.ActionRow(*row))
        self.add_item(discord.ui.Container(*children, accent_colour=self.accent))

    def _notes(self) -> List[str]:
        """One line per owner-disabled feature this screen touches."""
        keys = list(self.banner_keys) + ([self.switch_key] if self.switch_key else [])
        out = []
        for k in dict.fromkeys(keys):
            if self.off(k):
                try:
                    from modules import admin_controls as ac
                    label = ac.FEATURES[k][0]
                except Exception:
                    label = k
                out.append(f"⏸️ **{label}** is turned off by the bot owner for now"
                           + (" — settings here are read-only." if k == self.switch_key else "."))
        return out

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        wrong_server = self.inspect is None and interaction.guild_id != self.guild_id
        if interaction.user.id != self.opener_id or wrong_server:
            await interaction.response.send_message("This panel belongs to someone else.", ephemeral=True)
            return False
        if self.inspect is not None:
            from discord_bot.cogs._views_admin_panel import allowed_sections, refresh_access
            await refresh_access()
            if "inspect" not in allowed_sections(interaction.user.id):
                await interaction.response.send_message("You're no longer authorized for this.", ephemeral=True)
                return False
        elif not await guard(interaction):
            return False
        if self.read_only:
            cid = (getattr(interaction, "data", None) or {}).get("custom_id")
            if cid not in self._allowed_ids:
                await interaction.response.send_message("This screen is read-only right now.", ephemeral=True)
                return False
        return True

    async def on_timeout(self) -> None:
        for child in self.walk_children():
            if hasattr(child, "disabled"):
                child.disabled = True

    # navigation ---------------------------------------------------------
    def nav(self, view_cls) -> Callable:
        @read_only_ok
        async def cb(interaction: discord.Interaction):
            await interaction.response.defer()
            view = await view_cls.create(self._ctx(interaction))
            await interaction.edit_original_response(view=view)
        return cb

    def nav_p2(self, name: str) -> Callable:
        """Navigate to a Phase 2 screen by class name (lazy import avoids a cycle)."""
        @read_only_ok
        async def cb(interaction: discord.Interaction):
            from discord_bot.cogs import _views_server_panel_p2 as p2
            await interaction.response.defer()
            view = await getattr(p2, name).create(self._ctx(interaction))
            await interaction.edit_original_response(view=view)
        return cb

    def nav_p3(self, name: str) -> Callable:
        """Navigate to a Phase 3 screen by class name (lazy import avoids a cycle)."""
        @read_only_ok
        async def cb(interaction: discord.Interaction):
            from discord_bot.cogs import _views_server_panel_p3 as p3
            await interaction.response.defer()
            view = await getattr(p3, name).create(self._ctx(interaction))
            await interaction.edit_original_response(view=view)
        return cb

    def back_button(self, target=None) -> discord.ui.Button:
        return _btn("Back", discord.ButtonStyle.secondary, self.nav(target or HomeView), "⬅️")

    async def reload(self, interaction: discord.Interaction) -> None:
        """Re-render this same screen from fresh data after a write."""
        view = await type(self).create(self._ctx(interaction))
        await interaction.edit_original_response(view=view)


# ── home ─────────────────────────────────────────────────────────────────

class HomeView(ServerPanelView):
    title = "Server panel"

    @classmethod
    async def load(cls, interaction):
        cid = clone_id_of(interaction)
        import asyncio
        items, prem = await asyncio.gather(
            sp.setup_items(interaction.guild, cid),
            sp.premium_status(interaction.guild_id, cid),
        )
        done, total = sp.setup_score(items)
        return {
            "name": interaction.guild.name, "members": interaction.guild.member_count,
            "online": interaction.client.is_ready(), "premium": prem, "done": done, "total": total,
        }

    def body(self) -> List[str]:
        d = self.data
        prem = d.get("premium", {})
        if prem.get("active"):
            exp = prem.get("expires_at")
            prem_line = "💎 Premium active" + (f" — renews/expires <t:{int(exp.timestamp())}:R>" if exp else "")
        else:
            prem_line = "💎 Premium not active"
        return [
            f"**{d.get('name', 'This server')}** — {d.get('members', '?')} members",
            f"{'🟢 Bot online' if d.get('online') else '🔴 Bot reconnecting'}  ·  {prem_line}",
            f"Setup score: **{d.get('done', 0)}/{d.get('total', 8)}** core features configured",
        ]

    def controls(self):
        P = discord.ButtonStyle.primary
        S = discord.ButtonStyle.secondary
        out = [
            _btn("Setup", P, self.nav(SetupView), "📋"),
            _btn("Welcome & verification", P, self.nav(WelcomeView), "👋"),
            _btn("Moderation", P, self.nav(ModerationView), "🛡️"),
            _btn("Community", P, self.nav_p2("CommunityView"), "🌱"),
            _btn("Tickets", P, self.nav_p2("TicketsView"), "🎫"),
            _btn("Channels & logs", P, self.nav_p2("ChannelsLogsView"), "📚"),
            _btn("Stats", S, self.nav_p3("StatsView"), "📊"),
            _btn("Change history", S, self.nav_p3("HistoryView"), "🕘"),
            _btn("Help & tools", S, self.nav_p3("HelpToolsView"), "🧰"),
            _btn("Premium", S, self.nav(PremiumView), "💎"),
            _btn("Quick enable", S, self._legacy, "⚡"),
        ]
        if self.inspect is not None:
            out = [b for b in out if b.label != "Help & tools"]
            out.append(_btn("Back to owner panel", S, self._exit, "⬅️"))
        return out

    @read_only_ok
    async def _exit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        await self.inspect.back(interaction)

    async def _legacy(self, interaction: discord.Interaction):
        """The original one-tap enable wizard, kept until Phase 2 hubs cover it."""
        from discord_bot.cogs.automation import ServerSetupView
        await interaction.response.send_message(
            "Tap each feature you want to turn on:",
            view=ServerSetupView(clone_id_of(interaction), "en"), ephemeral=True)


# ── premium (read-only status + path to checkout) ────────────────────────

class PremiumView(ServerPanelView):
    title = "💎 Premium"
    accent = discord.Color.gold()

    @classmethod
    async def load(cls, interaction):
        return {"premium": await sp.premium_status(interaction.guild_id, clone_id_of(interaction))}

    def body(self):
        prem = self.data.get("premium", {})
        if prem.get("active"):
            exp = prem.get("expires_at")
            lines = ["Status: **active**" + (f" · expires <t:{int(exp.timestamp())}:F>" if exp else "")]
        else:
            lines = ["Status: **not active**"]
        lines.append("Premium unlocks the extra welcome card packs, honeypot extras and more for the whole server.")
        return lines

    def controls(self):
        label = "Renew" if self.data.get("premium", {}).get("active") else "Go Premium"
        return [
            _btn(label, discord.ButtonStyle.success, self._checkout, "💎"),
            self.back_button(),
        ]

    async def _checkout(self, interaction: discord.Interaction):
        from discord_bot.cogs._views_premium import send_premium_pitch
        await interaction.response.defer(ephemeral=True)
        await send_premium_pitch(interaction, interaction.guild_id, clone_id_of(interaction))


# ── setup checklist ──────────────────────────────────────────────────────

class SetupView(ServerPanelView):
    title = "📋 Setup checklist"

    @classmethod
    async def load(cls, interaction):
        items = await sp.setup_items(interaction.guild, clone_id_of(interaction))
        return {"items": items}

    def body(self):
        items = self.data.get("items", [])
        done, total = sp.setup_score(items)
        lines = [f"**{done}/{total}** done"]
        lines += [f"{'✅' if i.done else '⬜'} {i.label}" for i in items]
        return lines

    def controls(self):
        targets = {
            "welcome": self.nav(WelcomeView), "verification": self.nav(VerificationView),
            "automod": self.nav(ModerationView), "modlog": self.nav(ModerationView),
            "premium": self.nav(PremiumView),
            "leveling": self.nav_p2("LevelingView"), "tickets": self.nav_p2("TicketsView"),
            "channels": self.nav_p2("ChannelsLogsView"),
        }
        short = {"welcome": "Welcome", "verification": "Verification", "automod": "Auto-mod",
                 "modlog": "Mod-log", "leveling": "Leveling", "tickets": "Tickets",
                 "channels": "Create channels", "premium": "Premium"}
        out = [_btn(short[i.key], discord.ButtonStyle.primary, targets[i.key])
               for i in self.data.get("items", []) if not i.done]
        out.append(self.back_button())
        return out


# ── welcome & verification ───────────────────────────────────────────────

class WelcomeMessageModal(discord.ui.Modal, title="Edit welcome message"):
    template = discord.ui.TextInput(
        label="Message ({member} {guild} {count})",
        style=discord.TextStyle.paragraph, max_length=300)

    def __init__(self, current: str, opener_id: int):
        super().__init__()
        self.opener_id = opener_id
        self.template.default = current

    async def on_submit(self, interaction: discord.Interaction):
        if interaction.user.id != self.opener_id or not await guard(interaction):
            return
        await interaction.response.defer()
        await sp.set_welcome(interaction.guild_id, clone_id_of(interaction), interaction.user.id,
                             message_template=str(self.template.value).strip())
        view = await WelcomeView.create(interaction)
        await interaction.edit_original_response(view=view)


class WelcomeView(ServerPanelView):
    title = "👋 Welcome"

    @classmethod
    async def load(cls, interaction):
        from database import db
        import asyncio
        cid = clone_id_of(interaction)
        cfg, extras = await asyncio.gather(
            db.get_welcome_config(interaction.guild_id, cid),
            db.get_welcome_extras(interaction.guild_id, cid),
        )
        return {"cfg": cfg, "x": extras}

    def body(self):
        c = self.data.get("cfg", {})
        x = self.data.get("x", {})
        roles = [r for r in (x.get("member_role_id"), x.get("bot_role_id")) if r]
        return [
            f"Welcome card: {_onoff(c.get('enabled'))}",
            f"Channel: {_chan(None, c.get('channel_id'))}",
            f"Message: `{(c.get('message_template') or '')[:120]}`",
            f"Card style: `{c.get('card_style', 'gif')}` · theme `{c.get('card_theme', 'wolf')}`",
            f"Goodbye: {_onoff(x.get('goodbye_enabled'))} · auto-roles: {len(roles)} set",
        ]

    def controls(self):
        c = self.data.get("cfg", {})
        pick = discord.ui.ChannelSelect(
            channel_types=[discord.ChannelType.text], placeholder="Welcome channel",
            min_values=1, max_values=1)
        pick.callback = self._pick_channel
        return [
            pick,
            _btn("Turn off" if c.get("enabled") else "Turn on",
                 discord.ButtonStyle.danger if c.get("enabled") else discord.ButtonStyle.success, self._toggle),
            _btn("Edit message", discord.ButtonStyle.primary, self._edit, "✏️"),
            _btn("Cards & themes", discord.ButtonStyle.primary, self._open_wizard, "🎴"),
            _btn("Customize card", discord.ButtonStyle.primary, self._open_card_customizer, "🎨"),
            _btn("Goodbye & roles", discord.ButtonStyle.primary, self._open_extras, "👋"),
            _btn("Send test post", discord.ButtonStyle.success, self._test_post, "🧪"),
            _btn("Verification", discord.ButtonStyle.primary, self.nav(VerificationView), "🔐"),
            self.back_button(),
        ]

    @read_only_ok
    async def _open_extras(self, interaction: discord.Interaction):
        """Goodbye message and auto-roles for new members / bots."""
        from discord_bot.cogs._views_server_panel_extras import WelcomeExtrasView
        await interaction.response.defer()
        view = await WelcomeExtrasView.create(self._ctx(interaction))
        await interaction.edit_original_response(view=view)

    async def _test_post(self, interaction: discord.Interaction):
        """Posts a REAL welcome card for the person who pressed it, in the
        configured welcome channel — no alt account needed."""
        from discord_bot.cogs import welcome_extras as we
        await interaction.response.defer(ephemeral=True)
        member = interaction.guild.get_member(interaction.user.id) or interaction.user
        ok, msg = await we.send_test_welcome(interaction.client, interaction.guild, member)
        await interaction.followup.send(("✅ " if ok else "⚠️ ") + msg, ephemeral=True)

    async def _open_wizard(self, interaction: discord.Interaction):
        """Full welcome wizard: theme, card look/style, avatar shape, sticker,
        delivery mode, preview and the premium packs."""
        from database import db
        from discord_bot.cogs import _views_welcome as vw
        await interaction.response.defer(ephemeral=True)
        cfg = await db.get_welcome_config(interaction.guild_id, clone_id=self.clone_id)
        view = vw.build_wizard_view(interaction.guild_id, self.clone_id, interaction.user.id, cfg,
                                    goodbye=await vw.fetch_goodbye(interaction.guild_id, self.clone_id))
        await interaction.followup.send(view=view, ephemeral=True)

    async def _open_card_customizer(self, interaction: discord.Interaction):
        """Card customizer: banner, background, colors, shapes, heading text, preview."""
        from discord_bot.cogs import _views_card_customize as cc
        await interaction.response.defer(ephemeral=True)
        await cc.open_customize_wizard(interaction, interaction.guild_id, self.clone_id)

    async def _toggle(self, interaction: discord.Interaction):
        c = self.data.get("cfg", {})
        turning_on = not c.get("enabled")
        if turning_on and not c.get("channel_id"):
            await interaction.response.send_message("Pick a welcome channel first.", ephemeral=True)
            return
        await interaction.response.defer()
        await sp.set_welcome(interaction.guild_id, self.clone_id, interaction.user.id, enabled=turning_on)
        await self.reload(interaction)

    async def _pick_channel(self, interaction: discord.Interaction):
        select = next(c for c in self.walk_children() if isinstance(c, discord.ui.ChannelSelect))
        await interaction.response.defer()
        await sp.set_welcome(interaction.guild_id, self.clone_id, interaction.user.id,
                             channel_id=select.values[0].id)
        await self.reload(interaction)

    async def _edit(self, interaction: discord.Interaction):
        cur = self.data.get("cfg", {}).get("message_template") or ""
        await interaction.response.send_modal(WelcomeMessageModal(cur, interaction.user.id))


class VerificationView(ServerPanelView):
    title = "🔐 Verification gate"

    @classmethod
    async def load(cls, interaction):
        from database import db
        return {"cfg": await db.get_verification_config(interaction.guild_id, clone_id_of(interaction))}

    def body(self):
        c = self.data.get("cfg", {})
        return [
            f"Gate: {_onoff(c.get('enabled'))} · mode `{c.get('mode', 'button')}`",
            f"Channel: {_chan(None, c.get('channel_id'))}",
            f"Verified role: {'<@&%s>' % c['verified_role_id'] if c.get('verified_role_id') else 'not set'}",
        ]

    def controls(self):
        c = self.data.get("cfg", {})
        configured = bool(c.get("channel_id") and c.get("verified_role_id"))
        return [
            _btn("Open full wizard", discord.ButtonStyle.primary, self._wizard, "🧭"),
            _btn("Turn off" if c.get("enabled") else "Turn on",
                 discord.ButtonStyle.danger if c.get("enabled") else discord.ButtonStyle.success,
                 self._toggle, disabled=not configured and not c.get("enabled")),
            self.back_button(WelcomeView),
        ]

    async def _wizard(self, interaction: discord.Interaction):
        from discord_bot.cogs.verification import WizardView as VerificationWizard
        wiz = VerificationWizard(interaction.user.id, self.data.get("cfg", {}))
        await interaction.response.send_message(embed=wiz.build_embed(), view=wiz, ephemeral=True)

    async def _toggle(self, interaction: discord.Interaction):
        turning_on = not self.data.get("cfg", {}).get("enabled")
        await interaction.response.defer()
        await sp.set_verification(interaction.guild_id, self.clone_id, interaction.user.id, enabled=turning_on)
        await self.reload(interaction)


# ── moderation ───────────────────────────────────────────────────────────

class BannedWordsModal(discord.ui.Modal, title="Add banned words"):
    words = discord.ui.TextInput(
        label="Words or phrases, one per line", style=discord.TextStyle.paragraph, max_length=1500)

    def __init__(self, opener_id: int):
        super().__init__()
        self.opener_id = opener_id

    async def on_submit(self, interaction: discord.Interaction):
        if interaction.user.id != self.opener_id or not await guard(interaction):
            return
        from database import db
        await interaction.response.defer()
        words = [w.strip().lower() for w in str(self.words.value).splitlines() if w.strip()][:100]
        cid = clone_id_of(interaction)
        added = await db.add_automod_banned_words_bulk(interaction.guild_id, words, clone_id=cid)
        await sp.record_change(interaction.guild_id, cid, interaction.user.id,
                               "automod.banned_words", None, f"+{added}")
        await interaction.followup.send(f"Added {added} word(s)/phrase(s).", ephemeral=True)
        await interaction.edit_original_response(view=await ModerationView.create(interaction))


class ModerationView(ServerPanelView):
    title = "🛡️ Moderation"

    @classmethod
    async def load(cls, interaction):
        from database import db
        cid = clone_id_of(interaction)
        import asyncio
        async def _stats():
            try:
                return await db.get_honeypot_stats(interaction.guild_id, cid)
            except Exception:
                return {}
        async def _antiraid():
            try:
                return await db.get_antiraid_config(interaction.guild_id, cid)
            except Exception:
                return {}
        am, hp, stats, ar = await asyncio.gather(
            db.get_automod_config(interaction.guild_id, cid),
            db.get_honeypot_config(interaction.guild_id, cid),
            _stats(),
            _antiraid(),
        )
        return {"am": am, "hp": hp, "hp_stats": stats, "ar": ar}

    def body(self):
        am, hp = self.data.get("am", {}), self.data.get("hp", {})
        lines = [f"{label}: {_onoff(am.get(key))}" for key, label in AUTOMOD_FILTERS]
        action = am.get("action", "delete")
        if action == "timeout":
            action += f" ({am.get('timeout_minutes', 10)} min)"
        lines += [
            f"Action: `{action}` · banned words: {len(am.get('banned_words') or [])}",
            f"Mod-log channel: {_chan(None, am.get('log_channel_id'))}",
            f"Honeypot: {_onoff(hp.get('channel_id') and hp.get('enabled'))} · {_chan(None, hp.get('channel_id'))}",
        ]
        ar = self.data.get("ar") or {}
        if ar.get("active_until") and ar["active_until"] > datetime.now(timezone.utc):
            lines.append(f"Anti-raid: 🚨 **RAID MODE ACTIVE** — ends <t:{int(ar['active_until'].timestamp())}:R>")
        else:
            lines.append(f"Anti-raid: {_onoff(ar.get('enabled'))}"
                         + (f" · {ar.get('sensitivity', 'balanced')} · {ar.get('response', 'lockdown')}"
                            if ar.get("enabled") else ""))
        if hp.get("channel_id"):
            st = self.data.get("hp_stats") or {}
            lines.append(f"Honeypot catches: {st.get('day', 0)} today · {st.get('week', 0)} this week · "
                         f"{st.get('month', 0)} this month · {st.get('total', hp.get('triggered_count') or 0)} all time")
            lines.append("Honeypot alert role: "
                         + (f"<@&{hp['alert_role_id']}>" if hp.get("alert_role_id") else "not set"))
        return lines

    def controls(self):
        am, hp = self.data.get("am", {}), self.data.get("hp", {})
        log = discord.ui.ChannelSelect(channel_types=[discord.ChannelType.text],
                                       placeholder="Mod-log channel", min_values=1, max_values=1)
        log.callback = self._pick_log
        act = discord.ui.Select(
            placeholder="Action when a filter triggers",
            options=[discord.SelectOption(label=a, value=a, default=a == am.get("action", "delete"))
                     for a in AUTOMOD_ACTIONS])
        act.callback = self._pick_action
        out: list = [log, act]
        if hp.get("channel_id"):
            alert = discord.ui.RoleSelect(placeholder="Honeypot alert role (pinged on every catch)",
                                          min_values=1, max_values=1)
            alert.callback = self._pick_alert_role
            out.append(alert)
        for key, label in AUTOMOD_FILTERS:
            on = bool(am.get(key))
            out.append(_btn(f"{label}: {'on' if on else 'off'}",
                            discord.ButtonStyle.success if on else discord.ButtonStyle.secondary,
                            self._toggle_filter(key)))
        out.append(_btn("Add banned words", discord.ButtonStyle.primary, self._words, "📝"))
        if hp.get("channel_id"):
            out.append(_btn("Honeypot: on" if hp.get("enabled") else "Honeypot: off",
                            discord.ButtonStyle.success if hp.get("enabled") else discord.ButtonStyle.secondary,
                            self._toggle_honeypot, "🍯"))
        out.append(_btn("Honeypot settings" if hp.get("channel_id") else "Set up honeypot",
                        discord.ButtonStyle.primary, self._open_honeypot, "🍯"))
        if hp.get("channel_id"):
            out.append(_btn("Test honeypot", discord.ButtonStyle.success, self._test_honeypot, "🧪"))
        out.append(_btn("Anti-raid" if not (self.data.get("ar") or {}).get("enabled") else "Anti-raid settings",
                        discord.ButtonStyle.primary, self._open_antiraid, "🛡️"))
        out.append(self.back_button())
        return out

    def _toggle_filter(self, key: str) -> Callable:
        async def cb(interaction: discord.Interaction):
            await interaction.response.defer()
            now = not self.data.get("am", {}).get(key)
            await sp.set_automod(interaction.guild_id, self.clone_id, interaction.user.id, **{key: now})
            await self.reload(interaction)
        return cb

    async def _pick_log(self, interaction: discord.Interaction):
        select = next(c for c in self.walk_children() if isinstance(c, discord.ui.ChannelSelect))
        await interaction.response.defer()
        await sp.set_automod(interaction.guild_id, self.clone_id, interaction.user.id,
                             log_channel_id=select.values[0].id)
        await self.reload(interaction)

    async def _pick_action(self, interaction: discord.Interaction):
        select = next(c for c in self.walk_children() if type(c) is discord.ui.Select)
        await interaction.response.defer()
        await sp.set_automod(interaction.guild_id, self.clone_id, interaction.user.id, action=select.values[0])
        await self.reload(interaction)

    async def _words(self, interaction: discord.Interaction):
        await interaction.response.send_modal(BannedWordsModal(interaction.user.id))

    async def _open_honeypot(self, interaction: discord.Interaction):
        """Full honeypot panel (creates the trap channel if none exists yet):
        action, history window, log channel, pause/repost/remove, premium extras."""
        from discord_bot.cogs.honeypot import open_honeypot
        await interaction.response.defer(ephemeral=True)
        await open_honeypot(interaction, interaction.guild, self.clone_id)

    async def _open_antiraid(self, interaction: discord.Interaction):
        """The anti-raid setup wizard (same screen as /antiraid)."""
        from discord_bot.cogs.antiraid import open_antiraid
        await interaction.response.defer(ephemeral=True)
        await open_antiraid(interaction, interaction.guild, self.clone_id)

    async def _pick_alert_role(self, interaction: discord.Interaction):
        select = next(c for c in self.walk_children() if isinstance(c, discord.ui.RoleSelect))
        role = interaction.guild.get_role(select.values[0].id)
        if role is None or role.is_default():
            await interaction.response.send_message("Pick a real role — @everyone can't be the alert role.",
                                                    ephemeral=True)
            return
        await interaction.response.defer()
        await sp.set_honeypot(interaction.guild_id, self.clone_id, interaction.user.id, alert_role_id=role.id)
        from discord_bot.cogs.honeypot import _invalidate
        _invalidate(interaction.guild_id, self.clone_id)
        await self.reload(interaction)

    async def _test_honeypot(self, interaction: discord.Interaction):
        """Sample alert (with the real role ping) — nobody is actioned, stats unchanged."""
        from discord_bot.cogs.honeypot import send_test_alert
        await interaction.response.defer(ephemeral=True)
        ok, msg = await send_test_alert(interaction.client, interaction.guild, interaction.user)
        await interaction.followup.send(("✅ " if ok else "⚠️ ") + msg, ephemeral=True,
                                        allowed_mentions=discord.AllowedMentions.none())

    async def _toggle_honeypot(self, interaction: discord.Interaction):
        await interaction.response.defer()
        now = not self.data.get("hp", {}).get("enabled")
        await sp.set_honeypot(interaction.guild_id, self.clone_id, interaction.user.id, enabled=now)
        await self.reload(interaction)


# ── entry point ──────────────────────────────────────────────────────────

async def open_home(interaction: discord.Interaction) -> None:
    """Called by /serversetup. Sends the Home screen as a new ephemeral message."""
    if not await guard(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    import asyncio
    # best effort, feeds owner Health; runs alongside building the screen
    _, view = await asyncio.gather(
        sp.record_open(interaction.guild_id, clone_id_of(interaction)),
        HomeView.create(interaction),
    )
    await interaction.followup.send(view=view, ephemeral=True)


async def open_inspect(interaction: discord.Interaction, guild: discord.Guild,
                       back: Callable[[discord.Interaction], Awaitable[None]]) -> None:
    """Owner Server inspector -> this server's panel, READ-ONLY. The caller has
    already checked the owner's 'inspect' access and that the main bot is in
    `guild`. Edits the owner's current message in place."""
    await interaction.response.defer()
    ctx = InspectContext(guild=guild, back=back)
    view = await HomeView.create(InspectInteraction(interaction, ctx))
    await interaction.edit_original_response(view=view)
