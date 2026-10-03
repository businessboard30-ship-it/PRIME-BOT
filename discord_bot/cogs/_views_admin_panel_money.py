"""
Owner panel, Batch 4 — the Money hub: failed payments, revenue over time,
reverse a payment, discount codes and upcoming premium expiries.

Same rules as the rest of the panel (see _views_admin_panel.py): buttons,
selects and forms only (no new slash commands), only the person who opened the
panel can use it, access is re-checked on every click and modal submit, and
every state-changing action writes an audit line.

The whole hub is OWNER-ONLY (the "money" section is not in
admin_controls.GRANTABLE).

* Reversing a payment is two-step, idempotent, and only exists for `premium`
  payments (the one type with an exact inverse). It undoes the unlock; it does
  NOT refund money at the gateway.
* Discount codes are managed here but checkout does not read them yet.
"""

from __future__ import annotations

import logging
from typing import List, Optional

import discord

from discord_bot.cogs._views_admin_panel import PANEL_TIMEOUT, PanelView, _btn, allowed_sections, audit
from discord_bot.cogs._views_admin_panel_controls import TEXT_BUDGET, _clip, _denied, _fit, _home, _ts
from modules import admin_money as money

logger = logging.getLogger(__name__)

MONEY = "money"
NOTHING_CHANGED = "Couldn't save that (database problem). Nothing was changed."
LOAD_FAILED = "⚠️ Couldn't load this right now. Press Refresh."


def _safe(text: Optional[str]) -> str:
    return discord.utils.escape_mentions(discord.utils.escape_markdown(text or ""))


def _code(text: Optional[str], n: int = 60) -> str:
    """Text destined for a `code span`: markdown isn't interpreted there, so only
    backticks and pings need neutralising (escaping would show literal backslashes)."""
    return discord.utils.escape_mentions(_clip((text or "").replace("`", "'"), n))


def _age(dt) -> str:
    """'5m ago' / '3h ago' / '2d ago' as plain text (usable inside a code block)."""
    from datetime import datetime, timezone
    if dt is None:
        return "?"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    secs = max(0, int((datetime.now(timezone.utc) - dt).total_seconds()))
    return f"{secs // 86400}d ago" if secs >= 86400 else f"{secs // 3600}h ago" if secs >= 3600 else f"{secs // 60}m ago"


class _MoneyView(PanelView):
    notice: Optional[str] = None

    async def _edit(self, i: discord.Interaction) -> None:
        self._build()
        await i.response.edit_message(view=self)

    async def _back(self, i: discord.Interaction) -> None:
        await self.go(i, MoneyHubView(self.cog, self.owner_id))

    async def load(self) -> None:
        self._build()


# ── hub ──────────────────────────────────────────────────────────────────

class MoneyHubView(_MoneyView):
    title = "💰 Money"

    def __init__(self, cog, owner_id, section=MONEY):
        super().__init__(cog, owner_id, section)

    def body(self):
        return ["Payments, revenue and premium tools. Pick one.",
                "-# Pending payments, Revenue and Failed payments are read-only. Reverse and Discount codes change data."]

    def controls(self):
        P, S = discord.ButtonStyle.primary, discord.ButtonStyle.secondary
        return [
            _btn("Pending payments", P, self._pending, "🕒"),
            _btn("Failed payments", P, self._failures, "⚠️"),
            _btn("Revenue trend", P, self._revenue, "📈"),
            _btn("Reverse payment", P, self._reverse, "↩️"),
            _btn("Discount codes", P, self._coupons, "🏷️"),
            _btn("Upcoming expiries", P, self._expiries, "⏳"),
            _btn("Back", S, self._home_btn, "⬅️"),
        ]

    async def _home_btn(self, i):
        await _home(self, i)

    async def _pending(self, i):
        v = PendingView(self.cog, self.owner_id)
        await v.load()
        await self.go(i, v)

    async def _failures(self, i):
        v = FailuresView(self.cog, self.owner_id)
        await v.load()
        await self.go(i, v)

    async def _revenue(self, i):
        v = RevenueView(self.cog, self.owner_id)
        await v.load()
        await self.go(i, v)

    async def _reverse(self, i):
        await self.go(i, ReverseView(self.cog, self.owner_id))

    async def _coupons(self, i):
        v = CouponsView(self.cog, self.owner_id)
        await v.load()
        await self.go(i, v)

    async def _expiries(self, i):
        v = ExpiriesView(self.cog, self.owner_id)
        await v.load()
        await self.go(i, v)


