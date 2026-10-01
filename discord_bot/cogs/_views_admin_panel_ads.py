"""
Owner panel, Batch 5 — Ads approval and marketplace moderation.

Everything here calls the EXISTING logic in modules/ads_marketplace.py (the same
functions `/ad manage` uses: approve_ad, reject_ad, deactivate_ad, reactivate_ad,
deactivate_listing) — there is no second copy of the rules. Rejection DMs the
submitter through the same `notify_rejected` helper `/ad manage` uses.

Same panel rules as the rest (see _views_admin_panel.py): buttons, selects and
forms only; only the person who opened the panel can use it; access is
re-checked on every click and modal submit; every state change is audited.

* OWNER-ONLY. The ad actions in `/ad manage` are gated on DISCORD_CLONE_ADMIN_IDS,
  so "ads" is not in admin_controls.GRANTABLE.
* Ads: approve / reject / deactivate / reactivate. Ads are never deleted —
  "remove from circulation" is deactivate, which is reversible with Reactivate.
* Marketplace listings: Remove is two-step because the existing logic has no
  "put back" for a removed listing.
* The ads module swallows its own database errors (returns False / [] / {}),
  so a database problem is reported honestly as "nothing was changed or the
  database couldn't be reached" rather than guessed at.
"""

from __future__ import annotations

import logging
from typing import List, Optional

import discord

from discord_bot.cogs._views_admin_panel import PANEL_TIMEOUT, PanelView, _btn, allowed_sections, audit
from discord_bot.cogs._views_admin_panel_controls import TEXT_BUDGET, _clip, _denied, _fit, _home, _ts
from modules import ads_marketplace as ads

logger = logging.getLogger(__name__)

ADS = "ads"
LIST_LIMIT = 25        # Discord select-menu cap
MARKET_PAGE = 10
REASON_MAX = 200       # same cap as the /ad manage reject form

FILTERS = {            # key -> (button label, ad_submissions statuses)
    "pending": ("Pending", ("pending",)),
    "live": ("Live", ("approved",)),
    "paused": ("Paused", ("deactivated",)),
    "rejected": ("Rejected", ("rejected",)),
}
NOTHING_CHANGED = "Couldn't save that (database problem). Nothing was changed."
STATE_CHANGED = ("❌ Nothing was changed — that ad was already handled by someone else, "
                 "or the database couldn't be reached. Here's the current state.")


def _safe(text: Optional[str], n: int) -> str:
    """User-submitted text, made safe to show: clipped, no code-fence breaking
    backticks, and @ defused so an ad can't smuggle in a mass mention."""
    return _clip(text, n).replace("`", "'").replace("@", "@\u200b")


# ── ads ──────────────────────────────────────────────────────────────────

class RejectAdModal(discord.ui.Modal, title="Reject ad"):
    def __init__(self, view: "AdsView", ad_id: int):
        super().__init__(timeout=PANEL_TIMEOUT)
        self.view_, self.ad_id = view, ad_id
        self.reason = discord.ui.TextInput(
            label="Reason (sent to the submitter)", style=discord.TextStyle.paragraph,
            max_length=REASON_MAX, required=True)
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction):
        if ADS not in allowed_sections(interaction.user.id):   # modals skip interaction_check
            await _denied(interaction)
            return
        reason = str(self.reason.value).strip()[:REASON_MAX]
        if not reason:
            await interaction.response.send_message("Please give a reason — the submitter sees it.", ephemeral=True)
            return
        await interaction.response.defer()
        ad = await ads.get_ad(self.ad_id)
        try:
            ok = await ads.reject_ad(self.ad_id, reason)
        except Exception:
            logger.exception("[admin-panel] couldn't reject ad %s", self.ad_id)
            await interaction.followup.send(NOTHING_CHANGED, ephemeral=True)
            return
        if ok:
            audit(interaction, "ads.reject", ad_id=self.ad_id, reason=_clip(reason, 80))
            sent = False
            if ad:
                try:   # the DM must never undo or hide a rejection that already succeeded
                    from discord_bot.cogs._views_ads_wizard import notify_rejected
                    sent = await notify_rejected(interaction.client, ad, reason)
                except Exception:
                    logger.exception("[admin-panel] rejection DM failed for ad %s", self.ad_id)
            self.view_.notice = (f"❌ Ad #{self.ad_id} rejected. "
                                 + ("Submitter notified by DM." if sent else
                                    "Couldn't DM the submitter (DMs closed) — they can still see the reason in `/ad status`."))
        else:
            self.view_.notice = STATE_CHANGED
        self.view_.selected = None
        await self.view_.load()
        await interaction.edit_original_response(view=self.view_)


