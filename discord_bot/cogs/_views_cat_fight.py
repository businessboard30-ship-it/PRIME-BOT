# path: discord_bot/cogs/_views_cat_fight.py

"""Practice fight screens (fights plan, phase 2): pick a level (with the odds band), watch the replay, see the result.
Everything happens on one message, edited in place. Play coins only. The fight is already decided when the replay
starts; the replay is only a show, so nobody can stall it."""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

import discord

from discord_bot.cogs import _cat_visibility as vis
from modules import cat_adopt as ca
from modules import cat_battle as cb
from modules import cat_practice as cp
from modules import cat_stats as cs
from modules import cat_wallet

logger = logging.getLogger(__name__)

PINK = discord.Color.from_rgb(255, 182, 193)
REPLAY_DELAY = 1.4
TITLE = "🪩 Practice arena"


def _clone_id(client):
    return getattr(client, "clone_id", None)


def _box(text: str, rows=()) -> discord.ui.Container:
    box = discord.ui.Container(accent_colour=PINK)
    box.add_item(discord.ui.TextDisplay(text[:3900]))
    if rows:
        box.add_item(discord.ui.Separator())
        for r in rows:
            box.add_item(r)
    return box


def _screen(text: str, rows=()) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=600)
    view.add_item(_box(text, rows))
    return view


def pick_text(cat: ca.Cat, odds: dict, cooldown: int = 0, wins: int = 0) -> str:
    lines = [f"## {TITLE}", f"**{cat.name}** trains against a pretend cat. Nothing is at stake, and a win pays a few "
                            f"{cat_wallet.COINS} (up to {cp.DAILY_REWARDED_WINS} paid wins a day, {wins} so far today).", ""]
    for key, (name, emoji, _) in cb.DIFFICULTY.items():
        label, mark = odds.get(key, ("?", "❔"))
        lines.append(f"{emoji} **{name}** · {mark} {label} · win pays {cp.reward_for(cat.rarity, key):,} {cat_wallet.COINS}")
    if cooldown:
        lines += ["", f"⏳ Catch your breath: {cooldown}s until the next fight."]
    lines += ["", "-# Odds are a band from 200 quick test fights, not a promise · just for fun, play coins only"]
    return "\n".join(lines)


def replay_text(p: cp.Practice, upto: int) -> str:
    """The fight so far: the title, who fights, and the last two rounds shown."""
    shown = p.result.rounds[:upto]
    head = [f"## ⚔️ {p.mine.name} vs {p.npc.name}", f"-# {cb.DIFFICULTY[p.level][1]} {cb.DIFFICULTY[p.level][0]} · "
            f"HP {p.mine.hp} vs {p.npc.hp}", ""]
    body = [line for r in shown[-2:] for line in r]
    return "\n".join(head + body)


def result_text(p: cp.Practice, paid: int) -> str:
    r = p.result
    last = r.rounds[-1][-1]
    verdict = (f"🏵️ **{p.mine.name} wins!**" if p.won else f"🫧 **{p.npc.name} wins this one.** Practice makes purr-fect!")
    how = "A knock-out!" if r.ended_by == "ko" else f"After {len(r.rounds)} rounds, the most HP left wins."
    lines = [f"## ⚔️ {p.mine.name} vs {p.npc.name}", verdict, how, "", last, ""]
    if paid:
        lines.append(f"{cat_wallet.COINS} **+{paid:,}** practice coins.")
    elif p.won:
        lines.append(f"No more coin rewards today (up to {cp.DAILY_REWARDED_WINS} paid wins a day). Fighting is still free.")
    lines += ["", f"-# Fight code `{r.seed}` · just for fun, play coins only"]
    return "\n".join(lines)


class PracticeView(discord.ui.LayoutView):
    """Level picker. Opens from the Stats & skills screen."""

    def __init__(self, parent, cat: ca.Cat, stats: cs.Stats, odds: dict, note: Optional[str] = None):
        super().__init__(timeout=600)
        self.parent, self.cat, self.stats, self.odds, self.note = parent, cat, stats, odds, note
        self._build()

    def _build(self) -> None:
        self.clear_items()
        text = ((self.note + "\n\n") if self.note else "") + pick_text(
            self.cat, self.odds, cp.cooldown_left(self.parent.parent.user.id), cp.wins_today(self.parent.parent.user.id))
        btns = []
        for key, (name, emoji, _) in cb.DIFFICULTY.items():
            b = discord.ui.Button(label=name, emoji=emoji, style=discord.ButtonStyle.primary)
            b.callback = self._go_cb(key)
            btns.append(b)
        back = discord.ui.Button(label="Back", style=discord.ButtonStyle.secondary)
        back.callback = self._back
        btns.append(back)
        self.add_item(_box(text, [discord.ui.ActionRow(*btns)]))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await self.parent.interaction_check(interaction)

    async def _back(self, interaction: discord.Interaction):
        await self.parent._reload()
        await self.parent._push(interaction)

    def _go_cb(self, level: str):
        async def cb_(interaction: discord.Interaction):
            await self._go(interaction, level)
        return cb_

    async def _go(self, interaction: discord.Interaction, level: str) -> None:
        uid = interaction.user.id
        wait = cp.cooldown_left(uid)
        if wait:
            self.note = f"⏳ Catch your breath: {wait}s until the next fight."
            self._build()
            await vis.edit(interaction, view=self)
            return
        cp.start(uid)                                   # claimed before anything awaits, so two taps can't both run
        p = cp.play(self.cat, self.stats, level, uid)
        total = len(p.result.rounds)
        await vis.edit(interaction, view=_screen(replay_text(p, 1)))
        try:
            for n in range(2, total + 1):
                await asyncio.sleep(REPLAY_DELAY)
                await vis.edit(interaction, view=_screen(replay_text(p, n)), original=True)
            await asyncio.sleep(REPLAY_DELAY)
        except discord.HTTPException:
            logger.debug("[cat-fight] the replay message went away", exc_info=True)
        paid = await cp.pay(uid, _clone_id(interaction.client), p)
        again = discord.ui.Button(label="Practice again", emoji="🪩", style=discord.ButtonStyle.primary)
        back = discord.ui.Button(label="Back to stats", style=discord.ButtonStyle.secondary)
        again.callback, back.callback = self._again, self._back_after
        try:
            await vis.edit(interaction, view=_screen(result_text(p, paid), [discord.ui.ActionRow(again, back)]), original=True)
        except discord.HTTPException:
            logger.debug("[cat-fight] couldn't show the result", exc_info=True)

    async def _again(self, interaction: discord.Interaction):
        await open_practice(interaction, self.parent)

    async def _back_after(self, interaction: discord.Interaction):
        await self.parent._reload()
        await self.parent._push(interaction)


async def open_practice(interaction: discord.Interaction, parent) -> None:
    """Open the level picker for the cat on the stats screen `parent`."""
    cat, stats = parent.cat, parent.stats
    odds = {}
    for key in cb.DIFFICULTY:
        odds[key] = await asyncio.to_thread(cp.odds_for, cat, stats, key)
    await vis.edit(interaction, view=PracticeView(parent, cat, stats, odds))