# ── pending payments (references waiting in the queue) ───────────────────

class PendingView(_MoneyView):
    title = "🕒 Pending payments"

    def __init__(self, cog, owner_id, section=MONEY):
        self.rows: List[dict] = []
        self.total = 0
        self.notice = None
        self.error = False
        super().__init__(cog, owner_id, section)

    async def load(self) -> None:
        try:
            res = await money.list_pending()
            self.rows, self.total, self.error = res["rows"], res["total"], False
        except Exception:
            logger.exception("[admin-panel] couldn't load pending payments")
            self.rows, self.total, self.error = [], 0, True
        self._build()

    def body(self):
        lines = ["Payment references still waiting to be confirmed (newest first).",
                 "-# Read-only. Manual/Selar payments are approved by the payment flow, not from here."]
        if self.error:
            lines.append(LOAD_FAILED)
        elif not self.rows:
            lines.append("Nothing pending. 🎉")
        else:
            # One fenced block, no inline code and no <t:> timestamps: dozens of `code` spans mixed with
            # timestamps made the client strip them from each line and pile them up at the end.
            entries = []
            for r in self.rows:
                srv = f" | server {r['chat_id']}" if r.get("chat_id") else ""
                entries.append(f"{_code(r['paystack_reference'], 60)} | {_code(r.get('payment_type') or '?', 30)} | "
                               f"{_code(r.get('provider') or 'paystack', 12)} | {(r.get('amount') or 0):g} | "
                               f"buyer {r['user_id']}{srv} | {_age(r['created_date'])}")
            shown = _fit(entries, TEXT_BUDGET - 400)
            hidden = shown.pop() if shown and shown[-1].startswith("-# …and") else None
            lines.append("```\n" + "\n".join(shown) + "\n```")
            if hidden:
                lines.append(hidden)
            if self.total > len(self.rows):
                lines.append(f"-# Showing {len(self.rows)} of {self.total} pending.")
            else:
                lines.append(f"-# {self.total} pending.")
        return lines

    def controls(self):
        S = discord.ButtonStyle.secondary
        return [
            _btn("Refresh", S, self._refresh, "🔄"),
            _btn("Back", S, self._back, "⬅️"),
        ]

    async def _refresh(self, i):
        await self.load()
        await i.response.edit_message(view=self)


# ── failed payments ──────────────────────────────────────────────────────