class AdsView(PanelView):
    title = "📣 Ads"

    def __init__(self, cog, owner_id, section=ADS):
        self.flt = "pending"
        self.rows: List[dict] = []
        self.counts: dict = {}
        self.selected: Optional[int] = None
        self.detail: Optional[dict] = None
        self.notice: Optional[str] = None
        super().__init__(cog, owner_id, section)

    async def load(self) -> None:
        try:
            self.counts = await ads.count_ads_by_status() or {}
            self.rows = await ads.search_ads(None, "", FILTERS[self.flt][1], LIST_LIMIT) or []
            ids = {r["id"] for r in self.rows}
            if self.selected not in ids:
                self.selected, self.detail = None, None
            else:
                self.detail = await ads.get_ad(self.selected)
                if self.detail is None:
                    self.selected = None
        except Exception:
            logger.exception("[admin-panel] couldn't load ads")
            self.rows, self.selected, self.detail = [], None, None
            self.notice = "⚠️ Couldn't read ads right now. Press Refresh."
        self._build()

    def _status(self) -> Optional[str]:
        return (self.detail or {}).get("status")

    def body(self):
        c = self.counts
        lines: List[str] = []
        if self.notice:
            lines.append(self.notice)
        lines.append(f"**Pending** {c.get('pending', 0)} · **Live** {c.get('approved', 0)} · "
                     f"**Paused** {c.get('deactivated', 0)} · **Rejected** {c.get('rejected', 0)}")
        lines.append(f"Showing **{FILTERS[self.flt][0]}** ads, newest first (up to {LIST_LIMIT}).")
        if not self.rows:
            lines.append("No ads here. (If you expected some, press Refresh — the list is empty "
                         "when the database can't be read too.)")
        d = self.detail
        if d:
            sub = d.get("submitted_at")
            lines.append(f"**#{d['id']} — {_safe(d.get('company_name'), 60)}** · {d.get('status')}")
            lines.append(f"**{_safe(d.get('ad_title'), 100)}**")
            lines.append(_safe(d.get("ad_description"), 350))
            lines.append(f"Link: {_safe(d.get('target_url'), 120)} · Budget: ${d.get('budget_usd')} · "
                         f"Submitter: `{d.get('user_id')}`"
                         + (" · 🖼️ has image" if d.get("image_message_id") else "")
                         + (f" · Submitted {_ts(sub)}" if sub else ""))
            if d.get("status") == "rejected" and d.get("rejection_reason"):
                lines.append(f"Rejected because: {_safe(d['rejection_reason'], 200)}")
        elif self.rows:
            lines.append("Pick an ad below to see it and act on it.")
        return _fit(lines, TEXT_BUDGET)

    def controls(self):
        S, P, G, R = (discord.ButtonStyle.secondary, discord.ButtonStyle.primary,
                      discord.ButtonStyle.success, discord.ButtonStyle.danger)
        items: list = []
        for key, (label, _statuses) in FILTERS.items():
            items.append(_btn(label, P if key == self.flt else S, self._make_filter(key)))
        items.append(_btn("Refresh", S, self._refresh, "🔄"))
        if self.rows:
            sel = discord.ui.Select(
                placeholder="Pick an ad…",
                options=[discord.SelectOption(
                    label=f"#{r['id']} · {r['company_name']} — {r['ad_title']}"[:100],
                    value=str(r["id"]), description=str(r.get("status", ""))[:100],
                    default=(r["id"] == self.selected)) for r in self.rows[:LIST_LIMIT]])
            sel.callback = self._pick
            items.append(sel)
        st = self._status()
        if st == "pending":
            items += [_btn("Approve", G, self._approve, "✅"), _btn("Reject", R, self._reject, "❌")]
        elif st == "approved":
            items.append(_btn("Deactivate", R, self._deactivate, "⏸️"))
        elif st == "deactivated":
            items.append(_btn("Reactivate", G, self._reactivate, "▶️"))
        items += [_btn("Marketplace", P, self._market, "🛒"), _btn("Back", S, self._back, "⬅️")]
        return items

    def _make_filter(self, key: str):
        async def cb(i: discord.Interaction):
            self.flt, self.selected, self.notice = key, None, None
            await self.load()
            await i.response.edit_message(view=self)
        return cb

    async def _refresh(self, i: discord.Interaction):
        self.notice = None
        await self.load()
        await i.response.edit_message(view=self)

    async def _pick(self, i: discord.Interaction):
        raw = (i.data.get("values") or [""])[0]
        self.selected = int(raw) if str(raw).isdigit() else None
        self.notice = None
        await self.load()
        await i.response.edit_message(view=self)

    async def _run(self, i: discord.Interaction, act: str, fn, done: str):
        if self.selected is None:
            await i.response.send_message("Pick an ad first.", ephemeral=True)
            return
        ad_id = self.selected
        try:
            ok = await fn(ad_id)
        except Exception:
            logger.exception("[admin-panel] ads.%s failed for ad %s", act, ad_id)
            await i.response.send_message(NOTHING_CHANGED, ephemeral=True)
            return
        if ok:
            audit(i, f"ads.{act}", ad_id=ad_id)
            self.notice = done.format(id=ad_id)
        else:
            self.notice = STATE_CHANGED
        self.selected = None
        await self.load()
        await i.response.edit_message(view=self)

    async def _approve(self, i):
        await self._run(i, "approve", ads.approve_ad, "✅ Ad #{id} approved — it's live.")

    async def _deactivate(self, i):
        await self._run(i, "deactivate", ads.deactivate_ad,
                        "⏸️ Ad #{id} deactivated — off every surface. Use Paused → Reactivate to bring it back.")

    async def _reactivate(self, i):
        await self._run(i, "reactivate", ads.reactivate_ad, "▶️ Ad #{id} is live again.")

    async def _reject(self, i: discord.Interaction):
        if self.selected is None or self._status() != "pending":
            await i.response.send_message("Pick a pending ad first.", ephemeral=True)
            return
        await i.response.send_modal(RejectAdModal(self, self.selected))

    async def _market(self, i: discord.Interaction):
        view = MarketView(self.cog, self.owner_id)
        await view.load()
        await self.go(i, view)

    async def _back(self, i): await _home(self, i)


