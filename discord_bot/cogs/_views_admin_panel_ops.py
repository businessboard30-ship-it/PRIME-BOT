"""
Owner panel, Batch 2 — Panel access, Log tail, Config viewer and Database tools.

Same rules as the rest of the panel (see _views_admin_panel.py): buttons,
selects and forms only (no new slash commands), only the person who opened the
panel can use it, access is re-checked on every click and modal submit, and
every state-changing action writes an audit line.

* Panel access, Config and Database are OWNER-ONLY (they are not in
  admin_controls.GRANTABLE, so a helper can never be given them).
* Log tail can be granted to a helper; its text is masked (modules/admin_ops).
* Removing a helper and running the payment cleanup are two-step.
"""

from __future__ import annotations

import io
import logging
from typing import List, Optional, Set

import discord

from discord_bot.cogs import _views_admin_panel as main
from discord_bot.cogs._views_admin_panel import PANEL_TIMEOUT, PanelView, _btn, allowed_sections, audit
from discord_bot.cogs._views_admin_panel_controls import TEXT_BUDGET, _denied, _fit, _home, _ts
from modules import admin_controls as ac
from modules import admin_ops as ops

logger = logging.getLogger(__name__)

ACCESS = "access"
LOGS = "logs"
CONFIG = "config"
DATABASE = "database"

CONFIG_PAGE = 12


def _valid_id(raw: str) -> Optional[int]:
    raw = (raw or "").strip()
    return int(raw) if raw.isdigit() and 10 <= len(raw) <= 20 else None


# ── panel access ─────────────────────────────────────────────────────────

class AddHelperModal(discord.ui.Modal, title="Add a helper"):
    def __init__(self, cog):
        super().__init__(timeout=PANEL_TIMEOUT)
        self.cog = cog
        self.target = discord.ui.TextInput(label="Discord user ID", max_length=25,
                                           placeholder="e.g. 123456789012345678")
        self.add_item(self.target)

    async def on_submit(self, interaction: discord.Interaction):
        if ACCESS not in allowed_sections(interaction.user.id):   # modals skip interaction_check
            await _denied(interaction)
            return
        uid = _valid_id(self.target.value)
        if uid is None:
            await interaction.response.send_message(
                "That doesn't look like a Discord ID. It should be a long number (enable Developer Mode, "
                "right-click, **Copy ID**).", ephemeral=True)
            return
        if uid in main.DISCORD_CLONE_ADMIN_IDS:
            await interaction.response.send_message(
                "That account is already an owner (set in config) and has full access.", ephemeral=True)
            return
        current = ac.helper_sections(uid)
        view = HelperEditView(self.cog, interaction.user.id, uid, current, existing=bool(current))
        await interaction.response.edit_message(view=view)


class AccessView(PanelView):
    title = "🔑 Panel access"

    def __init__(self, cog, owner_id, section=ACCESS):
        self.helpers: List[dict] = []
        self.notice: Optional[str] = None
        self.error = False
        super().__init__(cog, owner_id, section)

    async def load(self) -> None:
        try:
            self.helpers = await ac.list_helpers(24)
            self.error = False
        except Exception:
            logger.exception("[admin-panel] couldn't load helpers")
            self.helpers, self.error = [], True
        self._build()

    def body(self):
        owners = " ".join(f"<@{u}>" for u in sorted(main.DISCORD_CLONE_ADMIN_IDS)) or "none"
        lines = [f"**Owners** (set in config, can't be changed here): {owners}",
                 "**Helpers** get only the sections you tick. Payments, broadcast, servers, bump, feedback and "
                 "system stay owner-only, because those screens run commands that check the owner list themselves."]
        if self.notice:
            lines.append(self.notice)
        if self.error:
            lines.append("⚠️ Couldn't load helpers right now. Press Refresh.")
        elif not self.helpers:
            lines.append("No helpers yet.")
        else:
            entries = []
            for r in self.helpers:
                names = ", ".join(ac.GRANTABLE[s] for s in sorted(r["sections"])) or "nothing"
                entries.append(f"<@{r['user_id']}> — {names} · added {_ts(r['created_at'])}")
            lines += _fit(entries)
        return lines

    def controls(self):
        S, P = discord.ButtonStyle.secondary, discord.ButtonStyle.primary
        items: list = []
        if self.helpers:
            sel = discord.ui.Select(
                placeholder="Pick a helper to edit or remove",
                options=[discord.SelectOption(label=f"User {r['user_id']}"[:100], value=str(r["user_id"]),
                                              description=(", ".join(ac.GRANTABLE[s] for s in sorted(r["sections"])) or None))
                         for r in self.helpers])
            sel.callback = self._pick
            items.append(sel)
        items += [_btn("Add helper", P, self._add, "➕"), _btn("Refresh", S, self._refresh, "🔄"),
                  _btn("Back", S, self._back, "⬅️")]
        return items

    async def _pick(self, i: discord.Interaction):
        uid = int(i.data["values"][0])
        row = next((r for r in self.helpers if r["user_id"] == uid), None)
        sections = set(row["sections"]) if row else set()
        await self.go(i, HelperEditView(self.cog, self.owner_id, uid, sections, existing=True))

    async def _add(self, i): await i.response.send_modal(AddHelperModal(self.cog))

    async def _refresh(self, i: discord.Interaction):
        self.notice = None
        await self.load()
        await i.response.edit_message(view=self)

    async def _back(self, i): await _home(self, i)


