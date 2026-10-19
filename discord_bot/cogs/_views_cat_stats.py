# path: discord_bot/cogs/_views_cat_stats.py

"""Stats and skills screen for one cat, opened from the Stats button on My cats. In place, with a Back button.

Pick points with the + buttons (nothing is saved yet), then Apply. Taking points back is a Respec (free once, then coins).
Skills are chosen with one multi-select, up to the cat's slots, and saved when picked. One Container, budget-tested."""

from __future__ import annotations

import logging
from typing import Dict, Optional

import discord

from discord_bot.cogs import _cat_visibility as vis
from modules import cat_adopt as ca
from modules import cat_rank as cr
from modules import cat_stats as cs

logger = logging.getLogger(__name__)

PINK = discord.Color.from_rgb(255, 182, 193)
_OOPS = "😿 Something went wrong, try again in a moment."


def _clone_id(client):
    return getattr(client, "clone_id", None)


NOTES = {
    "not_yours": "That cat isn't yours any more.",
    "stale": "Something changed on this cat. Have a look and try again.",
    "listed": "This cat is for sale. Take it off the market first.",
    "invalid": "Those points don't look right. Try again.",
    "shrink": "Points can't be taken back here. Use **Respec**.",
    "cap": f"One stat can hold at most {int(cs.CAP_SHARE * 100)}% of the points.",
    "pool": "That's more points than this cat has.",
    "slots": "This cat doesn't have that many skill slots yet.",
    "dupe": "Pick each skill only once.",
    "unknown": "That skill isn't available.",
    "rank": "This cat isn't a high enough rank for that skill yet.",
}


def screen_text(cat: ca.Cat, stats: cs.Stats, pending: Dict[str, int], note: Optional[str] = None,
                armed: bool = False) -> str:
    """The whole text of the screen (pure, so the budget test can measure it)."""
    total = cs.pool(cat.rarity, cat.cat_rank)
    left = total - stats.spent() - sum(pending.values())
    cap = cs.stat_cap(total)
    cost = cs.respec_cost(cat.cat_rank, stats.respec_count)
    parts = []
    if note:
        parts += [note, ""]
    parts += [f"## ⚔️ {cat.name} · Stats & skills",
              f"{ca.rarity_badge(cat.rarity)} · {cr.badge(cat.cat_rank)} · {cat.trait}",
              "",
              cs.stat_lines(stats, cat.trait, pending),
              "",
              f"**Points to spend:** {left} of {total} · one stat can hold up to {cap}",
              "",
              f"**Skills** ({cs.slots_for(cat.cat_rank)} of {cs.MAX_SLOTS} slots)",
              cs.skill_lines(stats.skills, cat.cat_rank),
              "",
              f"-# Respec: {'free (first time)' if not cost else f'{cost:,} coins'}"
              + (" · tap Respec again to confirm" if armed else "")
              + " · just for fun, play coins only"]
    return "\n".join(parts)