class FailuresView(_MoneyView):
    title = "⚠️ Failed payments"

    def __init__(self, cog, owner_id, section=MONEY):
        self.rows: List[dict] = []
        self.selected: Optional[int] = None
        self.notice = None
        self.error = False
        super().__init__(cog, owner_id, section)

    async def load(self) -> None:
        try:
            self.rows = await money.list_failures()
            self.error = False
        except Exception:
            logger.exception("[admin-panel] couldn't load payment failures")
            self.rows, self.error = [], True
        if self.selected not in {r["id"] for r in self.rows}:
            self.selected = None
        self._build()

    def body(self):
        lines = ["Webhook problems and mismatched references recorded from Paystack and Gumroad (newest first).",
                 "-# Selar has no webhook in this repo, so nothing from it appears here."]
        if self.notice:
            lines.append(self.notice)
        if self.error:
            lines.append(LOAD_FAILED)
        elif not self.rows:
            lines.append("No open failures. 🎉")
        else:
            entries = []
            for r in self.rows:
                ref = f" · `{_code(r['reference'], 40)}`" if r["reference"] else ""
                det = f"\n> {_clip(_safe(r['detail']), 120)}" if r["detail"] else ""
                entries.append(f"**#{r['id']}** {r['source']} · {_clip(_safe(r['kind']), 40)}{ref} · {_ts(r['created_at'])}{det}")
            lines += _fit(entries)
        return lines

    def controls(self):
        S = discord.ButtonStyle.secondary
        items: list = []
        if self.rows:
            sel = discord.ui.Select(
                placeholder="Pick one to dismiss",
                options=[discord.SelectOption(label=f"#{r['id']} · {r['source']} · {r['kind']}"[:100],
                                              value=str(r["id"]), default=r["id"] == self.selected)
                         for r in self.rows[:25]])
            sel.callback = self._pick
            items.append(sel)
        items += [
            _btn("Dismiss", S, self._dismiss, "🗑️", disabled=self.selected is None),
            _btn("Refresh", S, self._refresh, "🔄"),
            _btn("Back", S, self._back, "⬅️"),
        ]
        return items

    async def _pick(self, i):
        self.selected = int(i.data["values"][0])
        self.notice = None
        await self._edit(i)

    async def _refresh(self, i):
        self.notice = None
        await self.load()
        await i.response.edit_message(view=self)

    async def _dismiss(self, i):
        fid = self.selected
        try:
            done = await money.dismiss_failure(fid, i.user.id)
        except Exception:
            logger.exception("[admin-panel] couldn't dismiss failure %s", fid)
            self.notice = NOTHING_CHANGED
            await self._edit(i)
            return
        if done:
            audit(i, "money.failure.dismiss", id=fid)
        self.notice = "✅ Dismissed." if done else "That one was already dismissed."
        self.selected = None
        await self.load()
        await i.response.edit_message(view=self)


# ── revenue over time ────────────────────────────────────────────────────

class RevenueView(_MoneyView):
    title = "📈 Revenue trend"
    _CONFIG = {"day": (14, "daily, last 14 days"), "week": (8, "weekly, last 8 weeks")}

    def __init__(self, cog, owner_id, section=MONEY):
        self.period = "day"
        self.series: dict = {}
        self.error = False
        self.notice = None
        super().__init__(cog, owner_id, section)

    async def load(self) -> None:
        n, _ = self._CONFIG[self.period]
        try:
            self.series = await money.revenue_series(self.period, n)
            self.error = False
        except Exception:
            logger.exception("[admin-panel] couldn't load revenue series")
            self.series, self.error = {}, True
        self._build()

    def body(self):
        _, label = self._CONFIG[self.period]
        lines = [f"Completed payments, {label}.",
                 "-# Bucketed by when checkout started. Gumroad is counted in USD, everything else in GHS, never added together."]
        if self.error:
            lines.append(LOAD_FAILED)
            return lines
        for cur in ("GHS", "USD"):
            pts = self.series.get(cur) or []
            total = sum(p["total"] for p in pts)
            count = sum(p["count"] for p in pts)
            if not pts or count == 0:
                lines.append(f"**{cur}** — no completed payments in this window.")
                continue
            peak = max(pts, key=lambda p: p["total"])
            lines.append(f"**{cur}** — {total:g} from {count} payment(s)\n"
                         f"`{money.sparkline([p['total'] for p in pts])}`\n"
                         f"-# Oldest → newest. Best {'day' if self.period == 'day' else 'week'}: "
                         f"{peak['start']:%d %b} ({peak['total']:g}).")
        return lines

    def controls(self):
        S = discord.ButtonStyle.secondary
        return [
            _btn("Weekly" if self.period == "day" else "Daily", S, self._toggle, "🔀"),
            _btn("Refresh", S, self._refresh, "🔄"),
            _btn("Back", S, self._back, "⬅️"),
        ]

    async def _toggle(self, i):
        self.period = "week" if self.period == "day" else "day"
        await self.load()
        await i.response.edit_message(view=self)

    async def _refresh(self, i):
        await self.load()
        await i.response.edit_message(view=self)


