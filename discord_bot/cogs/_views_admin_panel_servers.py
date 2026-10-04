"""
Owner panel, Phase 3 — Servers, Find and Clones.

Covers: server list, server / person search, force-activating monetization on
a clone, commissions, subscribers, and clone management.

Same rules as Phase 1 (see _views_admin_panel.py): this is a thin UI shell.
Every action runs the SAME coroutine the matching `/admin ...` slash command
runs (via `call_cmd`), so auth checks, DB reads/writes and replies are
identical and there is no second copy of the logic. Only the person who
opened the panel can use it, access is re-checked on every click, and the
one action that changes state (force-activate monetization) is two-step.
"""

from __future__ import annotations

import logging
from typing import List, Optional

import discord

from database import db
from discord_bot.cogs._views_admin_panel import (
    PANEL_TIMEOUT,
    PanelView,
    _btn,
    allowed_sections,
    audit,
    call_cmd,
)

logger = logging.getLogger(__name__)

SECTION = "servers"
MONETIZE_DAYS = 30  # only used in the confirm text; the real value lives in config.CLONE_MONETIZATION_DAYS


def _bot_label(row: dict) -> str:
    if row.get("clone_id") is None:
        return "Main bot"
    return f"Clone #{row['clone_id']} ({row.get('bot_username') or 'unknown'})"


async def _module_missing(i: discord.Interaction, what: str) -> None:
    msg = f"{what} isn't loaded right now."
    if i.response.is_done():
        await i.followup.send(msg, ephemeral=True)
    else:
        await i.response.send_message(msg, ephemeral=True)


# ── hub ──────────────────────────────────────────────────────────────────

class FindModal(discord.ui.Modal, title="Find a server or person"):
    """One box: a name, or a pasted server/user ID. IDs go straight to /admin
    find; names show a pick-list first (the typed command does this through
    autocomplete, which a form can't)."""

    def __init__(self, cog):
        super().__init__(timeout=PANEL_TIMEOUT)
        self.cog = cog
        self.query = discord.ui.TextInput(
            label="Server name, person's name, or an ID", max_length=100,
            placeholder="e.g. Anime Hub, somebody, 123456789012345678")
        self.add_item(self.query)

    async def on_submit(self, interaction: discord.Interaction):
        # Modals bypass the view's interaction_check, and this searches the DB
        # directly, so re-check here.
        if SECTION not in allowed_sections(interaction.user.id):
            await interaction.response.send_message("You're no longer authorized for this.", ephemeral=True)
            return
        lookup = self.cog.lookup
        if lookup is None:
            await _module_missing(interaction, "Lookup")
            return
        q = self.query.value.strip()
        if not q:
            await interaction.response.send_message("Type a name or paste an ID.", ephemeral=True)
            return
        audit(interaction, "find.search", query=q)

        if q.isdigit():  # an ID: /admin find resolves guild vs user itself
            await call_cmd(lookup, "find", interaction, query=q)
            return

        guilds = await db.search_discord_guilds(q, limit=12)
        people = await db.search_cached_usernames(q, limit=12)
        if not guilds and not people:
            await interaction.response.send_message(f"Nothing matching **{q}** found.", ephemeral=True)
            return
        view = FindResultsView(self.cog, interaction.user.id, SECTION, q, guilds, people)
        await interaction.response.send_message(view=view, ephemeral=True)


