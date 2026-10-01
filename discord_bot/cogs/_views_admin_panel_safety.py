"""
Owner panel, Batch 3 — Abuse watchlist, Report queue, Status editor and
Honeypot overview.

Same rules as the rest of the panel (see _views_admin_panel.py): buttons,
selects and forms only (no new slash commands), only the person who opened the
panel can use it, access is re-checked on every click and modal submit, and
every state-changing action writes an audit line.

All four sections are OWNER-ONLY (none is in admin_controls.GRANTABLE): they
can blacklist servers/users or change what the bot shows publicly.

* Watchlist blacklists with one press after picking a row (reversible from the
  Blacklist screen). Report-queue "Blacklist server" is two-step.
* Removing a single status entry is immediate; resetting the whole rotation is two-step.
* Honeypot is read-only.
"""

from __future__ import annotations

import logging
from typing import List, Optional

import discord

from discord_bot.cogs import _views_admin_panel as main
from discord_bot.cogs._views_admin_panel import PANEL_TIMEOUT, PanelView, _btn, allowed_sections, audit
from discord_bot.cogs._views_admin_panel_controls import _clip, _denied, _fit, _home, _ts
from modules import admin_controls as ac
from modules import admin_safety as safety

logger = logging.getLogger(__name__)

WATCHLIST = "watchlist"
REPORTS = "reports"
STATUS = "status"
HONEYPOT = "honeypot"

NOTHING_CHANGED = "Couldn't save that (database problem). Nothing was changed."


def _safe(text: Optional[str]) -> str:
    """Untrusted text (report reasons, guild names): no markdown, no pings."""
    return discord.utils.escape_mentions(discord.utils.escape_markdown(text or ""))


def _guild_label(cog, guild_id: int) -> str:
    try:
        g = cog.bot.get_guild(guild_id)
    except Exception:
        g = None
    return f"{_clip(_safe(g.name), 40)} (`{guild_id}`)" if g is not None else f"`{guild_id}`"


class _SafetyView(PanelView):
    notice: Optional[str] = None

    async def _edit(self, i: discord.Interaction) -> None:
        self._build()
        await i.response.edit_message(view=self)

    async def _back(self, i: discord.Interaction) -> None:
        await _home(self, i)


# ── abuse watchlist ──────────────────────────────────────────────────────