class HelperEditView(PanelView):
    title = "🔑 Edit helper"

    def __init__(self, cog, owner_id, target_id: int, sections: Set[str], existing: bool, section=ACCESS):
        self.target_id = target_id
        self.chosen: Set[str] = {s for s in sections if s in ac.GRANTABLE}
        self.existing = existing
        self._confirm = False
        super().__init__(cog, owner_id, section)

    def body(self):
        names = ", ".join(ac.GRANTABLE[s] for s in sorted(self.chosen)) or "nothing selected"
        lines = [f"**Helper:** <@{self.target_id}> (`{self.target_id}`)",
                 f"**Can open:** {names}",
                 "-# Access is re-checked on every click, so a change applies right away."]
        if self._confirm:
            lines.append("⚠️ **Press Confirm to remove this helper's access completely.**")
        return lines

    def controls(self):
        S, G, D = discord.ButtonStyle.secondary, discord.ButtonStyle.success, discord.ButtonStyle.danger
        sel = discord.ui.Select(
            placeholder="Sections this helper can open",
            min_values=0, max_values=len(ac.GRANTABLE),
            options=[discord.SelectOption(label=label, value=key, default=key in self.chosen)
                     for key, label in ac.GRANTABLE.items()])
        sel.callback = self._pick
        items: list = [sel, _btn("Save", G, self._save, "💾", disabled=not self.chosen)]
        if self.existing:
            items.append(_btn("Confirm remove" if self._confirm else "Remove helper", D, self._remove, "🗑️"))
        items.append(_btn("Back", S, self._back, "⬅️"))
        return items

    async def _pick(self, i: discord.Interaction):
        self.chosen = {v for v in i.data["values"] if v in ac.GRANTABLE}
        self._confirm = False
        self._build()
        await i.response.edit_message(view=self)

    async def _save(self, i: discord.Interaction):
        if not self.chosen:
            await i.response.send_message("Pick at least one section, or use Remove helper.", ephemeral=True)
            return
        try:
            stored = await ac.set_helper(self.target_id, self.chosen, i.user.id)
        except Exception:
            logger.exception("[admin-panel] couldn't save helper")
            await i.response.send_message("Couldn't save that (database problem). Nothing was changed.",
                                          ephemeral=True)
            return
        audit(i, "access.helper_set", target=self.target_id, sections=",".join(sorted(stored)))
        view = AccessView(self.cog, self.owner_id)
        view.notice = f"✅ Saved access for <@{self.target_id}>."
        await view.load()
        await i.response.edit_message(view=view)

    async def _remove(self, i: discord.Interaction):
        if not self._confirm:
            self._confirm = True
            self._build()
            await i.response.edit_message(view=self)
            return
        try:
            await ac.remove_helper(self.target_id)
        except Exception:
            logger.exception("[admin-panel] couldn't remove helper")
            await i.response.send_message("Couldn't remove that (database problem). Nothing was changed.",
                                          ephemeral=True)
            return
        audit(i, "access.helper_remove", target=self.target_id)
        view = AccessView(self.cog, self.owner_id)
        view.notice = f"✅ Removed <@{self.target_id}>."
        await view.load()
        await i.response.edit_message(view=view)

    async def _back(self, i: discord.Interaction):
        view = AccessView(self.cog, self.owner_id)
        await view.load()
        await self.go(i, view)