class FindResultsView(PanelView):
    title = "🔎 Search results"

    def __init__(self, cog, owner_id, section, query: str, guilds: List[dict], people: List[dict]):
        self.query = query
        self.guilds = guilds[:12]
        self.people = people[:12]
        super().__init__(cog, owner_id, section)

    def body(self):
        return [f"Matches for **{self.query}** — {len(self.guilds)} server(s), {len(self.people)} person(s). "
                "Pick one to open its full card."]

    def controls(self):
        opts = []
        for r in self.guilds:
            label = f"🏠 {r.get('guild_name') or 'Unknown'} — {_bot_label(r)} ({r.get('member_count') or '?'} members)"
            opts.append(discord.SelectOption(label=label[:100], value=f"g:{r['guild_id']}"))
        for r in self.people:
            opts.append(discord.SelectOption(label=f"🧑 {r['username']} ({r['user_id']})"[:100],
                                             value=f"u:{r['user_id']}"))
        sel = discord.ui.Select(placeholder="Open a result…", options=opts[:25])
        sel.callback = self._pick
        return [sel]

    async def _pick(self, i: discord.Interaction):
        lookup = self.cog.lookup
        if lookup is None:
            await _module_missing(i, "Lookup")
            return
        value = i.data["values"][0]
        audit(i, "find.open", target=value)
        # /admin find accepts the same "g:<id>" / "u:<id>" values autocomplete produces.
        await call_cmd(lookup, "find", i, query=value)


class ServersHubView(PanelView):
    title = "🏠 Servers & clones"

    def __init__(self, cog, owner_id, section=SECTION):
        self.include_left = False
        super().__init__(cog, owner_id, section)

    def body(self):
        return ["Look up servers and people, manage clones, and check money and subscribers.",
                f"**Server list:** {'including' if self.include_left else 'hiding'} servers the bots have left.",
                "-# Each button runs the same command as its `/admin ...` slash command."]

    def controls(self):
        P, S = discord.ButtonStyle.primary, discord.ButtonStyle.secondary
        return [
            _btn("All servers", P, self._servers, "🏠"),
            _btn("Join dates", P, self._guilds, "📅"),
            _btn("Include left: on" if self.include_left else "Include left: off", S, self._toggle_left, "🚪"),
            _btn("Find server / person", P, self._find, "🔎"),
            _btn("Clones", P, self._clones, "🤖"),
            _btn("Commissions", S, self._commissions, "💰"),
            _btn("Subscribers", S, self._subscribers, "👥"),
            _btn("Scam Shield", P, self._scamshield, "🛡️", disabled="scamshield" not in allowed_sections(self.owner_id)),
            _btn("Back", S, self._back, "⬅️"),
        ]

    async def _scamshield(self, i: discord.Interaction):
        from discord_bot.cogs._views_admin_panel_scamshield import ScamShieldView  # lazy: avoids import cycle
        view = ScamShieldView(self.cog, self.owner_id)
        await view.load()
        await self.go(i, view)

    async def _servers(self, i: discord.Interaction):
        clone = self.cog.clone_admin
        if clone is None:
            await _module_missing(i, "Servers module")
            return
        audit(i, "servers.list", include_left=self.include_left)
        await call_cmd(clone, "allservers", i, include_left=self.include_left)

    async def _guilds(self, i: discord.Interaction):
        admin = self.cog.admin_cog
        if admin is None:
            await _module_missing(i, "Admin module")
            return
        audit(i, "guilds.list")
        await call_cmd(admin, "guilds", i)

    async def _toggle_left(self, i: discord.Interaction):
        self.include_left = not self.include_left
        self._build()
        await i.response.edit_message(view=self)

    async def _find(self, i: discord.Interaction):
        await i.response.send_modal(FindModal(self.cog))

    async def _clones(self, i: discord.Interaction):
        clones = await db.list_active_discord_clones()
        await self.go(i, ClonesView(self.cog, self.owner_id, SECTION, clones))

    async def _commissions(self, i: discord.Interaction):
        admin = self.cog.admin_cog
        if admin is None:
            await _module_missing(i, "Admin module")
            return
        audit(i, "commissions.view")
        await call_cmd(admin, "commissions", i)

    async def _subscribers(self, i: discord.Interaction):
        admin = self.cog.admin_cog
        if admin is None:
            await _module_missing(i, "Admin module")
            return
        audit(i, "subscribers.view")
        await call_cmd(admin, "subscribers", i)

    async def _back(self, i: discord.Interaction):
        from discord_bot.cogs._views_admin_panel import HomeView
        await self.go(i, HomeView(self.cog, self.owner_id))