class WatchlistView(_SafetyView):
    title = "🕵️ Abuse watchlist"

    def __init__(self, cog, owner_id, section=WATCHLIST):
        self.kind = "user"          # "user" | "guild"
        self.days = safety.WATCH_WINDOWS[0]
        self.rows: List[dict] = []
        self.selected: Optional[int] = None
        self.notice = None
        self.error = False
        super().__init__(cog, owner_id, section)

    async def load(self) -> None:
        try:
            self.rows = await safety.watchlist(self.kind, self.days)
            self.error = False
        except Exception:
            logger.exception("[admin-panel] couldn't load the watchlist")
            self.rows, self.error = [], True
        if self.selected not in {r["target_id"] for r in self.rows}:
            self.selected = None
        self._build()

    def body(self):
        what = "people" if self.kind == "user" else "servers"
        lines = [f"Top {what} by **auto-mod actions** in the last {self.days} days "
                 "(deleted/warned/timed-out/kicked by the auto-mod filters, main bot and clones).",
                 "-# Rate-limit hits aren't recorded anywhere, so they can't appear here."]
        if self.notice:
            lines.append(self.notice)
        if self.error:
            lines.append("⚠️ Couldn't load the list right now. Press Refresh.")
        elif not self.rows:
            lines.append("Nothing to show for this window.")
        else:
            entries = []
            for n, r in enumerate(self.rows, 1):
                who = f"`{r['target_id']}`" if self.kind == "user" else _guild_label(self.cog, r["target_id"])
                extra = f" · {r['guilds']} server(s)" if self.kind == "user" else ""
                entries.append(f"**{n}.** {who} — {r['hits']} actions{extra} · last {_ts(r['last_at'])}")
            lines += _fit(entries)
        return lines

    def controls(self):
        S, D = discord.ButtonStyle.secondary, discord.ButtonStyle.danger
        items: list = []
        if self.rows:
            sel = discord.ui.Select(
                placeholder="Pick one to blacklist",
                options=[discord.SelectOption(
                    label=f"{n}. {'User' if self.kind == 'user' else 'Server'} {r['target_id']}"[:100],
                    description=f"{r['hits']} actions"[:100], value=str(r["target_id"]),
                    default=r["target_id"] == self.selected)
                    for n, r in enumerate(self.rows[:25], 1)])
            sel.callback = self._pick
            items.append(sel)
        items += [
            _btn("Show servers" if self.kind == "user" else "Show people", S, self._toggle_kind, "🔀"),
            _btn("30 days" if self.days == 7 else "7 days", S, self._toggle_days, "📅"),
            _btn("Add to blacklist", D, self._blacklist, "🚫", disabled=self.selected is None),
            _btn("Refresh", S, self._refresh, "🔄"),
            _btn("Back", S, self._back, "⬅️"),
        ]
        return items

    async def _pick(self, i: discord.Interaction):
        self.selected = int(i.data["values"][0])
        self.notice = None
        await self._edit(i)

    async def _toggle_kind(self, i: discord.Interaction):
        self.kind = "guild" if self.kind == "user" else "user"
        self.selected, self.notice = None, None
        await self.load()
        await i.response.edit_message(view=self)

    async def _toggle_days(self, i: discord.Interaction):
        self.days = 30 if self.days == 7 else 7
        self.selected, self.notice = None, None
        await self.load()
        await i.response.edit_message(view=self)

    async def _refresh(self, i: discord.Interaction):
        self.notice = None
        await self.load()
        await i.response.edit_message(view=self)

    async def _blacklist(self, i: discord.Interaction):
        target = self.selected
        row = next((r for r in self.rows if r["target_id"] == target), None)
        if target is None or row is None:
            self.notice = "Pick a row first."
            await self._edit(i)
            return
        if self.kind == "user" and target in main.DISCORD_CLONE_ADMIN_IDS:
            self.notice = "You can't block a bot owner."
            await self._edit(i)
            return
        reason = f"Abuse watchlist: {row['hits']} auto-mod actions in {self.days} days"
        try:
            await ac.add_blacklist(self.kind, target, reason, i.user.id)
        except Exception:
            logger.exception("[admin-panel] couldn't blacklist from the watchlist")
            self.notice = f"⚠️ {NOTHING_CHANGED}"
            await self._edit(i)
            return
        audit(i, "watchlist.blacklist", kind=self.kind, target=target, hits=row["hits"], days=self.days)
        self.selected = None
        self.notice = f"✅ Blocked {self.kind} `{target}`. Undo it from the Blacklist screen."
        await self._edit(i)


# ── report queue ─────────────────────────────────────────────────────────

