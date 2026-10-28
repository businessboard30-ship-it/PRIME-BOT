# path: discord_bot/cogs/_views_card_trade.py

"""
Card Hub: the 💱 "Trade & games" screen. Same rules as the rest of the hub: pure Components V2, no new command, every
control is a persistent DynamicItem (HubButton / HubSelect from _views_card_hub) with NO view timeout, and everything that
matters (who you are, what you own, balances, the draft, the offer) is re-read from the database on every click.

  💱 Trade & games   your open offers (accept / decline / cancel) + start a global or local trade, send coins, play games
  ✏️ Draft           the offer you're building: pick cards on each side (stored in card_trade_drafts), set coins, send
  🎲 Games           Coin Flip, League Flip, Rob

An OFFER expires after card_trade.TRADE_TTL (30 min) by an explicit rule; the hub itself never expires. All rules (ownership,
rentals, locks, level/trade eligibility, atomic swap, daily caps, cooldowns) live in modules/card_*.py. This file only draws,
asks for IDs (same ID rules as the Cat Game box via resolve_user_ref) and calls them.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

import discord

from discord_bot.cogs import _views_card_hub as hub
from modules import card_economy as ce
from modules import card_games as cg
from modules import card_profile as cp
from modules import card_trade as ct

logger = logging.getLogger(__name__)

# acts handled here; the hub's route() hands these to route() below
ACTS = frozenset({"trade", "tnew", "tcards", "tadd", "tclr", "tcoin", "tdraft", "tsend", "tdrop", "tacc", "tdec",
                  "coins", "coinok", "games", "gcf", "gcfa", "glg", "glga", "grob"})
# acts that answer with a modal (so the hub must not defer first) and acts that must not run twice at once
MODAL_ACTS = frozenset({"tnew", "tcoin", "coins", "gcf", "glg", "grob"})
GUARDED = frozenset({"tacc", "tdec", "tsend", "coinok", "gcfa", "glga", "tadd"})

LEAGUE_KEYS = tuple(cg.LEAGUES)
SIDE_NAME = {"y": "give", "t": "get"}


# ---------------------------------------------------------------- small helpers
def _num(text: str, name: str, *, allow_empty: bool = False) -> int:
    raw = re.sub(r"[,\s_]", "", str(text or ""))
    if not raw:
        if allow_empty:
            return 0
        raise ce.StoreError("bad_amount", f"Enter a number for {name}.")
    if not raw.isdigit():
        raise ce.StoreError("bad_amount", f"{name.capitalize()} must be a whole number, like 250.")
    return int(raw)


async def _in_guild(client: discord.Client, guild_id: Optional[int], user_id: int) -> bool:
    """Server-side membership check for local trades. Unknown server / missing member = not a member."""
    if not guild_id:
        return False
    g = client.get_guild(int(guild_id))
    if g is None:
        return False
    if g.get_member(user_id) is not None:
        return True
    try:
        await g.fetch_member(user_id)
        return True
    except (discord.NotFound, discord.HTTPException):
        return False


async def _dm(client: discord.Client, user_id: int, text: str = None, view: discord.ui.LayoutView = None) -> bool:
    try:
        user = client.get_user(user_id) or await client.fetch_user(user_id)
        if view is not None:
            await user.send(view=view)
        else:
            await user.send(text)
        return True
    except Exception:
        return False


def _card_line(c: dict, mine: Optional[int] = None) -> str:
    gone = mine is not None and c.get("owner_id") != mine
    boost = f" · +{c['boost_level']}" if c.get("boost_level") else ""
    line = f"{hub._rar(c['rarity'])} **{discord.utils.escape_markdown(c['name'])}**{hub._serial(c)} · 💪 {c.get('strength', 0):,}{boost}"
    return line + ("  ⚠️ not available" if gone else "")


def _side_text(cards: list, coins: int, mine: Optional[int] = None) -> str:
    parts = [_card_line(c, mine) for c in cards]
    if coins:
        parts.append(f"🪙 **{coins:,}** coins")
    return "\n".join(f"- {p}" for p in parts) if parts else "- *nothing yet*"


def _scope_label(scope: str) -> str:
    return "🌍 Global" if scope == "global" else "🏠 Local"


def _offer_block(ctx: hub.Ctx, t: dict) -> str:
    me = ctx.user_id
    who_from, who_to = hub._display_name(ctx, t["initiator_id"]), hub._display_name(ctx, t["recipient_id"])
    head = f"**#{t['trade_id']}** · {_scope_label(t['scope'])} · {who_from} ➜ {who_to}"
    if t["initiator_id"] == me:
        offers, wants = "You give", "You get"
    else:
        offers, wants = "You get", "You give"
    exp = hub._ts(t["expires_at"])
    return (f"{head}\n**{offers}:**\n{_side_text(t['give'], t['give_coins'])}\n**{wants}:**\n"
            f"{_side_text(t['get'], t['get_coins'])}" + (f"\n-# Expires <t:{exp}:R>" if exp else ""))


def _fail(ctx: hub.Ctx, e: Exception, back_act: str = "trade", back_arg: str = "") -> discord.ui.LayoutView:
    if not isinstance(e, ce.StoreError):
        logger.warning("[card-trade] unexpected error", exc_info=e)
    return hub._notice(ctx, "trade", hub._err_text(e), back_act, back_arg, "Back")


# ---------------------------------------------------------------- modals
async def _modal_finish(interaction: discord.Interaction, clone_id, guild_id: int, build) -> None:
    """Shared tail for every modal: defer, build the next screen from the database, edit the hub message in place.
    A rule error is shown privately and leaves the screen as it was."""
    ctx = hub.Ctx(interaction, clone_id, guild_id)
    try:
        await interaction.response.defer()
        view = await build(ctx)
        await interaction.edit_original_response(view=view)
    except ce.StoreError as e:
        await _say(interaction, f"⚠️ {e}")
    except discord.NotFound:
        logger.debug("[card-trade] interaction expired", exc_info=True)
    except Exception:
        logger.warning("[card-trade] modal failed", exc_info=True)
        await _say(interaction, "⚠️ Something went wrong. Nothing was charged. Try again.")


async def _say(interaction: discord.Interaction, text: str) -> None:
    try:
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)
    except Exception:
        pass


async def _who(ctx: hub.Ctx, text: str) -> int:
    uid, err = await hub.resolve_user_ref(ctx.i, text)
    if err:
        raise ce.StoreError("bad_user", err)
    return uid


class NewTradeModal(discord.ui.Modal, title="New trade"):
    who = discord.ui.TextInput(label="Their Discord ID, @mention or trade ID", min_length=5, max_length=120,
                               placeholder="123456789012345678  or  142574D")
    give_coins = discord.ui.TextInput(label="Coins you give (optional)", required=False, max_length=10, placeholder="0")
    get_coins = discord.ui.TextInput(label="Coins you want (optional)", required=False, max_length=10, placeholder="0")

    def __init__(self, clone_id, guild_id: int, scope: str):
        super().__init__(title="New global trade" if scope == "global" else "New local trade")
        self.clone_id, self.guild_id, self.scope = clone_id, guild_id, scope

    async def on_submit(self, interaction: discord.Interaction):
        async def build(ctx: hub.Ctx):
            rid = await _who(ctx, str(self.who))
            gc, tc = _num(self.give_coins, "coins you give", allow_empty=True), _num(self.get_coins, "coins you want", allow_empty=True)
            if self.scope == "local":
                if ctx.guild_id in (0, None) or not await _in_guild(ctx.i.client, ctx.guild_id, rid):
                    raise ce.StoreError("not_in_server", "That user isn't a member of this server. Use a global trade instead.")
            await ct.start_draft(ctx.user_id, ctx.clone_id, rid, self.scope, ctx.guild_id, gc, tc)
            return await screen_draft(ctx)
        await _modal_finish(interaction, self.clone_id, self.guild_id, build)


class TradeCoinsModal(discord.ui.Modal, title="Coins in this trade"):
    give_coins = discord.ui.TextInput(label="Coins you give", required=False, max_length=10, placeholder="0")
    get_coins = discord.ui.TextInput(label="Coins you want", required=False, max_length=10, placeholder="0")

    def __init__(self, clone_id, guild_id: int):
        super().__init__()
        self.clone_id, self.guild_id = clone_id, guild_id

    async def on_submit(self, interaction: discord.Interaction):
        async def build(ctx: hub.Ctx):
            await ct.draft_set_coins(ctx.user_id, ctx.clone_id, _num(self.give_coins, "coins you give", allow_empty=True),
                                     _num(self.get_coins, "coins you want", allow_empty=True))
            return await screen_draft(ctx)
        await _modal_finish(interaction, self.clone_id, self.guild_id, build)


class SendCoinsModal(discord.ui.Modal, title="Send coins"):
    who = discord.ui.TextInput(label="Their Discord ID, @mention or trade ID", min_length=5, max_length=120,
                               placeholder="123456789012345678  or  142574D")
    amount = discord.ui.TextInput(label="Amount", max_length=10, placeholder="250")

    def __init__(self, clone_id, guild_id: int):
        super().__init__()
        self.clone_id, self.guild_id = clone_id, guild_id

    async def on_submit(self, interaction: discord.Interaction):
        async def build(ctx: hub.Ctx):
            uid = await _who(ctx, str(self.who))
            amount = _num(self.amount, "the amount")
            if amount <= 0 or amount > ct.MAX_TRANSFER:
                raise ce.StoreError("bad_amount", f"Amount must be between 1 and {ct.MAX_TRANSFER:,}.")
            return screen_coin_confirm(ctx, uid, amount)
        await _modal_finish(interaction, self.clone_id, self.guild_id, build)


class WagerModal(discord.ui.Modal, title="Place your bet"):
    wager = discord.ui.TextInput(label="Wager (coins)", max_length=7, placeholder="100")

    def __init__(self, clone_id, guild_id: int, game: str, league: str = ""):
        super().__init__(title="Coin Flip" if game == "cf" else f"League Flip · {league.title()}")
        self.clone_id, self.guild_id, self.game, self.league = clone_id, guild_id, game, league

    async def on_submit(self, interaction: discord.Interaction):
        async def build(ctx: hub.Ctx):
            return await play_wager(ctx, self.game, self.league, _num(self.wager, "your wager"))
        await _modal_finish(interaction, self.clone_id, self.guild_id, build)


class RobModal(discord.ui.Modal, title="Rob someone"):
    who = discord.ui.TextInput(label="Their Discord ID, @mention or trade ID", min_length=5, max_length=120,
                               placeholder="123456789012345678  or  142574D")

    def __init__(self, clone_id, guild_id: int):
        super().__init__()
        self.clone_id, self.guild_id = clone_id, guild_id

    async def on_submit(self, interaction: discord.Interaction):
        async def build(ctx: hub.Ctx):
            return await play_rob(ctx, await _who(ctx, str(self.who)))
        await _modal_finish(interaction, self.clone_id, self.guild_id, build)


# ---------------------------------------------------------------- screens
async def screen_trade(ctx: hub.Ctx) -> discord.ui.LayoutView:
    pend, draft, prof = await _gather(ctx)
    lines = ["## 💱 Trade & games", f"**🪙 Coins:** {prof['coins'] or 0:,}",
             "Trade cards or coins with anyone (🌍 global) or with someone in this server (🏠 local). Both people confirm, "
             f"offers last {int(ct.TRADE_TTL.total_seconds() // 60)} minutes, and the swap is all-or-nothing."]
    rows = [discord.ui.ActionRow(
        hub._btn(ctx, "tnew", "g", "Global trade", "🌍", discord.ButtonStyle.primary),
        hub._btn(ctx, "tnew", "l", "Local trade", "🏠", discord.ButtonStyle.primary),
        hub._btn(ctx, "coins", "", "Send coins", "🪙"),
        hub._btn(ctx, "games", "", "Games", "🎲"))]
    if draft:
        lines.append(f"\n✏️ You have an unfinished {_scope_label(draft['scope']).lower()} trade draft.")
        rows.append(discord.ui.ActionRow(hub._btn(ctx, "tdraft", "", "Continue draft", "✏️", discord.ButtonStyle.success)))
    lines.append("")
    if not pend["trades"]:
        lines.append("**Open offers:** none.")
    else:
        lines.append("**Open offers**")
    for t in pend["trades"]:
        lines.append(_offer_block(ctx, t))
        incoming = t["recipient_id"] == ctx.user_id
        buttons = ([hub._btn(ctx, "tacc", f"t{t['trade_id']}", f"Accept #{t['trade_id']}", "✅", discord.ButtonStyle.success),
                    hub._btn(ctx, "tdec", f"t{t['trade_id']}", "Decline", "✖️", discord.ButtonStyle.danger)] if incoming
                   else [hub._btn(ctx, "tdec", f"t{t['trade_id']}", f"Cancel #{t['trade_id']}", "✖️", discord.ButtonStyle.danger)])
        rows.append(discord.ui.ActionRow(*buttons))
    if pend["more"]:
        lines.append("-# More offers are waiting. Resolve these to see them.")
    lines.append("-# Only you can see this.")
    return hub._shell(ctx, "trade", "\n".join(lines), rows)


async def _gather(ctx: hub.Ctx):
    import asyncio
    return await asyncio.gather(ct.list_pending(ctx.user_id, ctx.clone_id, 4), ct.get_draft(ctx.user_id, ctx.clone_id),
                                cp.get_profile(ctx.user_id, ctx.user_id, ctx.clone_id))


async def screen_draft(ctx: hub.Ctx, note: str = "") -> discord.ui.LayoutView:
    d = await ct.get_draft(ctx.user_id, ctx.clone_id)
    if d is None:
        return hub._notice(ctx, "trade", "That trade draft is gone. Start a new one from the Trade screen.", "trade")
    cards = await ct.cards_by_ids(list(d["give"]) + list(d["get"]))
    give = [cards[i] for i in d["give"] if i in cards]
    get = [cards[i] for i in d["get"] if i in cards]
    text = "\n".join([
        f"## ✏️ Your {_scope_label(d['scope']).lower()} trade",
        f"**With:** {hub._display_name(ctx, d['recipient_id'])}  (`{d['recipient_id']}`)",
        "**You give:**", _side_text(give, d["give_coins"], ctx.user_id),
        "**You get:**", _side_text(get, d["get_coins"], d["recipient_id"]),
        note,
        f"-# Up to {ct.MAX_SIDE} cards per side. Nothing moves until they accept."])
    rows = [
        discord.ui.ActionRow(hub._btn(ctx, "tcards", "sy.p0", "Add my card", "🎴"), hub._btn(ctx, "tcards", "st.p0", "Add their card", "🎴"),
                             hub._btn(ctx, "tclr", "y", "Clear mine", "🧹"), hub._btn(ctx, "tclr", "t", "Clear theirs", "🧹")),
        discord.ui.ActionRow(hub._btn(ctx, "tcoin", "", "Set coins", "🪙"),
                             hub._btn(ctx, "tsend", "", "Send offer", "📨", discord.ButtonStyle.success),
                             hub._btn(ctx, "tdrop", "", "Discard", "🗑️", discord.ButtonStyle.danger)),
    ]
    return hub._shell(ctx, "trade", text, rows)


def _pick_option(c: dict) -> discord.SelectOption:
    boost = f" · +{c['boost_level']}" if c.get("boost_level") else ""
    serial = f" #{c['serial_no']:02d}/{c['stock_total']}" if c.get("serial_no") and c.get("stock_total") else ""
    return discord.SelectOption(label=f"{c['name'][:45]}{serial}"[:100], value=str(c["copy_id"]),
                                description=f"{str(c['rarity']).capitalize()} · 💪 {c['strength']:,}{boost}"[:100])


async def screen_pick(ctx: hub.Ctx, side_key: str, page: int) -> discord.ui.LayoutView:
    d = await ct.get_draft(ctx.user_id, ctx.clone_id)
    if d is None:
        return hub._notice(ctx, "trade", "That trade draft is gone. Start a new one from the Trade screen.", "trade")
    mine = side_key == "y"
    owner = ctx.user_id if mine else d["recipient_id"]
    if not mine and not (await cp.get_settings(owner, ctx.clone_id))["show_collection"]:
        return hub._notice(ctx, "trade", "🔒 They keep their collection private, so you can only ask them for coins.", "tdraft", "", "Back to draft")
    cards = await ct.list_tradeable(owner, ctx.clone_id, 25, page * 25)
    has_next = len(cards) > 25
    cards = cards[:25]
    chosen = d["give" if mine else "get"]
    who = "your" if mine else f"{hub._display_name(ctx, owner)}'s"
    text = (f"## 🎴 Pick {who} card\n**Picked so far:** {len(chosen)}/{ct.MAX_SIDE}. Pick one at a time; pick again to add more.\n"
            "-# Only cards that can be traded right now are listed (not rented, not already in another offer).")
    rows = []
    cards = [c for c in cards if c["copy_id"] not in chosen]
    if cards:
        rows.append(discord.ui.ActionRow(hub.HubSelect("tadd", ctx.clone_id, ctx.guild_id, side_key,
                                                      [_pick_option(c) for c in cards], "Choose a card…")))
    else:
        text += "\n\nNo tradeable cards on this page."
    rows.append(hub._page_row(ctx, "tcards", lambda p: f"s{side_key}.p{p}", page, has_next))
    rows.append(discord.ui.ActionRow(hub._btn(ctx, "tdraft", "", "Done", "✅", discord.ButtonStyle.success)))
    return hub._shell(ctx, "trade", text, rows)


def screen_coin_confirm(ctx: hub.Ctx, to_user: int, amount: int) -> discord.ui.LayoutView:
    text = (f"## 🪙 Send coins?\nSend **{amount:,}** coins to **{hub._display_name(ctx, to_user)}** (`{to_user}`).\n"
            f"Check the ID: coins can't be taken back. Daily send limit: {ct.DAILY_TRANSFER_CAP:,}.")
    return hub._shell(ctx, "trade", text, [discord.ui.ActionRow(
        hub._btn(ctx, "coinok", f"u{to_user}.a{amount}", f"Send {amount:,}", "✅", discord.ButtonStyle.success),
        hub._btn(ctx, "trade", "", "Cancel", "✖️"))])


async def do_send_coins(ctx: hub.Ctx, to_user: int, amount: int) -> discord.ui.LayoutView:
    if to_user == ctx.user_id:
        raise ce.StoreError("self", "You can't send coins to yourself.")
    bal = await ct.transfer_coins(ctx.user_id, to_user, ctx.clone_id, amount)
    await _dm(ctx.i.client, to_user, f"🪙 **{ctx.user.display_name}** sent you **{amount:,}** coins.")
    return hub._notice(ctx, "trade", f"✅ Sent **{amount:,}** coins to **{hub._display_name(ctx, to_user)}**.\n**Your balance:** {bal:,} 🪙",
                       "trade", "", "Back")


def screen_games(ctx: hub.Ctx) -> discord.ui.LayoutView:
    leagues = "\n".join(f"- **{k.title()}**: win {int(ch * 100)}% of the time, pays ×{m:g}" for k, (ch, m) in cg.LEAGUES.items())
    text = "\n".join([
        "## 🎲 Games",
        f"**Coin Flip:** double or nothing, {int(cg.COINFLIP_WIN_CHANCE * 100)}% to win. Wager {cg.MIN_WAGER}-{cg.MAX_WAGER:,} coins.",
        f"**League Flip:** pick a league, bigger payouts, lower odds:\n{leagues}",
        f"**Rob:** try to take {int(cg.ROB_STEAL_PCT[0] * 100)}-{int(cg.ROB_STEAL_PCT[1] * 100)}% of someone's coins "
        f"(up to {cg.ROB_MAX_STEAL:,}). {int(cg.ROB_SUCCESS * 100)}% chance. Fail and you pay them a fine. "
        f"Once an hour; they need {cg.ROB_MIN_VICTIM}+ coins; a robbed person is protected for {int(cg.ROB_SHIELD.total_seconds() // 3600)} hours.",
        "-# Games can cost you coins. Only play with coins you can afford to lose."])
    opts = [discord.SelectOption(label=f"{k.title()} league", value=k, description=f"{int(ch * 100)}% win · ×{m:g}")
            for k, (ch, m) in cg.LEAGUES.items()]
    rows = [discord.ui.ActionRow(hub._btn(ctx, "gcf", "", "Coin Flip", "🪙", discord.ButtonStyle.primary),
                                 hub._btn(ctx, "grob", "", "Rob", "🥷", discord.ButtonStyle.danger),
                                 hub._btn(ctx, "trade", "", "Back", "◀")),
            discord.ui.ActionRow(hub.HubSelect("glg", ctx.clone_id, ctx.guild_id, "", opts, "League Flip: pick a league…"))]
    return hub._shell(ctx, "trade", text, rows)


async def play_wager(ctx: hub.Ctx, game: str, league: str, wager: int) -> discord.ui.LayoutView:
    if game == "cf":
        res = await cg.coin_flip(ctx.user_id, ctx.clone_id, wager)
        title, again = "🪙 Coin Flip", f"w{wager}"
    else:
        res = await cg.league_flip(ctx.user_id, ctx.clone_id, league, wager)
        title, again = f"🎰 League Flip · {league.title()}", f"l{LEAGUE_KEYS.index(league)}.w{wager}"
    verdict = (f"🎉 **You won {res['delta']:,} coins!**" if res["won"] else f"💸 **You lost {-res['delta']:,} coins.**")
    text = f"## {title}\n{verdict}\n**Balance:** {res['balance']:,} 🪙"
    return hub._shell(ctx, "trade", text, [discord.ui.ActionRow(
        hub._btn(ctx, "gcfa" if game == "cf" else "glga", again, f"Same bet again ({wager:,})", "🔁", discord.ButtonStyle.primary),
        hub._btn(ctx, "games", "", "Games", "🎲"))])


async def play_rob(ctx: hub.Ctx, victim: int) -> discord.ui.LayoutView:
    res = await cg.rob(ctx.user_id, victim, ctx.clone_id)
    name = hub._display_name(ctx, victim)
    text = (f"## 🥷 Rob\n🎉 **Success!** You took **{res['amount']:,}** coins from {name}." if res["success"]
            else f"## 🥷 Rob\n🚨 **Caught!** You paid {name} a fine of **{res['amount']:,}** coins.")
    return hub._notice(ctx, "trade", text, "games", "", "Back to games")


# ---------------------------------------------------------------- offer actions
async def do_accept(ctx: hub.Ctx, trade_id: int) -> discord.ui.LayoutView:
    t = await ct.get_trade(trade_id)
    if t is None or t["status"] != "pending":
        return hub._notice(ctx, "trade", "⚠️ This offer is no longer open (it was completed, cancelled or expired).", "trade")
    if t["recipient_id"] != ctx.user_id:
        return hub._notice(ctx, "trade", "⚠️ This offer isn't addressed to you.", "trade")
    if t["clone_id"] != ctx.clone_id:
        return hub._notice(ctx, "trade", "⚠️ This offer belongs to a different bot.", "trade")
    client = ctx.i.client
    in_guild = True
    if t["scope"] == "local":
        in_guild = await _in_guild(client, t["guild_id"], ctx.user_id)
        if not await _in_guild(client, t["guild_id"], t["initiator_id"]):
            raise ce.StoreError("not_in_server", "The other person is no longer a member of that server.")
    await ct.accept_trade(trade_id, ctx.user_id, in_guild)
    await _dm(client, t["initiator_id"], f"✅ **{ctx.user.display_name}** accepted your trade **#{trade_id}**. The cards and coins have moved.")
    return hub._notice(ctx, "trade", f"✅ **Trade #{trade_id} complete.** The cards and coins have moved. Check 🎴 My cards.", "trade", "", "Back")


async def do_decline(ctx: hub.Ctx, trade_id: int) -> discord.ui.LayoutView:
    t = await ct.get_trade(trade_id)
    await ct.cancel_trade(trade_id, ctx.user_id)
    if t:
        other = t["initiator_id"] if t["recipient_id"] == ctx.user_id else t["recipient_id"]
        verb = "declined" if t["recipient_id"] == ctx.user_id else "cancelled"
        await _dm(ctx.i.client, other, f"✖️ **{ctx.user.display_name}** {verb} trade **#{trade_id}**.")
    return hub._notice(ctx, "trade", f"✖️ Trade #{trade_id} closed. Your cards are unlocked.", "trade", "", "Back")


async def do_send_draft(ctx: hub.Ctx) -> discord.ui.LayoutView:
    d = await ct.get_draft(ctx.user_id, ctx.clone_id)
    if d is None:
        return hub._notice(ctx, "trade", "That trade draft is gone. Start a new one.", "trade")
    in_guild = await _in_guild(ctx.i.client, d["guild_id"], d["recipient_id"]) if d["scope"] == "local" else False
    tid = await ct.send_draft(ctx.user_id, ctx.clone_id, in_guild)
    t = await ct.get_trade(tid)
    dm_view = offer_dm_view(ctx, t)
    delivered = await _dm(ctx.i.client, d["recipient_id"], view=dm_view)
    tail = ("They've been sent a private message with Accept / Decline buttons." if delivered else
            "I couldn't DM them (their DMs may be closed). They can accept it from 💱 Trade & games.")
    return hub._notice(ctx, "trade", f"📨 **Offer #{tid} sent** to {hub._display_name(ctx, d['recipient_id'])}.\n{tail}\n"
                                      f"It expires in {int(ct.TRADE_TTL.total_seconds() // 60)} minutes, and you can cancel it any time from the Trade screen.",
                       "trade", "", "Back")


def offer_dm_view(ctx: hub.Ctx, t: dict) -> discord.ui.LayoutView:
    """The message the recipient gets. Buttons are persistent DynamicItems and re-check everything when clicked."""
    me_view = hub.Ctx.__new__(hub.Ctx)
    me_view.__dict__.update(ctx.__dict__)
    me_view.user_id = t["recipient_id"]
    view = discord.ui.LayoutView(timeout=None)
    box = discord.ui.Container(accent_colour=hub.SOFT)
    box.add_item(discord.ui.TextDisplay(f"## 📨 Trade offer\n{_offer_block(me_view, t)}\n-# Both people confirm. Nothing moves unless you accept."[:3800]))
    box.add_item(discord.ui.Separator())
    box.add_item(discord.ui.ActionRow(
        hub._btn(ctx, "tacc", f"t{t['trade_id']}", f"Accept #{t['trade_id']}", "✅", discord.ButtonStyle.success),
        hub._btn(ctx, "tdec", f"t{t['trade_id']}", "Decline", "✖️", discord.ButtonStyle.danger)))
    view.add_item(box)
    return view


# ---------------------------------------------------------------- routing (called by the hub's route())
async def route(ctx: hub.Ctx, act: str, arg: str, value: Optional[str]):
    """Returns a LayoutView, or None when the action opened a modal. Rule errors become a friendly notice."""
    try:
        if act == "trade":
            return await screen_trade(ctx)
        if act == "games":
            return screen_games(ctx)
        if act == "tnew":
            await ctx.i.response.send_modal(NewTradeModal(ctx.clone_id, ctx.guild_id, "local" if arg == "l" else "global"))
            return None
        if act == "tcoin":
            await ctx.i.response.send_modal(TradeCoinsModal(ctx.clone_id, ctx.guild_id))
            return None
        if act == "coins":
            await ctx.i.response.send_modal(SendCoinsModal(ctx.clone_id, ctx.guild_id))
            return None
        if act == "gcf":
            await ctx.i.response.send_modal(WagerModal(ctx.clone_id, ctx.guild_id, "cf"))
            return None
        if act == "glg":
            if value not in cg.LEAGUES:
                return screen_games(ctx)
            await ctx.i.response.send_modal(WagerModal(ctx.clone_id, ctx.guild_id, "lg", value))
            return None
        if act == "grob":
            await ctx.i.response.send_modal(RobModal(ctx.clone_id, ctx.guild_id))
            return None
        if act == "tdraft":
            return await screen_draft(ctx)
        if act == "tcards":
            m = re.search(r"s([yt])", arg or "")
            return await screen_pick(ctx, m.group(1) if m else "y", hub._int_arg(arg, "p"))
        if act == "tadd":
            await ct.draft_add_cards(ctx.user_id, ctx.clone_id, SIDE_NAME.get(arg, "give"), [int(value)])
            return await screen_pick(ctx, arg if arg in SIDE_NAME else "y", 0)
        if act == "tclr":
            await ct.draft_clear_side(ctx.user_id, ctx.clone_id, SIDE_NAME.get(arg, "give"))
            return await screen_draft(ctx)
        if act == "tdrop":
            await ct.discard_draft(ctx.user_id, ctx.clone_id)
            return await screen_trade(ctx)
        if act == "tsend":
            return await do_send_draft(ctx)
        if act == "tacc":
            return await do_accept(ctx, hub._int_arg(arg, "t"))
        if act == "tdec":
            return await do_decline(ctx, hub._int_arg(arg, "t"))
        if act == "coinok":
            return await do_send_coins(ctx, hub._int_arg(arg, "u"), hub._int_arg(arg, "a"))
        if act == "gcfa":
            return await play_wager(ctx, "cf", "", hub._int_arg(arg, "w"))
        if act == "glga":
            idx = hub._int_arg(arg, "l")
            return await play_wager(ctx, "lg", LEAGUE_KEYS[idx] if idx < len(LEAGUE_KEYS) else LEAGUE_KEYS[0], hub._int_arg(arg, "w"))
    except Exception as e:           # a rule error or a bug: friendly private note, the hub itself stays usable
        back = "tdraft" if act in ("tcards", "tadd", "tclr", "tsend") and await ct.get_draft(ctx.user_id, ctx.clone_id) else \
            ("games" if act in ("gcfa", "glga") else "trade")
        return _fail(ctx, e, back)
    return await screen_trade(ctx)