# ── clones ───────────────────────────────────────────────────────────────

class ClonesView(PanelView):
    title = "🤖 Clones"

    def __init__(self, cog, owner_id, section, clones: List[dict]):
        self.clones = clones[:25]  # select menu limit
        self.total = len(clones)
        self.selected: Optional[int] = None
        self.server_count: Optional[int] = None
        self._confirm = False
        super().__init__(cog, owner_id, section)

    def _current(self) -> Optional[dict]:
        return next((c for c in self.clones if c["clone_id"] == self.selected), None)

    def body(self):
        if not self.clones:
            return ["No active clones right now."]
        lines = [f"**{self.total}** active clone(s)."]
        if self.total > len(self.clones):
            lines.append(f"-# Showing the first {len(self.clones)} in the picker; use *Manage / deactivate* to page through all.")
        c = self._current()
        if c is None:
            lines.append("Pick a clone to act on it.")
        else:
            lines += [f"**Selected:** `#{c['clone_id']}` — @{c.get('bot_username') or 'unknown'}",
                      f"**Owner:** <@{c['owner_id']}>"]
            if self.server_count is not None:
                lines.append(f"**Servers:** {self.server_count}")
        if self._confirm and c is not None:
            lines.append(f"⚠️ **Press Confirm to force-activate monetization on `#{c['clone_id']}` "
                         f"for {MONETIZE_DAYS} days with NO payment taken.**")
        return lines

    def controls(self):
        S, G, D = discord.ButtonStyle.secondary, discord.ButtonStyle.success, discord.ButtonStyle.danger
        items: list = []
        if self.clones:
            sel = discord.ui.Select(
                placeholder="Choose a clone",
                options=[discord.SelectOption(
                    label=f"Clone #{c['clone_id']} — {c.get('bot_username') or 'unknown'}"[:100],
                    value=str(c["clone_id"]), default=c["clone_id"] == self.selected)
                    for c in self.clones])
            sel.callback = self._pick
            items.append(sel)
        has = self._current() is not None
        items += [
            _btn("Confirm activation" if self._confirm else "Force-activate monetization",
                 G if self._confirm else D, self._monetize, "✅" if self._confirm else "⚡",
                 disabled=not has),
            _btn("Manage / deactivate", S, self._manage, "🛠️", disabled=not self.clones),
            _btn("Back", S, self._back, "⬅️"),
        ]
        return items

    async def _pick(self, i: discord.Interaction):
        self.selected = int(i.data["values"][0])
        self._confirm = False
        try:
            self.server_count = await db.get_discord_guild_count(self.selected)
        except Exception:
            logger.exception("[admin-panel] couldn't load server count for clone %s", self.selected)
            self.server_count = None
        self._build()
        await i.response.edit_message(view=self)

    async def _monetize(self, i: discord.Interaction):
        c = self._current()
        if c is None:
            await i.response.send_message("Pick a clone first.", ephemeral=True)
            return
        if not self._confirm:
            self._confirm = True
            self._build()
            await i.response.edit_message(view=self)
            return
        clone = self.cog.clone_admin
        if clone is None:
            await _module_missing(i, "Monetization module")
            return
        audit(i, "monetize.force", clone_id=c["clone_id"])
        self._confirm = False
        await call_cmd(clone, "ownermonetize", i, clone_id=c["clone_id"])  # defers + replies via followup
        await self.soft_refresh(i)                                          # reset the Confirm button

    async def _manage(self, i: discord.Interaction):
        admin = self.cog.admin_cog
        if admin is None:
            await _module_missing(i, "Admin module")
            return
        audit(i, "clones.manage")
        # Opens the existing paged clone card (Prev / Next / Deactivate) as its own ephemeral message.
        await call_cmd(admin, "clones", i)

    async def _back(self, i: discord.Interaction):
        await self.go(i, ServersHubView(self.cog, self.owner_id, SECTION))
