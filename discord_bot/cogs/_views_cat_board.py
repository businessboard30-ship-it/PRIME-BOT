# path: discord_bot/cogs/_views_cat_board.py

"""Request Board screens (fights plan, phase 4B): browse open fight requests, post one, take yours down.
Play coins only. Opens from the Arena screen (no new slash command) and edits one message in place.
Accepting a request comes in the next slice, so the board is browse-only for now.
Other players' ids never appear here: a request shows a display name and, only if the poster chose it, a server name."""

from __future__ import annotations

import logging
from typing import List, Optional

import discord

from discord_bot.cogs import _cat_visibility as vis
from discord_bot.cogs._views_cat_arena import OOPS, _box, _btn, _clone_id, cat_line
from modules import cat_arena as arena
from modules import cat_requests as cr
from modules import cat_wallet

logger = logging.getLogger(__name__)

HOUR_CHOICES = (1, 3, 6, 12, 24)


# ── pure text (the budget tests measure these) ──────────────────────────

def pr_band(power: int) -> str:
    """A rounded power so the board shows a band, not an exact number."""
    return f"~{max(10, int(round(int(power) / 10.0)) * 10)}"


def request_line(r: cr.Request, poster_name: str) -> str:
    where = cr.board_server_label(r.show_server, r.server_name)
    return (f"**{r.cat_name or 'A cat'}** · {r.rarity or '?'} · rank {r.cat_rank} · power {pr_band(r.power)} · "
            f"**{r.wager:,}** {cat_wallet.COINS} · by {poster_name}" + (f" · {where}" if where else ""))


def board_text(rows: List[str], page: int, pages: int, my_power: Optional[int], note: Optional[str] = None) -> str:
    parts = [note, ""] if note else []
    parts += ["## 📋 Request board",
              "Open fight requests from players with Arena on. You only see cats close to your cat's power "
              f"(±{int(cr.BAND_SHARE * 100)}%), from this server or from servers that allow cross-server fights.", ""]
    if my_power is None:
        parts += ["You have no cat that is ready to fight right now (tired, for sale, out or on a request)."]
    elif rows:
        parts += [f"Matching your cat's power {pr_band(my_power)}:", ""] + [f"{i}. {line}" for i, line in enumerate(rows, 1)]
        parts += ["", f"-# Page {page + 1} of {pages} · accepting a request arrives in the next update"]
    else:
        parts += ["Nothing on the board for your cat right now. Post a request and wait for someone to answer!"]
    return "\n".join(parts)


def post_text(ready: List[arena.Ready], cat_id: Optional[int], wager: int, hours: int, open_n: int,
              note: Optional[str] = None) -> str:
    parts = [note, ""] if note else []
    parts += ["## 📮 Post a request",
              "Put your cat on the board. Your wager is held right away and comes back in full if nobody accepts "
              "or you take it down. No coins are lost until someone accepts and the fight is fought.", ""]
    cur = next((r for r in ready if r.cat.id == cat_id), None)
    parts += [f"**Your cat:** {cat_line(cur)}" if cur else "You have no cat that is ready to fight right now.",
              f"**Wager:** {wager:,} {cat_wallet.COINS}", f"**Stays up for:** {hours} hour{'s' if hours != 1 else ''}",
              f"**Open requests:** {open_n}/{cr.MAX_OPEN}", "",
              f"-# Your cat is held (can't be sold, traded or sent to fight) while its request is open. "
              f"Winner gets both wagers minus {arena.FEE_PERCENT}%; nobody ever loses a cat."]
    return "\n".join(parts)


def mine_text(rows: List[cr.Request], note: Optional[str] = None) -> str:
    parts = [note, ""] if note else []
    parts += ["## 🗂️ My requests"]
    if not rows:
        parts += ["You have no open requests."]
    for i, r in enumerate(rows, 1):
        ends = f" · ends <t:{int(r.expires_at.timestamp())}:R>" if r.expires_at else ""
        parts += [f"{i}. **{r.cat_name or 'Your cat'}** · **{r.wager:,}** {cat_wallet.COINS}{ends}"]
    parts += ["", "-# Taking a request down gives your wager back in full."]
    return "\n".join(parts)