class ReportsView(_SafetyView):
    title = "🚩 Report queue"

    def __init__(self, cog, owner_id, section=REPORTS):
        self.rows: List[dict] = []
        self.counts: dict = {}
        self.selected: Optional[int] = None
        self._confirm = False
        self.notice = None
        self.error = False
        super().__init__(cog, owner_id, section)

    async def load(self) -> None:
        try:
            self.rows = await safety.list_new_reports(10)
            self.counts = await safety.report_counts()
            self.error = False
        except Exception:
            logger.exception("[admin-panel] couldn't load reports")
            self.rows, self.counts, self.error = [], {}, True
        if self.selected not in {r["id"] for r in self.rows}:
            self.selected = None
        self._confirm = False
        self._build()

    def _selected_row(self) -> Optional[dict]:
        return next((r for r in self.rows if r["id"] == self.selected), None)

    def body(self):
        c = self.counts
        lines = ["Public \"report this server\" submissions waiting for review (newest first).",
                 f"-# New {c.get('new', 0)} · reviewed {c.get('reviewed', 0)} · dismissed {c.get('dismissed', 0)}"]
        if self.notice:
            lines.append(self.notice)
        if self.error:
            lines.append("⚠️ Couldn't load reports right now. Press Refresh.")
        elif not self.rows:
            lines.append("Queue is empty. 🎉")
        else:
            entries = [f"**#{r['id']}** {_guild_label(self.cog, r['guild_id'])} · {_ts(r['created_at'])}\n"
                       f"> {_clip(_safe(r['reason']), 160)}" for r in self.rows]
            lines += _fit(entries)
        if self._confirm and self._selected_row():
            r = self._selected_row()
            lines.append(f"⚠️ **Press Confirm to blacklist server `{r['guild_id']}`** — every command is refused there, "
                         "on the main bot and all clones. The report is marked reviewed.")
        return lines

    def controls(self):
        S, G, D = discord.ButtonStyle.secondary, discord.ButtonStyle.success, discord.ButtonStyle.danger
        items: list = []
        has = self.selected is not None
        if self.rows:
            sel = discord.ui.Select(
                placeholder="Pick a report",
                options=[discord.SelectOption(
                    label=f"#{r['id']} · server {r['guild_id']}"[:100],
                    description=_clip(_safe(r["reason"]), 100) or None, value=str(r["id"]),
                    default=r["id"] == self.selected) for r in self.rows])
            sel.callback = self._pick
            items.append(sel)
        items += [
            _btn("Mark reviewed", G, self._reviewed, "✅", disabled=not has),
            _btn("Dismiss", S, self._dismiss, "🗑️", disabled=not has),
            _btn("Confirm blacklist" if self._confirm else "Blacklist server", D, self._blacklist, "🚫", disabled=not has),
            _btn("Refresh", S, self._refresh, "🔄"),
            _btn("Back", S, self._back, "⬅️"),
        ]
        return items

    async def _pick(self, i: discord.Interaction):
        self.selected = int(i.data["values"][0])
        self._confirm, self.notice = False, None
        await self._edit(i)

    async def _refresh(self, i: discord.Interaction):
        self.notice = None
        await self.load()
        await i.response.edit_message(view=self)

    async def _resolve(self, i: discord.Interaction, status: str, label: str):
        self._confirm = False
        rid = self.selected
        if rid is None:
            self.notice = "Pick a report first."
            await self._edit(i)
            return
        try:
            changed = await safety.resolve_report(rid, status, i.user.id)
        except Exception:
            logger.exception("[admin-panel] couldn't update report %s", rid)
            self.notice = f"⚠️ {NOTHING_CHANGED}"
            await self._edit(i)
            return
        if changed:
            audit(i, f"report.{status}", report=rid)
            self.notice = f"✅ Report #{rid} {label}."
        else:
            self.notice = f"Report #{rid} was already handled."
        self.selected = None
        await self.load()
        await i.response.edit_message(view=self)

    async def _reviewed(self, i): await self._resolve(i, safety.REPORT_REVIEWED, "marked reviewed")
    async def _dismiss(self, i): await self._resolve(i, safety.REPORT_DISMISSED, "dismissed")

    async def _blacklist(self, i: discord.Interaction):
        row = self._selected_row()
        if row is None:
            self._confirm = False
            self.notice = "Pick a report first."
            await self._edit(i)
            return
        if not self._confirm:
            self._confirm, self.notice = True, None
            await self._edit(i)
            return
        self._confirm = False
        gid, rid = row["guild_id"], row["id"]
        try:
            await ac.add_blacklist("guild", gid, f"Reported via directory (report #{rid})", i.user.id)
        except Exception:
            logger.exception("[admin-panel] couldn't blacklist reported server")
            self.notice = f"⚠️ {NOTHING_CHANGED}"
            await self._edit(i)
            return
        audit(i, "report.blacklist", report=rid, guild=gid)
        note = f"✅ Blocked server `{gid}` and marked report #{rid} reviewed."
        try:
            await safety.resolve_report(rid, safety.REPORT_REVIEWED, i.user.id)
        except Exception:
            logger.exception("[admin-panel] blacklisted but couldn't mark report %s", rid)
            note = f"✅ Blocked server `{gid}`, but report #{rid} could not be marked reviewed — dismiss it manually."
        self.selected, self.notice = None, note
        await self.load()
        await i.response.edit_message(view=self)


# ── status editor ────────────────────────────────────────────────────────