# ── marketplace listings ─────────────────────────────────────────────────

class MarketView(PanelView):
    title = "🛒 Marketplace listings"

    def __init__(self, cog, owner_id, section=ADS):
        self.offset = 0
        self.rows: List[dict] = []
        self.selected: Optional[str] = None
        self.detail: Optional[dict] = None
        self.notice: Optional[str] = None
        self._confirm_id: Optional[str] = None
        super().__init__(cog, owner_id, section)

    async def load(self) -> None:
        try:
            self.rows = await ads.get_marketplace_listings(MARKET_PAGE, self.offset) or []
            ids = {str(r["id"]) for r in self.rows}
            if self.selected not in ids:
                self.selected, self.detail = None, None
            else:
                self.detail = await ads.get_listing(self.selected)
                if self.detail is None or self.detail.get("status") != "active":
                    self.selected, self.detail = None, None
        except Exception:
            logger.exception("[admin-panel] couldn't load marketplace listings")
            self.rows, self.selected, self.detail = [], None, None
            self.notice = "⚠️ Couldn't read listings right now. Press Refresh."
        if self._confirm_id != self.selected:
            self._confirm_id = None
        self._build()

    def body(self):
        lines: List[str] = []
        if self.notice:
            lines.append(self.notice)
        lines.append(f"Active listings, newest first · page {self.offset // MARKET_PAGE + 1}.")
        if not self.rows:
            lines.append("No active listings on this page. (Press Refresh if you expected some — "
                         "the list is also empty when the database can't be read.)")
        d = self.detail
        if d:
            lines.append(f"**{_safe(d.get('service_title'), 100)}** · {_safe(d.get('service_name'), 60)} · "
                         f"${d.get('price_usd')} · {_safe(d.get('category'), 30)}")
            lines.append(_safe(d.get("description"), 350))
            lines.append(f"Seller: `{d.get('user_id')}` · Clicks: {d.get('clicks', 0)} · ID `{d.get('id')}`")
            if self._confirm_id == self.selected:
                lines.append("⚠️ **Press Confirm to remove this listing.** There is no undo from the panel.")
        elif self.rows:
            lines.append("Pick a listing below to see it.")
        return _fit(lines, TEXT_BUDGET)

    def controls(self):
        S, R = discord.ButtonStyle.secondary, discord.ButtonStyle.danger
        items: list = []
        if self.rows:
            sel = discord.ui.Select(
                placeholder="Pick a listing…",
                options=[discord.SelectOption(
                    label=f"{r.get('service_title') or r.get('service_name') or r['id']}"[:100],
                    value=str(r["id"])[:100], description=f"${r.get('price_usd')} · seller {r.get('user_id')}"[:100],
                    default=(str(r["id"]) == self.selected)) for r in self.rows[:MARKET_PAGE]])
            sel.callback = self._pick
            items.append(sel)
        confirming = self._confirm_id is not None and self._confirm_id == self.selected
        items += [
            _btn("Prev", S, self._prev, "◀️", disabled=self.offset == 0),
            _btn("Next", S, self._next, "▶️", disabled=len(self.rows) < MARKET_PAGE),
            _btn("Refresh", S, self._refresh, "🔄"),
            _btn("Confirm remove" if confirming else "Remove", R, self._remove, "🗑️",
                 disabled=self.selected is None),
            _btn("Back to ads", S, self._back_ads, "⬅️"),
        ]
        return items

    async def _pick(self, i: discord.Interaction):
        raw = (i.data.get("values") or [""])[0]
        self.selected = str(raw) or None
        self._confirm_id, self.notice = None, None
        await self.load()
        await i.response.edit_message(view=self)

    async def _page(self, i: discord.Interaction, offset: int):
        self.offset, self.selected, self._confirm_id, self.notice = max(0, offset), None, None, None
        await self.load()
        await i.response.edit_message(view=self)

    async def _prev(self, i): await self._page(i, self.offset - MARKET_PAGE)
    async def _next(self, i): await self._page(i, self.offset + MARKET_PAGE)
    async def _refresh(self, i): await self._page(i, self.offset)

    async def _remove(self, i: discord.Interaction):
        if self.selected is None:
            await i.response.send_message("Pick a listing first.", ephemeral=True)
            return
        if self._confirm_id != self.selected:          # step one: just ask
            self._confirm_id = self.selected
            self._build()
            await i.response.edit_message(view=self)
            return
        listing_id, self._confirm_id = self.selected, None
        try:
            listing = await ads.get_listing(listing_id)
            if not listing or listing.get("status") != "active":
                self.notice = "That listing is already gone. Nothing was changed."
            else:
                # deactivate_listing is scoped to the seller, so pass the real seller's id
                ok = await ads.deactivate_listing(listing["user_id"], listing_id)
                if ok:
                    audit(i, "market.remove", listing_id=listing_id, seller=listing["user_id"])
                    self.notice = "🗑️ Listing removed."
                else:
                    self.notice = ("❌ Nothing was changed — the listing may already be gone or the "
                                   "database couldn't be reached.")
        except Exception:
            logger.exception("[admin-panel] couldn't remove listing %s", listing_id)
            await i.response.send_message(NOTHING_CHANGED, ephemeral=True)
            return
        self.selected = None
        await self.load()
        await i.response.edit_message(view=self)

    async def _back_ads(self, i: discord.Interaction):
        view = AdsView(self.cog, self.owner_id)
        await view.load()
        await self.go(i, view)
