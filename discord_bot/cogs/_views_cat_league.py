# path: discord_bot/cogs/_views_cat_league.py

"""League screen (fights plan, phase 5B): join the season, see your division, points and the top of your division.
Opens from the Arena screen (no new slash command) and edits one message in place. Play coins only.
Other players show as display names only, never ids."""

from __future__ import annotations

import logging
from typing import List, Optional

import discord

from discord_bot.cogs import _cat_visibility as vis
from discord_bot.cogs._views_cat_arena import OOPS, _box, _btn, _clone_id
from discord_bot.cogs._views_cat_board import _name_of
from modules import cat_league as cl
from modules import cat_wallet

logger = logging.getLogger(__name__)


def league_text(entry: Optional[cl.Entry], names: List[str], ends_ts: Optional[int], note: Optional[str] = None,
                top: Optional[List[cl.Entry]] = None) -> str:
    parts = [note, ""] if note else []
    parts += ["## 🏆 Cat League",
              f"Seasons last {cl.SEASON_DAYS} days. Joining costs **{cl.ENTRY_FEE:,}** {cat_wallet.COINS} once per season (not refunded). "
              f"Fights against other League players earn **{cl.POINTS_WIN}** points for a win and **{cl.POINTS_LOSS}** for a loss. "
              f"At the end the top fifth of each division moves up and the bottom fifth moves down.", ""]
    if entry is None:
        parts += ["You are not in this season yet." + (f" It ends <t:{ends_ts}:R>." if ends_ts else " The season starts when the first player joins.")]
        return "\n".join(parts)
    parts += [f"**Your division:** {cl.division_name(entry.division)} · **{entry.points}** pts · {entry.wins}W {entry.losses}L"
              + (f" · season ends <t:{ends_ts}:R>" if ends_ts else ""), "", f"**Top of {cl.division_name(entry.division)}**"]
    parts += [f"{i}. {n} · {e.points} pts" for i, (n, e) in enumerate(zip(names, top or []), 1)] or ["Nobody has points yet."]
    return "\n".join(parts)


class LeagueView(discord.ui.LayoutView):
    def __init__(self, parent):
        super().__init__(timeout=600)
        self.parent = parent
        self.note: Optional[str] = None
        self.entry: Optional[cl.Entry] = None
        self.top: List[cl.Entry] = []
        self.ends_ts: Optional[int] = None
        self.names: List[str] = []
        self.client = None
        self.user_id = 0
        self.clone_id = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await self.parent.interaction_check(interaction)

    async def load(self, interaction: discord.Interaction) -> None:
        self.client, self.user_id, self.clone_id = interaction.client, interaction.user.id, _clone_id(interaction.client)
        self.entry = await cl.my_entry(self.user_id, self.clone_id)
        self.top, self.names = [], []
        if self.entry:
            self.top = await cl.standings(self.entry.season_id, self.entry.division)
            self.names = [_name_of(self.client, e.owner_id) for e in self.top]
            self.ends_ts = int(self.entry.ends_at.timestamp()) if self.entry.ends_at else None
        else:
            info = await cl.season_info(self.clone_id)
            self.ends_ts = int(info[1].timestamp()) if info else None
        self._build()

    def _build(self) -> None:
        self.clear_items()
        row = discord.ui.ActionRow(
            _btn(f"Join ({cl.ENTRY_FEE:,} coins)", self._join, discord.ButtonStyle.success, "🏆", disabled=self.entry is not None),
            _btn("Refresh", self._refresh, emoji="🔄"),
            _btn("Back", self._back))
        self.add_item(_box(league_text(self.entry, self.names, self.ends_ts, self.note, self.top), [row]))

    @staticmethod
    async def _ack(interaction: discord.Interaction) -> None:
        """Answer the tap NOW. Everything below talks to the database, and Discord gives only 3 seconds to answer a tap
        (a far database can take longer than that), so acknowledge first and edit the message when the work is done."""
        if not interaction.response.is_done():
            try:
                await interaction.response.defer()
            except discord.HTTPException:
                logger.warning("[cat-league] couldn't acknowledge the tap", exc_info=True)

    async def _show(self, interaction: discord.Interaction, note: Optional[str] = None) -> None:
        await self._ack(interaction)
        self.note = note
        try:
            await self.load(interaction)
        except Exception:
            logger.exception("[cat-league] couldn't load the League screen")
            self.note = OOPS
            self._build()
        await vis.edit(interaction, view=self, original=True)

    async def _refresh(self, interaction: discord.Interaction):
        await self._show(interaction)

    async def _join(self, interaction: discord.Interaction):
        await self._ack(interaction)
        try:
            res = await cl.join(interaction.user.id, _clone_id(interaction.client))
        except Exception:
            logger.exception("[cat-league] couldn't join")
            await self._show(interaction, OOPS)
            return
        if res.status == "ok":
            await self._show(interaction, f"✅ You are in! Placed in **{cl.division_name(res.division)}**. {res.coins:,} {cat_wallet.COINS} left.")
        else:
            await self._show(interaction, "🚫 " + cl.STATUS_TEXT.get(res.status, "That didn't work."))

    async def _back(self, interaction: discord.Interaction):
        await self.parent.push(interaction)


async def open_league(interaction: discord.Interaction, parent) -> None:
    view = LeagueView(parent)
    await view._show(interaction)