class StatusAddModal(discord.ui.Modal, title="Add status text"):
    def __init__(self, cog, kind: str):
        super().__init__(timeout=PANEL_TIMEOUT)
        self.cog, self.kind = cog, kind
        self.text = discord.ui.TextInput(
            label=f"{safety.STATUS_KINDS.get(kind, kind)} …", max_length=safety.STATUS_TEXT_MAX,
            placeholder="e.g. {members} members  ({members} is filled in live)")
        self.add_item(self.text)

    async def on_submit(self, interaction: discord.Interaction):
        if STATUS not in allowed_sections(interaction.user.id):   # modals skip interaction_check
            await _denied(interaction)
            return
        text = safety.clean_status_text(self.text.value)
        if not text:
            await interaction.response.send_message("The status text can't be empty.", ephemeral=True)
            return
        try:
            added = await safety.add_status_entry(self.kind, text, interaction.user.id)
        except Exception:
            logger.exception("[admin-panel] couldn't add status entry")
            await interaction.response.send_message(NOTHING_CHANGED, ephemeral=True)
            return
        view = StatusView(self.cog, interaction.user.id)
        view.new_kind = self.kind
        if added:
            audit(interaction, "status.add", kind=self.kind, text=text)
            view.notice = "✅ Added."
        else:
            view.notice = f"⚠️ The list is full ({safety.STATUS_MAX_ENTRIES}). Remove one first."
        await view.load()
        await interaction.response.edit_message(view=view)


class StatusView(_SafetyView):
    title = "🎭 Status editor"

    def __init__(self, cog, owner_id, section=STATUS):
        self.entries: List[dict] = []
        self.presence = "online"
        self.new_kind = "playing"
        self.selected: Optional[int] = None
        self._confirm = False
        self.notice = None
        self.error = False
        super().__init__(cog, owner_id, section)

    async def load(self) -> None:
        try:
            self.entries = await safety.list_status_entries()
            self.presence = await safety.get_presence()
            self.error = False
        except Exception:
            logger.exception("[admin-panel] couldn't load status settings")
            self.entries, self.presence, self.error = [], "online", True
        if self.selected not in {e["id"] for e in self.entries}:
            self.selected = None
        self._confirm = False
        self._build()

    def _counts(self):
        try:
            guilds = list(self.cog.bot.guilds)
            return len(guilds), sum((g.member_count or 0) for g in guilds)
        except Exception:
            return 0, 0

    def body(self):
        lines = ["Replace the text under the bot's name. Changes reach the main bot and every clone within "
                 "about 90 seconds (no restart). Presence: "
                 f"**{safety.PRESENCES.get(self.presence, 'Online')}**."]
        if self.notice:
            lines.append(self.notice)
        if self.error:
            lines.append("⚠️ Couldn't load the settings right now.")
        elif not self.entries:
            lines.append("No custom entries — the **built-in rotation** is running.")
        else:
            servers, members = self._counts()
            lines.append("**Preview** (what people will see, using current counts; entries rotate every 30s):")
            entries = [f"{n}. {safety.STATUS_KINDS[e['kind']]} {_clip(_safe(safety.render_status_text(e['text'], servers, members)), 80)}"
                       for n, e in enumerate(self.entries, 1)]
            lines += _fit(entries, 2400)
            lines.append("-# Custom entries replace the built-in ones until you reset.")
        if self._confirm:
            lines.append("⚠️ **Press Confirm to remove every custom entry** and go back to the built-in rotation.")
        return lines

    def controls(self):
        S, P, D = discord.ButtonStyle.secondary, discord.ButtonStyle.primary, discord.ButtonStyle.danger
        kind_sel = discord.ui.Select(
            placeholder="Type for the next entry",
            options=[discord.SelectOption(label=label, value=k, default=k == self.new_kind)
                     for k, label in safety.STATUS_KINDS.items()])
        kind_sel.callback = self._pick_kind
        pres_sel = discord.ui.Select(
            placeholder="Presence dot",
            options=[discord.SelectOption(label=label, value=k, default=k == self.presence)
                     for k, label in safety.PRESENCES.items()])
        pres_sel.callback = self._pick_presence
        items: list = [kind_sel, pres_sel]
        if self.entries:
            sel = discord.ui.Select(
                placeholder="Pick an entry to remove",
                options=[discord.SelectOption(
                    label=f"{n}. {safety.STATUS_KINDS[e['kind']]} {_clip(_safe(e['text']), 70)}"[:100],
                    value=str(e["id"]), default=e["id"] == self.selected)
                    for n, e in enumerate(self.entries[:25], 1)])
            sel.callback = self._pick_entry
            items.append(sel)
        can_reset = bool(self.entries) or self.presence != "online"
        items += [
            _btn("Add entry", P, self._add, "➕", disabled=len(self.entries) >= safety.STATUS_MAX_ENTRIES),
            _btn("Remove selected", S, self._remove, "➖", disabled=self.selected is None),
            _btn("Confirm reset" if self._confirm else "Reset to default", D, self._reset, "♻️", disabled=not can_reset),
            _btn("Back", S, self._back, "⬅️"),
        ]
        return items

    async def _pick_kind(self, i: discord.Interaction):
        self.new_kind = i.data["values"][0]
        self._confirm = False
        await self._edit(i)

    async def _pick_entry(self, i: discord.Interaction):
        self.selected = int(i.data["values"][0])
        self._confirm = False
        await self._edit(i)

    async def _pick_presence(self, i: discord.Interaction):
        value = i.data["values"][0]
        self._confirm = False
        try:
            await safety.set_presence(value, i.user.id)
        except Exception:
            logger.exception("[admin-panel] couldn't set presence")
            self.notice = f"⚠️ {NOTHING_CHANGED}"
            await self._edit(i)
            return
        audit(i, "status.presence", presence=value)
        self.presence, self.notice = value, "✅ Presence updated."
        await self._edit(i)

    async def _add(self, i: discord.Interaction):
        self._confirm = False
        await i.response.send_modal(StatusAddModal(self.cog, self.new_kind))

    async def _remove(self, i: discord.Interaction):
        self._confirm = False
        eid = self.selected
        entry = next((e for e in self.entries if e["id"] == eid), None)
        if eid is None or entry is None:
            self.notice = "Pick an entry first."
            await self._edit(i)
            return
        try:
            await safety.remove_status_entry(eid)
        except Exception:
            logger.exception("[admin-panel] couldn't remove status entry")
            self.notice = f"⚠️ {NOTHING_CHANGED}"
            await self._edit(i)
            return
        audit(i, "status.remove", entry=eid, text=entry["text"])
        self.selected, self.notice = None, "✅ Removed."
        await self.load()
        await i.response.edit_message(view=self)

    async def _reset(self, i: discord.Interaction):
        if not self._confirm:
            self._confirm, self.notice = True, None
            await self._edit(i)
            return
        self._confirm = False
        try:
            await safety.reset_status(i.user.id)
        except Exception:
            logger.exception("[admin-panel] couldn't reset status")
            self.notice = f"⚠️ {NOTHING_CHANGED}"
            await self._edit(i)
            return
        audit(i, "status.reset", removed=len(self.entries))
        self.selected, self.notice = None, "✅ Back to the built-in rotation."
        await self.load()
        await i.response.edit_message(view=self)


