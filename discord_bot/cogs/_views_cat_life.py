"""Cat Life screens: earnings, home rent, walks, heists and lucky games (play coins only, never real money).

One ephemeral message that edits itself: the hub (🌸) -> a sub screen -> back to the hub with a note on top.
All the rules and database work live in modules/cat_life.py; this file only draws screens.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

import discord
from discord_bot.cogs import _cat_visibility as vis

from modules import cat_adopt, cat_life, cat_wallet

logger = logging.getLogger(__name__)

PINK = discord.Color.from_rgb(255, 182, 193)
COINS, DIAMOND = cat_wallet.COINS, cat_wallet.DIAMOND
FOOTER = "Play coins only 💖 just for fun, no real money. Your cats are never hurt or lost."
OOPS = "😿 Something went wrong, please try again in a moment."


def _clone(interaction):
    return getattr(interaction.client, "clone_id", None)


def _ts(dt) -> int:
    return int(cat_life._utc(dt).timestamp())


# ── text (pure, easy to test) ─────────────────────────────────────────────

def hub_description(ov: "cat_life.Overview", wallet: "cat_wallet.Wallet", note: Optional[str] = None) -> str:
    lines = []
    if note:
        lines += [note, ""]
    lines.append(f"💖 {cat_wallet.wallet_text(wallet)}")
    if ov.cats == 0:
        lines.append("🐱 You don't have a cat yet. Adopt one from a spawn to start! ✨")
        return "\n".join(lines)
    if ov.home_on:
        lines.append(f"🏡 Cozy home: rent paid until <t:{_ts(ov.home_until)}:R> ✨")
    else:
        lines.append(f"🏡 Cozy home: **rent not paid**. Your cats earn only {cat_life.percent(cat_life.HOME_OFF_FACTOR)} "
                     f"until you pay {COINS} {ov.rent_per_day:,} a day. 🥺")
    lines.append(f"💰 Waiting for you: {COINS} **{ov.pending:,}** (your fed cats earn about {COINS} {ov.estimate_per_day:,} a day, "
                 f"coins stop piling up after {cat_life.HOURS_CAP}h)")
    if ov.outings_out or ov.outings_ready:
        lines.append(f"🌷 Out and about: **{ov.outings_out}** cat{'s' if ov.outings_out != 1 else ''} away"
                     + (f" · 🎁 **{ov.outings_ready}** back and waiting for you!" if ov.outings_ready else ""))
    lines.append(f"🎀 Heists left today: **{ov.heists_left}** · 🎰 You can still lose up to {COINS} {ov.loss_left:,} in games today")
    return "\n".join(lines)


def rent_note(res: "cat_life.RentResult") -> str:
    if res.status == "ok":
        extra = f" (and picked up {COINS} {res.settled:,} your cats had earned)" if res.settled else ""
        return f"🏡 Rent paid! Your cozy home is ready until <t:{_ts(res.until)}:R>. Spent {COINS} {res.cost:,}{extra}. {COINS} {res.coins:,} left. 💖"
    if res.status == "poor":
        return f"🥺 The rent is {COINS} {res.cost:,} and you don't have enough yet. Collect, take a walk or convert a diamond first."
    if res.status == "too_far":
        return f"🏡 You've already paid ahead as far as allowed ({cat_life.RENT_MAX_PREPAID_DAYS} days). Come back later! ✨"
    return "🐱 Adopt a cat first and then you can rent a home for it."


def collect_note(res: "cat_life.CollectResult") -> str:
    if res.status == "ok":
        return f"💰 You picked up {COINS} **{res.coins:,}**! Your cats purr happily. 🐾"
    return "💤 Nothing to collect yet. Fed cats earn a little every hour, so come back soon! 🌸"


def outing_start_note(res: "cat_life.OutingStart", kind: str, route: str) -> str:
    if res.status == "ok":
        if kind == "walk":
            w = cat_life.WALKS[route]
            return (f"{w.emoji} **{res.cat_name}** went for a **{w.name}**! Back <t:{_ts(res.ends_at)}:R>. "
                    f"(Spent {COINS} {res.cost:,}, {COINS} {res.coins:,} left.) Tap 🎁 Claim when it's home. 🌸")
        h = cat_life.HEISTS[route]
        return (f"🎀 You and **{res.cat_name}** are sneaking into **{h.name}** {h.emoji}! Your chance: **{cat_life.percent(res.chance)}**. "
                f"Back <t:{_ts(res.ends_at)}:R>. (Spent {COINS} {res.cost:,}, {COINS} {res.coins:,} left.) Fingers crossed! 🤞")
    return {
        "poor": f"🥺 That costs {COINS} {res.cost:,} and you don't have enough yet.",
        "hungry": f"🍽️ **{res.cat_name}** is hungry and too sleepy to go out. Feed it first in **My cats**. 💛",
        "busy": f"🌷 **{res.cat_name}** is already out, or for sale. Pick another cat. ✨",
        "gone": "That cat isn't yours any more.",
        "daily_limit": f"🌙 That's {cat_life.HEISTS_PER_DAY} heists today! Your cats need rest. Come back after UTC midnight. 💤",
        "bad_route": "That place isn't on the map.",
    }.get(res.status, OOPS)


def claim_note(items) -> str:
    if not items:
        return "🌷 Nobody is back yet. Check the times in the hub! 🐾"
    lines = ["🎁 **Welcome home!**"]
    total, gems = 0, 0
    for o in items[:6]:
        kind = "🎀" if o.kind == "heist" else "🌷"
        lines.append(f"{kind} {o.story}")
        total += o.payout
        gems += o.diamonds
    if len(items) > 6:
        lines.append(f"…and {len(items) - 6} more outings.")
    lines.append(f"Total: {COINS} **{total:,}**" + (f" and {DIAMOND} **{gems}**!" if gems else ""))
    return "\n".join(lines)


def gamble_note(res: "cat_life.GambleResult") -> str:
    if res.status == "ok":
        name = "🪙 Coin flip" if res.game == "flip" else "🎡 Lucky spin"
        if res.payout > res.bet:
            head = f"✨ **You won!** {name}: bet {res.bet:,}, got back {COINS} **{res.payout:,}** (x{res.multiplier:g})"
        elif res.payout == res.bet:
            head = f"💫 {name}: you got your {res.bet:,} back (x{res.multiplier:g})."
        elif res.payout:
            head = f"🌧️ {name}: bet {res.bet:,}, got back {res.payout:,} (x{res.multiplier:g})."
        else:
            head = f"🌧️ {name}: bet {res.bet:,}, nothing this time. Better luck next round! 💖"
        return f"{head}\n{COINS} {res.coins:,} left · you can still lose up to {COINS} {res.loss_left:,} today."
    if res.status == "poor":
        return f"🥺 You don't have {COINS} {res.bet:,} to bet."
    if res.status == "limit":
        return (f"🌙 That's enough luck for today! Your daily limit is {COINS} {cat_life.DAILY_LOSS_LIMIT:,} in losses "
                f"(you can still lose {COINS} {res.loss_left:,}). Try a smaller bet or come back after UTC midnight. 💖")
    return OOPS


# ── screens ───────────────────────────────────────────────────────────────

async def hub_payload(user_id: int, clone_id, note: Optional[str] = None):
    wallet = await cat_wallet.safe_wallet(user_id, clone_id)
    ov = await cat_life.overview(user_id, clone_id)
    embed = discord.Embed(title="🌸 Cat Life 🎀", color=PINK, description=hub_description(ov, wallet, note))
    embed.set_footer(text=FOOTER)
    return embed, HubView(ov)


async def _show_hub(interaction: discord.Interaction, note: Optional[str] = None) -> None:
    try:
        embed, view = await hub_payload(interaction.user.id, _clone(interaction), note)
    except Exception:
        logger.exception("[cat-life] hub failed")
        await _reply(interaction, OOPS)
        return
    await _edit(interaction, embed, view)


async def _reply(interaction: discord.Interaction, text: str) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(text, ephemeral=True)
    else:
        await interaction.response.send_message(text, ephemeral=True)


async def _edit(interaction: discord.Interaction, embed: discord.Embed, view: discord.ui.View) -> None:
    vis.lock_to_owner(view, interaction.user.id)       # every redraw of a public Cat Life screen stays owner-only
    if interaction.response.is_done():
        await interaction.edit_original_response(embed=embed, view=view)
    else:
        await interaction.response.edit_message(embed=embed, view=view)


class CatLifeButton(discord.ui.Button):
    """The entry point, added to the /cat feature buttons."""

    def __init__(self):
        super().__init__(label="Cat Life", emoji="🌸", style=discord.ButtonStyle.success, custom_id="cat_feature:life")

    async def callback(self, interaction: discord.Interaction):
        try:
            embed, view = await hub_payload(interaction.user.id, _clone(interaction))
        except Exception:
            logger.exception("[cat-life] hub failed")
            await interaction.response.send_message(OOPS, ephemeral=True)
            return
        await vis.send(interaction, embed=embed, view=view)


class HubView(discord.ui.View):
    def __init__(self, ov: "cat_life.Overview"):
        super().__init__(timeout=300)
        has = ov.cats > 0
        self._add("Collect", "💰", discord.ButtonStyle.success, self._collect, 0, disabled=not has)
        self._add("Claim", "🎁", discord.ButtonStyle.primary, self._claim, 0, disabled=not (ov.outings_ready or ov.outings_out))
        self._add(f"Pay rent ({ov.rent_per_day:,}/day)", "🏡", discord.ButtonStyle.secondary, self._rent, 0, disabled=not has)
        self._add("Take a walk", "🌷", discord.ButtonStyle.secondary, self._walk, 1, disabled=not has)
        self._add("Heist", "🎀", discord.ButtonStyle.secondary, self._heist, 1, disabled=not has)
        self._add("Lucky games", "🎰", discord.ButtonStyle.secondary, self._games, 1)

    def _add(self, label, emoji, style, cb, row, disabled=False):
        b = discord.ui.Button(label=label[:80], emoji=emoji, style=style, row=row, disabled=disabled)
        b.callback = cb
        self.add_item(b)

    async def _collect(self, interaction: discord.Interaction):
        try:
            res = await cat_life.collect(interaction.user.id, _clone(interaction))
        except Exception:
            logger.exception("[cat-life] collect failed")
            await _reply(interaction, OOPS)
            return
        await _show_hub(interaction, collect_note(res))

    async def _claim(self, interaction: discord.Interaction):
        try:
            items = await cat_life.claim_outings(interaction.user.id, _clone(interaction))
        except Exception:
            logger.exception("[cat-life] claim failed")
            await _reply(interaction, OOPS)
            return
        await _show_hub(interaction, claim_note(items))

    async def _rent(self, interaction: discord.Interaction):
        try:
            res = await cat_life.pay_rent(interaction.user.id, _clone(interaction), 1)
        except Exception:
            logger.exception("[cat-life] rent failed")
            await _reply(interaction, OOPS)
            return
        await _show_hub(interaction, rent_note(res))

    async def _outing(self, interaction: discord.Interaction, kind: str):
        try:
            rows = await cat_life.free_cats(interaction.user.id, _clone(interaction))
        except Exception:
            logger.exception("[cat-life] cats failed")
            await _reply(interaction, OOPS)
            return
        if not rows:
            await _show_hub(interaction, "🐱 None of your cats can go out right now. They may be hungry, away, or for sale. "
                                         "Feed them in **My cats**. 💛")
            return
        await _edit(interaction, _outing_embed(kind), OutingView(kind, rows))

    async def _walk(self, interaction: discord.Interaction):
        await self._outing(interaction, "walk")

    async def _heist(self, interaction: discord.Interaction):
        await self._outing(interaction, "heist")

    async def _games(self, interaction: discord.Interaction):
        await _edit(interaction, _games_embed(), GamesView())


def _outing_embed(kind: str) -> discord.Embed:
    if kind == "walk":
        lines = [f"{w.emoji} **{w.name}**: {w.hours}h · costs {COINS} {w.fee:,}" + (f" · {DIAMOND} lucky find chance" if w.diamond_chance else "")
                 for w in cat_life.WALKS.values()]
        e = discord.Embed(title="🌷 Take a walk", color=PINK, description=(
            "Pick a place and a cat. The longer and fancier the walk, the more it costs, and the more your cat can bring home "
            "(sometimes a bit less, a walk is an adventure!). Rarer and higher ranked cats do better. 🌸\n\n" + "\n".join(lines)))
    else:
        lines = [f"{h.emoji} **{h.name}**: {h.seconds}s · costs {COINS} {h.fee:,} · base chance {cat_life.percent(h.success)} · "
                 f"win {COINS} {int(h.fee * h.multiplier):,}" for h in cat_life.HEISTS.values()]
        e = discord.Embed(title="🎀 Kitty heist", color=PINK, description=(
            "You and your cat sneak into a pretend place in the cutest heist ever! If you win you get a big payout; if you get "
            "caught, the fee is gone. Stronger cats have better chances (up to "
            f"{cat_life.percent(cat_life.HEIST_MAX_SUCCESS)}). Only pretend places, never real players. 🧶\n\n" + "\n".join(lines)))
    e.set_footer(text=FOOTER)
    return e


async def _heist_reveal(interaction: discord.Interaction, res: "cat_life.OutingStart", heist: "cat_life.Heist") -> None:
    """Heists take seconds: show the sneak-in screen, wait, then pay out and show the result on the same message.
    The outing is already saved, so if the bot restarts during the wait the 🎁 Claim button still pays it."""
    e = discord.Embed(title=f"🎀 Sneaking into {heist.name} {heist.emoji}", color=PINK, description=(
        f"You and **{res.cat_name}** tiptoe in... Your chance: **{cat_life.percent(res.chance)}**\n"
        f"Result <t:{_ts(res.ends_at)}:R>! 🤞\n(Spent {COINS} {res.cost:,}, {COINS} {res.coins:,} left.)"))
    e.set_footer(text=FOOTER)
    await interaction.response.edit_message(embed=e, view=None)
    await asyncio.sleep(heist.seconds + 1)
    items = []
    for _ in range(4):                                # a second or two of clock difference between the bot and the database
        try:
            items = await cat_life.claim_outings(interaction.user.id, _clone(interaction))
        except Exception:
            logger.exception("[cat-life] heist reveal failed")
            break
        if items:
            break
        await asyncio.sleep(1.5)
    await _show_hub(interaction, claim_note(items) if items else
                    "🎀 The heist is still going. Tap 🎁 Claim in a moment to see how it went! 🐾")


class OutingView(discord.ui.View):
    def __init__(self, kind: str, cat_rows):
        super().__init__(timeout=300)
        self.kind = kind
        self.route: Optional[str] = None
        self.cat_id: Optional[int] = None
        table = cat_life.WALKS if kind == "walk" else cat_life.HEISTS
        self.place = discord.ui.Select(placeholder="Where to? 🌸", min_values=1, max_values=1, row=0, options=[
            discord.SelectOption(label=v.name[:100], value=k, emoji=v.emoji, description=f"{v.fee:,} coins"[:100])
            for k, v in table.items()])
        self.place.callback = self._place
        self.add_item(self.place)
        self.cat = discord.ui.Select(placeholder="Which cat? 🐱", min_values=1, max_values=1, row=1, options=[
            discord.SelectOption(label=r["name"][:100], value=str(r["id"]), emoji=cat_adopt.RARITY_EMOJI.get(r["rarity"]),
                                 description=f"{r['rarity'].title()} · rank {int(r['cat_rank'] or 0)}"[:100])
            for r in cat_rows[:25]])
        self.cat.callback = self._cat
        self.add_item(self.cat)
        self.go = discord.ui.Button(label="Let's go!", emoji="✨", style=discord.ButtonStyle.success, disabled=True, row=2)
        self.go.callback = self._go
        self.add_item(self.go)
        back = discord.ui.Button(label="Back", emoji="⬅️", style=discord.ButtonStyle.secondary, row=2)
        back.callback = self._back
        self.add_item(back)

    def _ready(self):
        self.go.disabled = not (self.route and self.cat_id)

    async def _place(self, interaction: discord.Interaction):
        self.route = self.place.values[0]
        self._ready()
        await interaction.response.edit_message(view=self)

    async def _cat(self, interaction: discord.Interaction):
        self.cat_id = int(self.cat.values[0])
        self._ready()
        await interaction.response.edit_message(view=self)

    async def _go(self, interaction: discord.Interaction):
        if not (self.route and self.cat_id):
            await interaction.response.defer()
            return
        try:
            start = cat_life.start_walk if self.kind == "walk" else cat_life.start_heist
            res = await start(interaction.user.id, _clone(interaction), self.cat_id, self.route)
        except Exception:
            logger.exception("[cat-life] outing failed")
            await _reply(interaction, OOPS)
            return
        if self.kind == "heist" and res.status == "ok":
            await _heist_reveal(interaction, res, cat_life.HEISTS[self.route])
            return
        await _show_hub(interaction, outing_start_note(res, self.kind, self.route))

    async def _back(self, interaction: discord.Interaction):
        await _show_hub(interaction)


def _games_embed(note: Optional[str] = None) -> discord.Embed:
    desc = (note + "\n\n" if note else "") + (
        "🪙 **Coin flip**: pick a bet, then flip. Win and you get double back!\n"
        "🎡 **Lucky spin**: anything from nothing up to **x15** your bet!\n\n"
        f"To keep it fun and gentle, bets are {COINS} {cat_life.BETS[0]:,} to {COINS} {cat_life.BETS[-1]:,} and you can lose at most "
        f"{COINS} {cat_life.DAILY_LOSS_LIMIT:,} a day. The house wins a little more often than you, so play for fun, not to get rich. 💖")
    e = discord.Embed(title="🎰 Lucky games", color=PINK, description=desc)
    e.set_footer(text=FOOTER)
    return e


class GamesView(discord.ui.View):
    def __init__(self, bet: int = cat_life.BETS[0]):
        super().__init__(timeout=300)
        self.bet = bet
        sel = discord.ui.Select(placeholder=f"Bet: {bet:,} coins", min_values=1, max_values=1, row=0, options=[
            discord.SelectOption(label=f"{b:,} coins", value=str(b), emoji="🪙", default=b == bet) for b in cat_life.BETS])
        sel.callback = self._bet
        self.add_item(sel)
        for label, emoji, game in (("Coin flip", "🪙", "flip"), ("Lucky spin", "🎡", "spin")):
            b = discord.ui.Button(label=label, emoji=emoji, style=discord.ButtonStyle.primary, row=1)
            b.callback = self._play(game)
            self.add_item(b)
        back = discord.ui.Button(label="Back", emoji="⬅️", style=discord.ButtonStyle.secondary, row=1)
        back.callback = self._back
        self.add_item(back)

    async def _bet(self, interaction: discord.Interaction):
        bet = int(interaction.data["values"][0])
        await interaction.response.edit_message(view=GamesView(bet))

    def _play(self, game: str):
        async def cb(interaction: discord.Interaction):
            try:
                res = await cat_life.gamble(interaction.user.id, game, self.bet)
            except Exception:
                logger.exception("[cat-life] gamble failed")
                await _reply(interaction, OOPS)
                return
            await interaction.response.edit_message(embed=_games_embed(gamble_note(res)), view=GamesView(self.bet))
        return cb

    async def _back(self, interaction: discord.Interaction):
        await _show_hub(interaction)