def _name_of(client, user_id: int) -> str:
    user = client.get_user(int(user_id)) if hasattr(client, "get_user") else None
    name = getattr(user, "display_name", None) or getattr(user, "name", None)
    return " ".join(str(name).split())[:32] if name else "A player"


# ── the board ───────────────────────────────────────────────────────────

class BoardView(discord.ui.LayoutView):
    def __init__(self, parent, ready: List[arena.Ready]):
        super().__init__(timeout=600)
        self.parent, self.ready = parent, ready
        self.cat_id: Optional[int] = ready[0].cat.id if ready else None
        self.page, self.pages = 0, 1
        self.items: List[cr.Request] = []
        self.note: Optional[str] = None
        self.client = None
        self.user_id = 0
        self.guild_id: Optional[int] = None
        self.clone_id = None

    def _power(self) -> Optional[int]:
        cur = next((r for r in self.ready if r.cat.id == self.cat_id), None)
        return cur.fighter.power if cur else None

    async def load(self, interaction: discord.Interaction) -> None:
        self.client = interaction.client
        self.user_id, self.clone_id = interaction.user.id, _clone_id(interaction.client)
        self.guild_id = getattr(interaction, "guild_id", None)
        power = self._power()
        if power is None:
            self.items, self.pages, self.page = [], 1, 0
        else:
            self.items, self.page, self.pages = await cr.board(self.user_id, self.clone_id, self.guild_id, power, self.page)
        self._build()

    def _build(self) -> None:
        self.clear_items()
        rows = []
        if self.ready:
            sel = discord.ui.Select(placeholder="Match this cat", options=[
                discord.SelectOption(label=f"{r.cat.name} · power {r.fighter.power}"[:100], value=str(r.cat.id),
                                     description=f"{r.cat.rarity}, rank {r.cat.cat_rank}", default=r.cat.id == self.cat_id)
                for r in self.ready[:25]])
            sel.callback = self._pick_cat
            rows.append(discord.ui.ActionRow(sel))
        rows.append(discord.ui.ActionRow(
            _btn("Prev", self._prev, emoji="◀️", disabled=self.page <= 0),
            _btn("Next", self._next, emoji="▶️", disabled=self.page >= self.pages - 1),
            _btn("Refresh", self._refresh, emoji="🔄")))
        rows.append(discord.ui.ActionRow(
            _btn("Post a request", self._post, discord.ButtonStyle.success, "📮"),
            _btn("My requests", self._mine, discord.ButtonStyle.primary, "🗂️"),
            _btn("Back", self._back)))
        names = [request_line(r, _name_of(self.client, r.poster_id)) for r in self.items]
        self.add_item(_box(board_text(names, self.page, self.pages, self._power(), self.note), rows))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await self.parent.interaction_check(interaction)

    async def _show(self, interaction: discord.Interaction, note: Optional[str] = None) -> None:
        self.note = note
        try:
            await self.load(interaction)
        except Exception:
            logger.exception("[cat-board] couldn't load the board")
            self.note = OOPS
            self._build()
        await vis.edit(interaction, view=self)

    async def _pick_cat(self, interaction: discord.Interaction):
        self.cat_id, self.page = int(interaction.data["values"][0]), 0
        await self._show(interaction)

    async def _prev(self, interaction: discord.Interaction):
        self.page = max(0, self.page - 1)
        await self._show(interaction)

    async def _next(self, interaction: discord.Interaction):
        self.page += 1
        await self._show(interaction)

    async def _refresh(self, interaction: discord.Interaction):
        await self._show(interaction)

    async def _post(self, interaction: discord.Interaction):
        await open_post(interaction, self.parent, self)

    async def _mine(self, interaction: discord.Interaction):
        await open_mine(interaction, self.parent, self)

    async def _back(self, interaction: discord.Interaction):
        await self.parent.push(interaction)