# ── log tail ─────────────────────────────────────────────────────────────

class LogsView(PanelView):
    title = "📋 Log tail"

    def __init__(self, cog, owner_id, section=LOGS):
        self.mode = "errors"
        super().__init__(cog, owner_id, section)

    def body(self):
        entries = ops.recent_logs(20, self.mode)
        label = "errors" if self.mode == "errors" else "warnings and errors"
        if not entries:
            return [f"No {label} logged since the bot last started.",
                    "-# Only this bot process is covered; the buffer keeps the latest 200."]
        shown, used = [], 0
        for e in entries:
            line = ops.format_log_line(e)
            if used + len(line) + 1 > TEXT_BUDGET - 120:
                break
            shown.append(line)
            used += len(line) + 1
        lines = [f"Latest **{len(shown)}** {label}, newest first. Secrets are masked."]
        lines.append("```\n" + "\n".join(shown) + "\n```")
        if len(shown) < len(entries):
            lines.append(f"-# …and {len(entries) - len(shown)} more not shown. Use **Send as file** for everything.")
        return lines

    def controls(self):
        S = discord.ButtonStyle.secondary
        toggle = "Show warnings too" if self.mode == "errors" else "Errors only"
        return [_btn(toggle, S, self._toggle, "🔀"), _btn("Refresh", S, self._refresh, "🔄"),
                _btn("Send as file", discord.ButtonStyle.primary, self._file, "📎"),
                _btn("Back", S, self._back, "⬅️")]

    async def _toggle(self, i: discord.Interaction):
        self.mode = "warnings" if self.mode == "errors" else "errors"
        self._build()
        await i.response.edit_message(view=self)

    async def _refresh(self, i: discord.Interaction):
        self._build()
        await i.response.edit_message(view=self)

    async def _file(self, i: discord.Interaction):
        audit(i, "logs.download", mode=self.mode)
        data = ops.logs_as_text(self.mode).encode("utf-8")
        await i.response.send_message(file=discord.File(io.BytesIO(data), filename="prime-bot-logs.txt"),
                                      ephemeral=True)

    async def _back(self, i): await _home(self, i)


# ── config viewer ────────────────────────────────────────────────────────

class ConfigFilterModal(discord.ui.Modal, title="Filter settings"):
    def __init__(self, view: "ConfigView"):
        super().__init__(timeout=PANEL_TIMEOUT)
        self.cview = view
        self.query = discord.ui.TextInput(label="Part of a setting name", max_length=40, required=False,
                                          default=view.query or None, placeholder="e.g. PREMIUM")
        self.add_item(self.query)

    async def on_submit(self, interaction: discord.Interaction):
        if CONFIG not in allowed_sections(interaction.user.id):
            await _denied(interaction)
            return
        self.cview.query = (self.query.value or "").strip()
        self.cview.page = 0
        self.cview._build()
        await interaction.response.edit_message(view=self.cview)


