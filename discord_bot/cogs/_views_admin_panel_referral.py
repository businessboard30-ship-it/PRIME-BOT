"""
Owner panel — Referral giveaway. Buttons, selects and forms only (no new slash
commands). OWNER-ONLY: the "referral" section is not in admin_controls.GRANTABLE,
access is re-checked on every click and modal submit, and every state-changing
action writes an audit line.

Flow: New giveaway (title, prize, days, winners, optional server+role) ->
people use the existing /referral mycode and /referral use -> watch the
standings here -> End & pick winners (two-step). A 'role' giveaway grants the
role to the winners automatically; a 'manual' one lists the winners so you can
hand out the prize and tick them off with "Mark prize given".
"""

from __future__ import annotations

import logging
from typing import List, Optional

import discord

from discord_bot.cogs._views_admin_panel import PANEL_TIMEOUT, PanelView, _btn, allowed_sections, audit
from discord_bot.cogs._views_admin_panel_controls import _clip, _denied, _fit, _home, _ts
from modules import referral_giveaway as rg

logger = logging.getLogger(__name__)

SECTION = "referral"
NOTHING_CHANGED = "Couldn't save that (database problem). Nothing was changed."
LOAD_FAILED = "⚠️ Couldn't load this right now. Press Refresh."


def _owners() -> set:
    try:
        from config import DISCORD_CLONE_ADMIN_IDS, DISCORD_OWNER_BROADCAST_IDS
        return set(DISCORD_CLONE_ADMIN_IDS) | set(DISCORD_OWNER_BROADCAST_IDS)
    except Exception:
        return set()


def _safe(text: Optional[str]) -> str:
    return discord.utils.escape_mentions(discord.utils.escape_markdown(text or ""))


class NewGiveawayModal(discord.ui.Modal, title="New referral giveaway"):
    def __init__(self, cog):
        super().__init__(timeout=PANEL_TIMEOUT)
        self.cog = cog
        self.name = discord.ui.TextInput(label="Title", max_length=100)
        self.prize = discord.ui.TextInput(label="Prize", max_length=200, placeholder="e.g. 1 month Premium / VIP role")
        self.days = discord.ui.TextInput(label="Runs for how many days (1-365)", max_length=3, default="7")
        self.winners = discord.ui.TextInput(label="Number of winners (1-20)", max_length=2, default="1")
        self.role = discord.ui.TextInput(label="Auto role: SERVER_ID ROLE_ID (blank = I give it)", required=False,
                                         max_length=60)
        for item in (self.name, self.prize, self.days, self.winners, self.role):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction):
        if SECTION not in allowed_sections(interaction.user.id):   # modals skip interaction_check
            await _denied(interaction)
            return
        days = rg.parse_int(self.days.value, 1, rg.MAX_DAYS)
        winners = rg.parse_int(self.winners.value, 1, rg.MAX_WINNERS)
        ok, guild_id, role_id = rg.parse_role_spec(self.role.value)
        problem = None
        if not self.name.value.strip() or not self.prize.value.strip():
            problem = "Title and prize can't be empty."
        elif days is None:
            problem = f"Days must be a whole number from 1 to {rg.MAX_DAYS}."
        elif winners is None:
            problem = f"Winners must be a whole number from 1 to {rg.MAX_WINNERS}."
        elif not ok:
            problem = "For an automatic role type the server ID and the role ID separated by a space, or leave it blank."
        if problem:
            await interaction.response.send_message(f"⚠️ {problem}", ephemeral=True)
            return
        view = ReferralGiveawayView(self.cog, interaction.user.id)
        try:
            gid = await rg.create_giveaway(self.name.value.strip(), self.prize.value.strip(), days, winners,
                                           interaction.user.id, guild_id, role_id)
        except Exception:
            logger.exception("[admin-panel] couldn't create referral giveaway")
            await interaction.response.send_message(NOTHING_CHANGED, ephemeral=True)
            return
        audit(interaction, "referral.giveaway.create", id=gid, days=days, winners=winners,
              role_server=guild_id, role=role_id)
        view.selected = gid
        view.notice = f"✅ Giveaway **#{gid}** started. Tell people to use `/referral mycode` and `/referral use`."
        await view.load()
        await interaction.response.edit_message(view=view)