async def open_board(interaction: discord.Interaction, parent) -> None:
    """Open the board from the Arena screen. The strongest ready cat is matched first."""
    try:
        ready = await _free_cats(interaction)
        view = BoardView(parent, ready)
        await view.load(interaction)
    except Exception:
        logger.exception("[cat-board] couldn't open the board")
        await parent.push(interaction, OOPS)
        return
    await vis.edit(interaction, view=view)


async def _free_cats(interaction: discord.Interaction) -> List[arena.Ready]:
    """Cats that can fight now and aren't already held by an open request."""
    ready = await arena.ready_cats(interaction.user.id, _clone_id(interaction.client))
    held = await cr.locked_cat_ids([r.cat.id for r in ready])
    return [r for r in ready if r.cat.id not in held]


# ── post a request ──────────────────────────────────────────────────────

class PostView(discord.ui.LayoutView):
    def __init__(self, parent, board: BoardView, ready: List[arena.Ready], open_n: int, note: Optional[str] = None):
        super().__init__(timeout=600)
        self.parent, self.board, self.ready, self.open_n, self.note = parent, board, ready, open_n, note
        self.cat_id: Optional[int] = ready[0].cat.id if ready else None
        self.wager = arena.LADDER[0]
        self.hours = cr.DEFAULT_HOURS
        self.done = False
        self._build()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await self.parent.interaction_check(interaction)

    def _build(self) -> None:
        self.clear_items()
        rows = []
        if self.ready:
            cats = discord.ui.Select(placeholder="Your cat", options=[
                discord.SelectOption(label=f"{r.cat.name} · power {r.fighter.power}"[:100], value=str(r.cat.id),
                                     description=f"{r.cat.rarity}, rank {r.cat.cat_rank}", default=r.cat.id == self.cat_id)
                for r in self.ready[:25]])
            cats.callback = self._pick_cat
            rows.append(discord.ui.ActionRow(cats))
        wag = discord.ui.Select(placeholder="Wager", options=[
            discord.SelectOption(label=f"{w:,} coins", value=str(w), emoji="🪙", default=w == self.wager) for w in arena.LADDER])
        wag.callback = self._pick_wager
        rows.append(discord.ui.ActionRow(wag))
        hrs = discord.ui.Select(placeholder="Stays up for", options=[
            discord.SelectOption(label=f"{h} hour{'s' if h != 1 else ''}", value=str(h), default=h == self.hours) for h in HOUR_CHOICES])
        hrs.callback = self._pick_hours
        rows.append(discord.ui.ActionRow(hrs))
        rows.append(discord.ui.ActionRow(
            _btn("Post request", self._send, discord.ButtonStyle.success, "📮", disabled=not self.cat_id or self.done),
            _btn("My requests", self._mine, discord.ButtonStyle.primary, "🗂️"),
            _btn("Back to board", self._back)))
        self.add_item(_box(post_text(self.ready, self.cat_id, self.wager, self.hours, self.open_n, self.note), rows))

    async def _again(self, interaction: discord.Interaction, note: Optional[str] = None):
        self.note = note
        self._build()
        await vis.edit(interaction, view=self)

    async def _pick_cat(self, interaction: discord.Interaction):
        self.cat_id = int(interaction.data["values"][0])
        await self._again(interaction)

    async def _pick_wager(self, interaction: discord.Interaction):
        self.wager = int(interaction.data["values"][0])
        await self._again(interaction)

    async def _pick_hours(self, interaction: discord.Interaction):
        self.hours = cr.clamp_hours(interaction.data["values"][0])
        await self._again(interaction)

    async def _send(self, interaction: discord.Interaction):
        cur = next((r for r in self.ready if r.cat.id == self.cat_id), None)
        if cur is None:
            await self._again(interaction, "Pick a cat first.")
            return
        guild = getattr(interaction, "guild", None)
        try:
            res = await cr.post(interaction.user.id, _clone_id(interaction.client), cur.cat.id, self.wager, cur.fighter.power,
                                self.hours, getattr(interaction, "guild_id", None), getattr(guild, "name", None))
        except Exception:
            logger.exception("[cat-board] couldn't post a request")
            await self._again(interaction, OOPS)
            return
        if res.status != "ok":
            await self._again(interaction, "🚫 " + cr.STATUS_TEXT.get(res.status, "That didn't work."))
            return
        self.ready = [r for r in self.ready if r.cat.id != cur.cat.id]
        self.cat_id = self.ready[0].cat.id if self.ready else None
        self.open_n += 1
        await self._again(interaction, f"✅ **{cur.cat.name}** is on the board for {self.hours}h. {self.wager:,} {cat_wallet.COINS} "
                                       f"held ({res.coins:,} left). You get it back if nobody accepts.")

    async def _mine(self, interaction: discord.Interaction):
        await open_mine(interaction, self.parent, self.board)

    async def _back(self, interaction: discord.Interaction):
        await self.board._show(interaction)