# ── reverse a payment ────────────────────────────────────────────────────

class ReferenceModal(discord.ui.Modal, title="Reverse a payment"):
    def __init__(self, cog):
        super().__init__(timeout=PANEL_TIMEOUT)
        self.cog = cog
        self.ref = discord.ui.TextInput(label="Payment reference", max_length=120,
                                        placeholder="e.g. gum_premium_123_ab12cd34")
        self.add_item(self.ref)

    async def on_submit(self, interaction: discord.Interaction):
        if MONEY not in allowed_sections(interaction.user.id):   # modals skip interaction_check
            await _denied(interaction)
            return
        view = ReverseView(self.cog, interaction.user.id)
        try:
            view.row = await money.find_payment(self.ref.value)
            view.looked_up = True
        except Exception:
            logger.exception("[admin-panel] payment lookup failed")
            view.notice = LOAD_FAILED
        view._build()
        await interaction.response.edit_message(view=view)


class ReverseView(_MoneyView):
    title = "↩️ Reverse a payment"

    def __init__(self, cog, owner_id, section=MONEY):
        self.row: Optional[dict] = None
        self.looked_up = False
        self.notice = None
        self._confirm = False
        super().__init__(cog, owner_id, section)

    def _problem(self) -> Optional[str]:
        return money.reversal_problem(self.row) if self.looked_up else None

    def body(self):
        lines = ["Undo a `premium` payment: takes its days back off the server's premium and marks the payment "
                 "**reversed** (the row is kept).",
                 "⚠️ This does **not** refund money. Refund in Paystack/Gumroad yourself.",
                 "-# Other payment types have no exact inverse, so they're refused here."]
        if self.notice:
            lines.append(self.notice)
        if self.looked_up:
            r = self.row
            if r:
                lines.append(f"`{_code(r['paystack_reference'])}` · {r.get('payment_type')} · "
                             f"{r.get('status')} · amount {(r.get('amount') or 0):g} · buyer `{r.get('user_id')}`"
                             + (f" · server `{r['chat_id']}`" if r.get("chat_id") else " · no server attached"))
            problem = self._problem()
            lines.append(f"🚫 {problem}" if problem else "✅ This payment can be reversed.")
            if self._confirm and not problem:
                from config import PREMIUM_DAYS
                lines.append(f"⚠️ **Press Confirm to remove {PREMIUM_DAYS} days of premium and mark this payment reversed.**")
        return lines

    def controls(self):
        S, D = discord.ButtonStyle.secondary, discord.ButtonStyle.danger
        can = self.looked_up and self._problem() is None
        return [
            _btn("Enter reference", S, self._enter, "🔎"),
            _btn("Confirm reversal" if self._confirm else "Reverse", D, self._reverse, "↩️", disabled=not can),
            _btn("Back", S, self._back, "⬅️"),
        ]

    async def _enter(self, i):
        await i.response.send_modal(ReferenceModal(self.cog))

    async def _reverse(self, i):
        if self._problem() is not None:
            await self._edit(i)
            return
        if not self._confirm:                       # first press only arms the button
            self._confirm = True
            await self._edit(i)
            return
        self._confirm = False
        pid, ref = self.row["payment_id"], self.row["paystack_reference"]
        try:
            res = await money.reverse_payment(pid, i.user.id)
        except Exception:
            logger.exception("[admin-panel] reversal of payment %s failed", pid)
            self.notice = NOTHING_CHANGED
            await self._edit(i)
            return
        if res["ok"]:
            audit(i, "money.reverse", payment_id=pid, reference=ref, days=res["days"],
                  server=self.row.get("chat_id"))
            self.row = {**self.row, "status": "reversed"}
            self.notice = "✅ Reversed. Remember to refund the money at the gateway if needed."
        else:
            self.notice = "Nothing changed: it was already reversed or is no longer reversible."
            try:
                self.row = await money.find_payment(ref)
            except Exception:
                logger.debug("[admin-panel] re-lookup after no-op failed", exc_info=True)
        await self._edit(i)