class StatsView(discord.ui.LayoutView):
    def __init__(self, parent, cat: ca.Cat, stats: Optional[cs.Stats] = None, note: Optional[str] = None):
        super().__init__(timeout=600)
        self.parent, self.cat = parent, cat
        self.stats = stats or cs.Stats()
        self.note = note
        self.pending: Dict[str, int] = {s: 0 for s in cs.STATS}
        self.armed = False
        self._build()

    # -- numbers ---------------------------------------------------------
    @property
    def total(self) -> int:
        return cs.pool(self.cat.rarity, self.cat.cat_rank)

    @property
    def left(self) -> int:
        return self.total - self.stats.spent() - sum(self.pending.values())

    def _can_add(self, s: str) -> bool:
        return self.left > 0 and int(getattr(self.stats, s)) + self.pending[s] < cs.stat_cap(self.total)

    # -- layout ----------------------------------------------------------
    def text(self) -> str:
        return screen_text(self.cat, self.stats, self.pending, self.note, self.armed)

    def _build(self) -> None:
        self.clear_items()
        box = discord.ui.Container(accent_colour=PINK)
        box.add_item(discord.ui.TextDisplay(self.text()[:3900]))
        box.add_item(discord.ui.Separator())
        plus = []
        for s in cs.STATS:
            b = discord.ui.Button(label=f"+ {cs.STAT_LABEL[s]}", emoji=cs.STAT_EMOJI[s],
                                  style=discord.ButtonStyle.primary, disabled=not self._can_add(s))
            b.callback = self._plus_cb(s)
            plus.append(b)
        box.add_item(discord.ui.ActionRow(*plus))
        have = bool(sum(self.pending.values()))
        apply_b = discord.ui.Button(label="Apply", style=discord.ButtonStyle.success, disabled=not have)
        apply_b.callback = self._apply
        clear_b = discord.ui.Button(label="Clear picks", style=discord.ButtonStyle.secondary, disabled=not have)
        clear_b.callback = self._clear
        respec_b = discord.ui.Button(label="Tap again: Respec" if self.armed else "Respec",
                                     style=discord.ButtonStyle.danger if self.armed else discord.ButtonStyle.secondary,
                                     disabled=self.stats.spent() == 0)
        respec_b.callback = self._respec
        prac_b = discord.ui.Button(label="Practice", emoji="🪩", style=discord.ButtonStyle.primary,
                                   disabled=have)
        prac_b.callback = self._practice
        back_b = discord.ui.Button(label="Back", style=discord.ButtonStyle.secondary)
        back_b.callback = self._back
        box.add_item(discord.ui.ActionRow(apply_b, clear_b, respec_b, prac_b, back_b))
        options = cs.skills_for(self.cat.cat_rank)
        slots = cs.slots_for(self.cat.cat_rank)
        sel = discord.ui.Select(
            placeholder=f"Choose up to {slots} skill{'s' if slots != 1 else ''}",
            min_values=0, max_values=min(slots, len(options)),
            options=[discord.SelectOption(label=f"{o.name} · every {o.cooldown} rounds"[:100], value=o.key, emoji=o.emoji,
                                          description=o.text[:100], default=o.key in self.stats.skills) for o in options])
        sel.callback = self._skills
        box.add_item(discord.ui.ActionRow(sel))
        self.add_item(box)

    async def _push(self, interaction: discord.Interaction, note: Optional[str] = None) -> None:
        self.note = note
        self._build()
        await vis.edit(interaction, view=self)

    async def _reload(self) -> None:
        self.stats = await cs.get_stats(self.cat.id)
        self.pending = {s: 0 for s in cs.STATS}
        self.armed = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await self.parent.interaction_check(interaction)

    # -- buttons ---------------------------------------------------------
    def _plus_cb(self, s: str):
        async def cb(interaction: discord.Interaction):
            self.armed = False
            if self._can_add(s):
                self.pending[s] += 1
            await self._push(interaction)
        return cb

    async def _clear(self, interaction: discord.Interaction):
        self.pending = {s: 0 for s in cs.STATS}
        self.armed = False
        await self._push(interaction)

    async def _apply(self, interaction: discord.Interaction):
        new = {s: int(getattr(self.stats, s)) + self.pending[s] for s in cs.STATS}
        try:
            res = await cs.apply_points(self.cat.id, interaction.user.id, _clone_id(interaction.client), self.cat.cat_rank, new)
        except Exception:
            logger.exception("[cat-stats] apply failed for cat %s", self.cat.id)
            await interaction.response.send_message(_OOPS, ephemeral=True)
            return
        await self._after(interaction, res, f"✅ Points saved for **{self.cat.name}**.")

    async def _respec(self, interaction: discord.Interaction):
        if not self.armed:
            self.armed = True
            await self._push(interaction)
            return
        try:
            res = await cs.respec(self.cat.id, interaction.user.id, _clone_id(interaction.client), self.cat.cat_rank,
                                  self.stats.respec_count)
        except Exception:
            logger.exception("[cat-stats] respec failed for cat %s", self.cat.id)
            await interaction.response.send_message(_OOPS, ephemeral=True)
            return
        ok = (f"🔄 All points are back. (spent {res.cost:,} coins, {res.coins:,} left)" if res.cost
              else "🔄 All points are back. That was your free respec.")
        if res.status == "poor":
            await self._push(interaction, f"You need {res.cost:,} coins to respec and you have {res.coins:,}.")
            return
        await self._after(interaction, res, ok)

    async def _skills(self, interaction: discord.Interaction):
        keys = list(interaction.data.get("values", []))
        try:
            res = await cs.set_skills(self.cat.id, interaction.user.id, _clone_id(interaction.client), self.cat.cat_rank, keys)
        except Exception:
            logger.exception("[cat-stats] skills failed for cat %s", self.cat.id)
            await interaction.response.send_message(_OOPS, ephemeral=True)
            return
        await self._after(interaction, res, "✅ Skills saved.")

    async def _after(self, interaction, res: cs.StatsResult, ok_note: str) -> None:
        if res.status == "ok":
            await self._reload()
            await self._push(interaction, ok_note)
            return
        await self._reload()
        await self._push(interaction, NOTES.get(res.status, _OOPS))

    async def _practice(self, interaction: discord.Interaction):
        from discord_bot.cogs._views_cat_fight import open_practice
        await open_practice(interaction, self)

    async def _back(self, interaction: discord.Interaction):
        await self.parent._push(interaction)


async def open_stats(interaction: discord.Interaction, parent) -> None:
    """Open the screen for the cat on show in My cats."""
    cat = parent.current
    stats = await cs.get_stats(cat.id)
    await vis.edit(interaction, view=StatsView(parent, cat, stats))