# ── honeypot overview (read-only) ────────────────────────────────────────

class HoneypotView(_SafetyView):
    title = "🍯 Honeypot overview"

    def __init__(self, cog, owner_id, section=HONEYPOT):
        self.totals: dict = {}
        self.rows: List[dict] = []
        self.error = False
        super().__init__(cog, owner_id, section)

    async def load(self) -> None:
        try:
            self.totals, self.rows = await safety.honeypot_overview(15)
            self.error = False
        except Exception:
            logger.exception("[admin-panel] couldn't load honeypot overview")
            self.totals, self.rows, self.error = {}, [], True
        self._build()

    def body(self):
        lines = ["Read-only. Only counters are stored, so individual catches can't be listed."]
        if self.error:
            lines.append("⚠️ Couldn't load right now. Press Refresh.")
            return lines
        t = self.totals
        lines.append(f"**{t.get('enabled', 0)}** on of **{t.get('configured', 0)}** configured · "
                     f"**{t.get('triggers', 0)}** total catches")
        if not self.rows:
            lines.append("No server has set up a honeypot yet.")
        else:
            entries = []
            for r in self.rows:
                where = _guild_label(self.cog, r["guild_id"]) + (f" · clone #{r['clone_id']}" if r.get("clone_id") is not None else "")
                last = _ts(r["last_triggered_at"]) if r.get("last_triggered_at") else "never"
                entries.append(f"{'🟢' if r['enabled'] else '⚪'} {where} — {r['action']} · "
                               f"{r['triggered_count']} catches · last {last}")
            lines += _fit(entries)
        return lines

    def controls(self):
        S = discord.ButtonStyle.secondary
        return [_btn("Refresh", S, self._refresh, "🔄"), _btn("Back", S, self._back, "⬅️")]

    async def _refresh(self, i: discord.Interaction):
        await self.load()
        await i.response.edit_message(view=self)