# ── discount codes ───────────────────────────────────────────────────────

class CouponModal(discord.ui.Modal, title="New discount code"):
    def __init__(self, cog):
        super().__init__(timeout=PANEL_TIMEOUT)
        self.cog = cog
        self.code = discord.ui.TextInput(label="Code (letters, digits, - _)", min_length=3, max_length=24)
        self.percent = discord.ui.TextInput(label="Percent off (1-100)", max_length=3)
        self.uses = discord.ui.TextInput(label="Max uses (blank = unlimited)", required=False, max_length=6)
        self.days = discord.ui.TextInput(label="Valid for days (blank = never expires)",
                                         required=False, max_length=4)
        for item in (self.code, self.percent, self.uses, self.days):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction):
        if MONEY not in allowed_sections(interaction.user.id):   # modals skip interaction_check
            await _denied(interaction)
            return
        code = money.normalize_code(self.code.value)
        pct = money.parse_int(self.percent.value, 1, 100)
        uses = money.parse_int(self.uses.value, 1, 1_000_000) if self.uses.value.strip() else None
        days = money.parse_int(self.days.value, 1, 3650) if self.days.value.strip() else None
        problem = None
        if code is None:
            problem = "The code must be 3-24 characters: letters, digits, `-` or `_`."
        elif pct is None:
            problem = "Percent off must be a whole number from 1 to 100."
        elif self.uses.value.strip() and uses is None:
            problem = "Max uses must be a whole number, or blank."
        elif self.days.value.strip() and days is None:
            problem = "Days must be a whole number up to 3650, or blank."
        if problem:
            await interaction.response.send_message(f"⚠️ {problem}", ephemeral=True)
            return
        view = CouponsView(self.cog, interaction.user.id)
        try:
            created = await money.create_coupon(code, pct, uses, days, interaction.user.id)
        except Exception:
            logger.exception("[admin-panel] couldn't create coupon")
            await interaction.response.send_message(NOTHING_CHANGED, ephemeral=True)
            return
        if created:
            audit(interaction, "money.coupon.create", code=code, percent=pct, max_uses=uses, days=days)
            view.notice = f"✅ Created `{code}`."
        else:
            view.notice = f"⚠️ `{code}` already exists."
        await view.load()
        await interaction.response.edit_message(view=view)