async def open_post(interaction: discord.Interaction, parent, board: BoardView) -> None:
    try:
        ready = await _free_cats(interaction)
        open_n = len(await cr.mine(interaction.user.id, _clone_id(interaction.client)))
    except Exception:
        logger.exception("[cat-board] couldn't open the post screen")
        await board._show(interaction, OOPS)
        return
    await vis.edit(interaction, view=PostView(parent, board, ready, open_n))


# ── my requests ─────────────────────────────────────────────────────────

class MineView(discord.ui.LayoutView):
    def __init__(self, parent, board: BoardView, items: List[cr.Request], note: Optional[str] = None):
        super().__init__(timeout=600)
        self.parent, self.board, self.items, self.note = parent, board, items, note
        self.pick: Optional[int] = items[0].id if items else None
        self._build()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await self.parent.interaction_check(interaction)

    def _build(self) -> None:
        self.clear_items()
        rows = []
        if self.items:
            sel = discord.ui.Select(placeholder="Pick a request", options=[
                discord.SelectOption(label=f"{r.cat_name or 'Your cat'} · {r.wager:,} coins"[:100], value=str(r.id),
                                     default=r.id == self.pick) for r in self.items[:25]])
            sel.callback = self._select
            rows.append(discord.ui.ActionRow(sel))
        rows.append(discord.ui.ActionRow(
            _btn("Take it down", self._cancel, discord.ButtonStyle.danger, "🗑️", disabled=not self.items),
            _btn("Back to board", self._back)))
        self.add_item(_box(mine_text(self.items, self.note), rows))

    async def _again(self, interaction: discord.Interaction, note: Optional[str] = None):
        self.note = note
        self._build()
        await vis.edit(interaction, view=self)

    async def _select(self, interaction: discord.Interaction):
        self.pick = int(interaction.data["values"][0])
        await self._again(interaction)

    async def _cancel(self, interaction: discord.Interaction):
        if not self.pick:
            await self._again(interaction, "Pick a request first.")
            return
        try:
            res = await cr.cancel(self.pick, interaction.user.id)
            self.items = await cr.mine(interaction.user.id, _clone_id(interaction.client))
        except Exception:
            logger.exception("[cat-board] couldn't take a request down")
            await self._again(interaction, OOPS)
            return
        self.pick = self.items[0].id if self.items else None
        if res.status == "ok":
            await self._again(interaction, f"🗑️ Request taken down. {res.wager:,} {cat_wallet.COINS} are back in your wallet.")
        else:
            await self._again(interaction, "🚫 " + cr.STATUS_TEXT.get(res.status, "That request isn't open any more."))

    async def _back(self, interaction: discord.Interaction):
        await self.board._show(interaction)


async def open_mine(interaction: discord.Interaction, parent, board: BoardView) -> None:
    try:
        items = await cr.mine(interaction.user.id, _clone_id(interaction.client))
    except Exception:
        logger.exception("[cat-board] couldn't list my requests")
        await board._show(interaction, OOPS)
        return
    await vis.edit(interaction, view=MineView(parent, board, items))