class ConfigView(PanelView):
    title = "🧾 Config viewer"

    def __init__(self, cog, owner_id, section=CONFIG, module=None):
        if module is None:
            import config as module  # the live settings module
        self.module = module
        self.query = ""
        self.page = 0
        super().__init__(cog, owner_id, section)

    def _entries(self):
        return ops.config_entries(self.module, self.query)

    def _pages(self, n: int) -> int:
        return max(1, -(-n // CONFIG_PAGE))

    def body(self):
        entries = self._entries()
        pages = self._pages(len(entries))
        self.page = min(self.page, pages - 1)
        masked = sum(1 for e in entries if e.secret)
        head = f"**{len(entries)}** setting{'s' if len(entries) != 1 else ''}"
        if self.query:
            head += f" matching `{self.query.replace('`', '')}`"
        head += f" · 🔒 {masked} masked · page {self.page + 1}/{pages}"
        if not entries:
            return [head, "Nothing matches that filter."]
        chunk = entries[self.page * CONFIG_PAGE:(self.page + 1) * CONFIG_PAGE]
        return [head, *_fit([f"`{e.name}` = {e.shown if e.secret else '`' + e.shown + '`'}" for e in chunk]),
                "-# Read-only. Secrets (tokens, keys, passwords, URLs with credentials) are never shown, only "
                "whether they're set. Values are from when the bot started."]

    def controls(self):
        S = discord.ButtonStyle.secondary
        pages = self._pages(len(self._entries()))
        return [_btn("Prev", S, self._prev, "◀️", disabled=self.page <= 0),
                _btn("Next", S, self._next, "▶️", disabled=self.page >= pages - 1),
                _btn("Filter", discord.ButtonStyle.primary, self._filter, "🔎"),
                _btn("Clear filter", S, self._clear, "❎", disabled=not self.query),
                _btn("Back", S, self._back, "⬅️")]

    async def _move(self, i: discord.Interaction, delta: int):
        self.page = max(0, self.page + delta)
        self._build()
        await i.response.edit_message(view=self)

    async def _prev(self, i): await self._move(i, -1)
    async def _next(self, i): await self._move(i, 1)
    async def _filter(self, i): await i.response.send_modal(ConfigFilterModal(self))

    async def _clear(self, i: discord.Interaction):
        self.query, self.page = "", 0
        self._build()
        await i.response.edit_message(view=self)

    async def _back(self, i): await _home(self, i)


# ── database tools ───────────────────────────────────────────────────────

class DatabaseView(PanelView):
    title = "🗄️ Database tools"

    def __init__(self, cog, owner_id, section=DATABASE):
        self.info: Optional[dict] = None
        self.error = False
        self.notice: Optional[str] = None
        self._confirm = False
        super().__init__(cog, owner_id, section)

    async def load(self) -> None:
        try:
            self.info = await ops.db_overview()
            self.error = False
        except Exception:
            logger.exception("[admin-panel] couldn't load database overview")
            self.info, self.error = None, True
        self._build()

    @property
    def stale(self) -> int:
        return (self.info or {}).get("stale_payments", 0)

    def body(self):
        lines: List[str] = []
        if self.notice:
            lines.append(self.notice)
        if self.error or self.info is None:
            lines.append("⚠️ Couldn't read the database right now. Press Refresh.")
            return lines
        p = self.info["pool"]

        def fmt(v):
            return "?" if v is None else str(v)
        lines.append(f"**Connection pool:** {fmt(p['busy'])} busy · {fmt(p['idle'])} idle · "
                     f"{fmt(p['size'])} open (limit {fmt(p['max'])})")
        rows = [f"`{t}` {'n/a' if n is None else format(n, ',')}" for t, n in self.info["counts"]]
        lines.append("**Rows:** " + " · ".join(rows))
        lines.append(f"**Stale checkouts:** {self.stale} pending for more than {ops.STALE_HOURS}h")
        if self._confirm:
            lines.append(f"⚠️ **Press Confirm to mark {self.stale} abandoned checkout(s) as expired.** "
                         "Rows are kept, only their status changes, and they stop counting as pending revenue.")
        return lines

    def controls(self):
        S, G, P = discord.ButtonStyle.secondary, discord.ButtonStyle.success, discord.ButtonStyle.primary
        label = "Confirm cleanup" if self._confirm else f"Clean up stale payments ({self.stale})"
        return [_btn("Refresh", S, self._refresh, "🔄"),
                _btn(label, G if self._confirm else P, self._cleanup, "🧹",
                     disabled=self.error or self.stale == 0),
                _btn("Back", S, self._back, "⬅️")]

    async def _refresh(self, i: discord.Interaction):
        self._confirm = False
        self.notice = None
        await self.load()
        await i.response.edit_message(view=self)

    async def _cleanup(self, i: discord.Interaction):
        if not self._confirm:
            self._confirm = True
            self._build()
            await i.response.edit_message(view=self)
            return
        self._confirm = False
        try:
            n = await ops.run_stale_payment_cleanup()
        except Exception:
            logger.exception("[admin-panel] stale payment cleanup failed")
            await i.response.send_message("Cleanup failed (database problem). Nothing was changed.", ephemeral=True)
            return
        audit(i, "database.cleanup_stale_payments", expired=n, older_than_hours=ops.STALE_HOURS)
        self.notice = f"✅ Marked {n} stale checkout(s) as expired."
        await self.load()
        await i.response.edit_message(view=self)

    async def _back(self, i): await _home(self, i)