class CouponsView(_MoneyView):
    title = "🏷️ Discount codes"

    def __init__(self, cog, owner_id, section=MONEY):
        self.rows: List[dict] = []
        self.selected: Optional[str] = None
        self.notice = None
        self.error = False
        super().__init__(cog, owner_id, section)

    async def load(self) -> None:
        try:
            self.rows = await money.list_coupons()
            self.error = False
        except Exception:
            logger.exception("[admin-panel] couldn't load coupons")
            self.rows, self.error = [], True
        if self.selected not in {r["code"] for r in self.rows}:
            self.selected = None
        self._build()

    def _selected_row(self) -> Optional[dict]:
        return next((r for r in self.rows if r["code"] == self.selected), None)

    def body(self):
        lines = ["⚠️ **Not live yet:** codes are saved here, but checkout doesn't read them. "
                 "Nothing is discounted until checkout is wired to this table."]
        if self.notice:
            lines.append(self.notice)
        if self.error:
            lines.append(LOAD_FAILED)
        elif not self.rows:
            lines.append("No codes yet.")
        else:
            entries = []
            for r in self.rows:
                cap = f"{r['uses']}/{r['max_uses']}" if r["max_uses"] else f"{r['uses']}/∞"
                exp = f" · expires {_ts(r['expires_at'])}" if r["expires_at"] else ""
                entries.append(f"`{r['code']}` — {r['percent_off']}% off · used {cap}{exp} · **{money.coupon_state(r)}**")
            lines += _fit(entries)
        return lines

    def controls(self):
        S, G = discord.ButtonStyle.secondary, discord.ButtonStyle.success
        items: list = []
        if self.rows:
            sel = discord.ui.Select(
                placeholder="Pick a code to enable/disable",
                options=[discord.SelectOption(label=r["code"], description=money.coupon_state(r),
                                              value=r["code"], default=r["code"] == self.selected)
                         for r in self.rows[:25]])
            sel.callback = self._pick
            items.append(sel)
        cur = self._selected_row()
        items += [
            _btn("New code", G, self._new, "➕"),
            _btn("Enable" if cur and not cur["active"] else "Disable", S, self._toggle, "🔀", disabled=cur is None),
            _btn("Refresh", S, self._refresh, "🔄"),
            _btn("Back", S, self._back, "⬅️"),
        ]
        return items

    async def _pick(self, i):
        self.selected = i.data["values"][0]
        self.notice = None
        await self._edit(i)

    async def _new(self, i):
        await i.response.send_modal(CouponModal(self.cog))

    async def _refresh(self, i):
        self.notice = None
        await self.load()
        await i.response.edit_message(view=self)

    async def _toggle(self, i):
        cur = self._selected_row()
        if cur is None:
            await self._edit(i)
            return
        want = not cur["active"]
        try:
            await money.set_coupon_active(cur["code"], want)
        except Exception:
            logger.exception("[admin-panel] couldn't toggle coupon")
            self.notice = NOTHING_CHANGED
            await self._edit(i)
            return
        audit(i, "money.coupon.enable" if want else "money.coupon.disable", code=cur["code"])
        self.notice = f"✅ `{cur['code']}` {'enabled' if want else 'disabled'}."
        await self.load()
        await i.response.edit_message(view=self)


# ── upcoming premium expiries ────────────────────────────────────────────

class ExpiriesView(_MoneyView):
    title = "⏳ Upcoming premium expiries"

    def __init__(self, cog, owner_id, section=MONEY):
        self.days = money.EXPIRY_WINDOWS[0]
        self.rows: List[dict] = []
        self.error = False
        self.notice = None
        super().__init__(cog, owner_id, section)

    async def load(self) -> None:
        try:
            self.rows = await money.upcoming_expiries(self.days)
            self.error = False
        except Exception:
            logger.exception("[admin-panel] couldn't load expiries")
            self.rows, self.error = [], True
        self._build()

    def body(self):
        lines = [f"Server premiums ending in the next {self.days} days (soonest first).",
                 "-# 🔁 = Gumroad membership, renews by itself. Others lapse unless paid again."]
        if self.error:
            lines.append(LOAD_FAILED)
        elif not self.rows:
            lines.append("Nothing expiring in this window.")
        else:
            entries = []
            for r in self.rows:
                where = f" · clone `{r['clone_id']}`" if r["clone_id"] else ""
                guild = None
                try:
                    guild = self.cog.bot.get_guild(r["guild_id"])
                except Exception:
                    pass
                name = f"{_clip(_safe(guild.name), 40)} (`{r['guild_id']}`)" if guild is not None else f"`{r['guild_id']}`"
                entries.append(f"{'🔁' if r['auto_renews'] else '•'} {name}{where} — {_ts(r['expires_at'])}")
            lines += _fit(entries)
        return lines

    def controls(self):
        S = discord.ButtonStyle.secondary
        nxt = money.EXPIRY_WINDOWS[(money.EXPIRY_WINDOWS.index(self.days) + 1) % len(money.EXPIRY_WINDOWS)]
        return [
            _btn(f"{nxt} days", S, self._cycle, "📅"),
            _btn("Refresh", S, self._refresh, "🔄"),
            _btn("Back", S, self._back, "⬅️"),
        ]

    async def _cycle(self, i):
        self.days = money.EXPIRY_WINDOWS[(money.EXPIRY_WINDOWS.index(self.days) + 1) % len(money.EXPIRY_WINDOWS)]
        await self.load()
        await i.response.edit_message(view=self)

    async def _refresh(self, i):
        await self.load()
        await i.response.edit_message(view=self)