class ReferralGiveawayView(PanelView):
    title = "🎁 Referral giveaway"

    def __init__(self, cog, owner_id, section=SECTION):
        self.rows: List[dict] = []
        self.selected: Optional[int] = None
        self.table: List[dict] = []
        self.pick_winner: Optional[int] = None
        self.notice: Optional[str] = None
        self.error = False
        self._confirm = False
        super().__init__(cog, owner_id, section)

    # ── data ──
    def _current(self) -> Optional[dict]:
        return next((r for r in self.rows if r["id"] == self.selected), None)

    async def load(self) -> None:
        try:
            self.rows = await rg.list_giveaways()
            self.error = False
        except Exception:
            logger.exception("[admin-panel] couldn't load referral giveaways")
            self.rows, self.error = [], True
        if self.selected not in {r["id"] for r in self.rows}:
            self.selected = self.rows[0]["id"] if self.rows else None
        self.table = []
        cur = self._current()
        if cur is not None and cur["status"] == "active":
            try:
                self.table = await rg.standings(cur, _owners())
            except Exception:
                logger.exception("[admin-panel] couldn't load referral standings")
                self.error = True
        if self.pick_winner not in {w["user_id"] for w in (cur or {}).get("winners", [])}:
            self.pick_winner = None
        self._build()

    # ── rendering ──
    def body(self):
        lines = ["Reward the people who bring in the most users with `/referral use`. Only you can see or run this.",
                 "-# Counts referrals redeemed from the moment a giveaway starts. You never win your own giveaway."]
        if self.notice:
            lines.append(self.notice)
        if self.error:
            lines.append(LOAD_FAILED)
        cur = self._current()
        if cur is None:
            if not self.error:
                lines.append("No giveaways yet. Press **New giveaway**.")
            return lines
        prize = f"{_clip(_safe(cur['prize']), 80)} " + (
            f"(auto role <@&{cur['role_id']}> in server `{cur['guild_id']}`)" if cur["prize_kind"] == "role"
            else "(you hand it out)")
        lines.append(f"**#{cur['id']} {_clip(_safe(cur['title']), 60)}** — {cur['status']}\n"
                     f"Prize: {prize}\nWinners: {cur['winner_count']} · "
                     f"{_ts(cur['starts_at'])} → {_ts(cur['ends_at'])}")
        if cur["status"] == "active":
            if self.table:
                entries = [f"**{n}.** <@{r['user_id']}> — {r['count']} referral(s)" for n, r in enumerate(self.table, 1)]
                lines += _fit(entries)
            elif not self.error:
                lines.append("No referrals yet.")
            if self._confirm:
                lines.append("⚠️ **Press Confirm to end it now and pick the winners from the standings above.**")
        elif not cur["winners"]:
            lines.append("Ended with no winners (nobody referred anyone).")
        else:
            entries = []
            for n, w in enumerate(cur["winners"], 1):
                if cur["prize_kind"] == "role":
                    state = "✅ role given" if w.get("role_ok") else "⚠️ role failed, press Retry role"
                else:
                    state = "✅ prize given" if w.get("awarded") else "⏳ waiting for you to give the prize"
                entries.append(f"**{n}.** <@{w['user_id']}> — {w['count']} referral(s) · {state}")
            lines += _fit(entries)
        return lines

    def controls(self):
        S, G, D = discord.ButtonStyle.secondary, discord.ButtonStyle.success, discord.ButtonStyle.danger
        items: list = []
        cur = self._current()
        if self.rows:
            sel = discord.ui.Select(
                placeholder="Pick a giveaway",
                options=[discord.SelectOption(label=f"#{r['id']} {r['title']}"[:100], description=r["status"],
                                              value=str(r["id"]), default=r["id"] == self.selected)
                         for r in self.rows[:25]])
            sel.callback = self._pick
            items.append(sel)
        if cur is not None and cur["status"] == "ended" and cur["prize_kind"] == "manual" and cur["winners"]:
            wsel = discord.ui.Select(
                placeholder="Pick a winner you gave the prize to",
                options=[discord.SelectOption(label=f"{n}. user {w['user_id']} ({w['count']} referrals)"[:100],
                                              description="prize given" if w.get("awarded") else "waiting",
                                              value=str(w["user_id"]), default=w["user_id"] == self.pick_winner)
                         for n, w in enumerate(cur["winners"][:25], 1)])
            wsel.callback = self._pick_winner
            items.append(wsel)
        active = cur is not None and cur["status"] == "active"
        failed = (cur is not None and cur["status"] == "ended" and cur["prize_kind"] == "role"
                  and any(w.get("role_ok") is False for w in cur["winners"]))
        items += [
            _btn("New giveaway", G, self._new, "➕"),
            _btn("Confirm end" if self._confirm else "End & pick winners", D, self._end, "🏁", disabled=not active),
            _btn("Mark prize given", G, self._mark, "✅", disabled=self.pick_winner is None),
            _btn("Retry role", S, self._retry, "🔁", disabled=not failed),
            _btn("Refresh", S, self._refresh, "🔄"),
            _btn("Back", S, self._back, "⬅️"),
        ]
        return items

    # ── actions ──
    async def _redraw(self, i):
        await self.load()
        await i.response.edit_message(view=self)

    async def _back(self, i):
        await _home(self, i)

    async def _refresh(self, i):
        self.notice, self._confirm = None, False
        await self._redraw(i)

    async def _pick(self, i):
        self.selected = int(i.data["values"][0])
        self.notice, self._confirm, self.pick_winner = None, False, None
        await self._redraw(i)

    async def _pick_winner(self, i):
        self.pick_winner = int(i.data["values"][0])
        self._build()
        await i.response.edit_message(view=self)

    async def _new(self, i):
        await i.response.send_modal(NewGiveawayModal(self.cog))

    async def _end(self, i):
        cur = self._current()
        if cur is None or cur["status"] != "active":
            await self._redraw(i)
            return
        if not self._confirm:                       # first press only arms the button
            self._confirm = True
            self._build()
            await i.response.edit_message(view=self)
            return
        self._confirm = False
        try:
            done = await rg.end_giveaway(cur["id"], _owners())
        except Exception:
            logger.exception("[admin-panel] couldn't end referral giveaway %s", cur["id"])
            self.notice = NOTHING_CHANGED
            await self._redraw(i)
            return
        if done is None:
            self.notice = "Nothing changed: it was already ended."
            await self._redraw(i)
            return
        granted = await rg.grant_winner_roles(done)
        audit(i, "referral.giveaway.end", id=cur["id"], winners=[w["user_id"] for w in done["winners"]],
              roles_given=granted)
        if not done["winners"]:
            self.notice = "🏁 Ended. Nobody referred anyone, so there are no winners."
        elif done["prize_kind"] == "role":
            self.notice = f"🏁 Ended. Role given to {granted} of {len(done['winners'])} winner(s)."
        else:
            self.notice = "🏁 Ended. Give the prize to the winners below, then tick them off with **Mark prize given**."
        await self._redraw(i)

    async def _retry(self, i):
        cur = self._current()
        if cur is None:
            await self._redraw(i)
            return
        granted = await rg.grant_winner_roles(cur, only_failed=True)
        audit(i, "referral.giveaway.retry_role", id=cur["id"], roles_ok=granted)
        self.notice = f"🔁 Role now given to {granted} of {len(cur['winners'])} winner(s)."
        await self._redraw(i)

    async def _mark(self, i):
        cur, uid = self._current(), self.pick_winner
        if cur is None or uid is None:
            await self._redraw(i)
            return
        try:
            done = await rg.mark_awarded(cur["id"], uid)
        except Exception:
            logger.exception("[admin-panel] couldn't mark prize given")
            self.notice = NOTHING_CHANGED
            await self._redraw(i)
            return
        if done:
            audit(i, "referral.giveaway.prize_given", id=cur["id"], winner=uid)
        self.notice = "✅ Marked as given." if done else "That winner was already marked."
        self.pick_winner = None
        await self._redraw(i)
