# path: discord_bot/cogs/_views_card_admin.py

"""
Card Hub: 🛠️ Admin screen (bot owner only). Create store cards (unlimited or limited edition), add rental/permanent offers,
replenish limited stock, show/hide a card. No new command: it is one more screen in the Card Hub, and the nav entry only
appears for DISCORD_CLONE_ADMIN_IDS. The check is repeated SERVER-SIDE on every click (a custom_id proves nothing), so a
forwarded or stale message can't give anyone else these controls. Same persistence rules as the rest of the hub.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

import discord

import config as app_config
from discord_bot.cogs import _views_card_hub as hub
from modules import card_economy as ce

logger = logging.getLogger(__name__)

ACTS = frozenset({"admin", "admnew", "admcard", "admrep", "admoff", "admtog"})
MODAL_ACTS = frozenset({"admnew", "admrep", "admoff"})
GUARDED = frozenset({"admtog"})


def is_admin(user_id: int) -> bool:
    return int(user_id) in app_config.DISCORD_CLONE_ADMIN_IDS


def _int(text: str, name: str):
    raw = re.sub(r"[,\s_]", "", str(text or ""))
    if not raw:
        raise ce.StoreError("bad_amount", f"Enter a number for {name}.")
    if not raw.isdigit():
        raise ce.StoreError("bad_amount", f"{name.capitalize()} must be a whole number.")
    return int(raw)


def _opt_int(text: str, name: str) -> Optional[int]:
    return None if not str(text or "").strip() else _int(text, name)


def _deny(ctx: hub.Ctx) -> discord.ui.LayoutView:
    return hub._notice(ctx, "home", "⚠️ That screen is for the bot owner.", "home", "", "Home")


class NewCardModal(discord.ui.Modal, title="New store card"):
    name = discord.ui.TextInput(label="Card name", min_length=2, max_length=60)
    rarity = discord.ui.TextInput(label="Rarity (common, rare, epic, legendary, god, supreme)", max_length=12, default="rare")
    strength = discord.ui.TextInput(label="Base strength", max_length=9, placeholder="100")
    price = discord.ui.TextInput(label="Price in coins (permanent)", max_length=12, placeholder="5000")
    stock = discord.ui.TextInput(label="Limited copies (blank = unlimited)", required=False, max_length=7)

    def __init__(self, clone_id, guild_id: int):
        super().__init__()
        self.clone_id, self.guild_id = clone_id, guild_id

    async def on_submit(self, interaction: discord.Interaction):
        from discord_bot.cogs import _views_card_trade as vt

        async def build(ctx: hub.Ctx):
            if not is_admin(ctx.user_id):
                raise ce.StoreError("denied", "That's for the bot owner.")
            r = await ce.create_card(str(self.name), str(self.rarity).strip().lower(), _int(self.strength, "base strength"),
                                     _int(self.price, "the price"), _opt_int(self.stock, "limited copies"),
                                     actor_id=ctx.user_id)
            return await screen_card(ctx, r["card_id"], "✅ Card created and live in the store.")
        await vt._modal_finish(interaction, self.clone_id, self.guild_id, build)


class ReplenishModal(discord.ui.Modal, title="Add stock"):
    amount = discord.ui.TextInput(label="Extra copies to release", max_length=7, placeholder="50")

    def __init__(self, clone_id, guild_id: int, card_id: int):
        super().__init__()
        self.clone_id, self.guild_id, self.card_id = clone_id, guild_id, card_id

    async def on_submit(self, interaction: discord.Interaction):
        from discord_bot.cogs import _views_card_trade as vt

        async def build(ctx: hub.Ctx):
            if not is_admin(ctx.user_id):
                raise ce.StoreError("denied", "That's for the bot owner.")
            left = await ce.replenish_stock(self.card_id, _int(self.amount, "the amount"), ctx.user_id)
            return await screen_card(ctx, self.card_id, f"✅ Stock added. {left:,} left to buy.")
        await vt._modal_finish(interaction, self.clone_id, self.guild_id, build)


class OfferModal(discord.ui.Modal, title="Add or re-price an offer"):
    kind = discord.ui.TextInput(label="permanent or rental", max_length=9, default="permanent")
    days = discord.ui.TextInput(label="Rental days (rental only)", required=False, max_length=4, placeholder="7")
    price = discord.ui.TextInput(label="Price in coins", max_length=12)

    def __init__(self, clone_id, guild_id: int, card_id: int):
        super().__init__()
        self.clone_id, self.guild_id, self.card_id = clone_id, guild_id, card_id

    async def on_submit(self, interaction: discord.Interaction):
        from discord_bot.cogs import _views_card_trade as vt

        async def build(ctx: hub.Ctx):
            if not is_admin(ctx.user_id):
                raise ce.StoreError("denied", "That's for the bot owner.")
            kind = str(self.kind).strip().lower()
            days = _opt_int(self.days, "rental days") if kind == "rental" else None
            await ce.create_offer(self.card_id, kind, _int(self.price, "the price"), days)
            return await screen_card(ctx, self.card_id, "✅ Offer saved.")
        await vt._modal_finish(interaction, self.clone_id, self.guild_id, build)


async def screen_admin(ctx: hub.Ctx, page: int = 0) -> discord.ui.LayoutView:
    if not is_admin(ctx.user_id):
        return _deny(ctx)
    rows = await ce.list_catalog(25, page * 25)
    has_next = len(rows) > 25
    rows = rows[:25]
    text = ("## 🛠️ Card admin\nCreate cards, set limited stock, add offers, release more copies. Pick a card to manage it.\n"
            "-# Only the bot owner sees this.")
    comps = [discord.ui.ActionRow(hub._btn(ctx, "admnew", "", "New card", "➕", discord.ButtonStyle.success))]
    if rows:
        opts = [discord.SelectOption(label=f"{c['name'][:60]}"[:100], value=str(c["card_id"]),
                                     description=(f"{str(c['rarity']).capitalize()} · "
                                                  + ("unlimited" if c["stock_total"] is None else f"{c['stock_remaining']}/{c['stock_total']} left")
                                                  + ("" if c["store_enabled"] else " · hidden"))[:100]) for c in rows]
        comps.append(discord.ui.ActionRow(hub.HubSelect("admcard", ctx.clone_id, ctx.guild_id, "", opts, "Manage a card…")))
    else:
        text += "\n\nNo cards yet. Create the first one."
    comps.append(hub._page_row(ctx, "admin", lambda p: f"p{p}", page, has_next))
    return hub._shell(ctx, "admin", text, comps)


async def screen_card(ctx: hub.Ctx, card_id: int, note: str = "") -> discord.ui.LayoutView:
    if not is_admin(ctx.user_id):
        return _deny(ctx)
    c = await ce.get_card_admin(card_id)
    if c is None:
        return hub._notice(ctx, "admin", "⚠️ That card no longer exists.", "admin", "", "Back")
    offers = "\n".join(f"- {'Buy' if o['kind'] == 'permanent' else 'Rent ' + str(o['rental_days']) + 'd'}: {o['price']:,}🪙"
                       + ("" if o["active"] else " (off)") for o in c["offers"]) or "- none"
    stock = "♾️ Unlimited" if c["stock_total"] is None else (
        f"📦 {c['stock_remaining']}/{c['stock_total']} left" + ("  ⛔ **OUT OF STOCK**" if c["stock_remaining"] <= 0 else ""))
    text = "\n".join([f"## 🛠️ {discord.utils.escape_markdown(c['name'])}", note,
                      f"{hub._rar(c['rarity'])} · 💪 {c['base_strength']:,} · {stock}",
                      f"Level {c['min_level']}+" + (f" up to {c['max_level']}" if c["max_level"] is not None else "") +
                      f" · {'tradeable' if c['tradeable'] else 'not tradeable'} · {c['copies']:,} copies owned · "
                      f"{'🟢 in store' if c['store_enabled'] else '⚫ hidden'}", "**Offers**", offers])
    limited = c["stock_total"] is not None
    comps = [discord.ui.ActionRow(
        hub._btn(ctx, "admoff", f"c{card_id}", "Add / re-price offer", "🏷️"),
        hub._btn(ctx, "admrep", f"c{card_id}", "Add stock", "📦", disabled=not limited),
        hub._btn(ctx, "admtog", f"c{card_id}", "Hide" if c["store_enabled"] else "Show in store", "👁️"),
        hub._btn(ctx, "admin", "", "Back", "◀"))]
    return hub._shell(ctx, "admin", text, comps)


async def route(ctx: hub.Ctx, act: str, arg: str, value: Optional[str]):
    if not is_admin(ctx.user_id):          # re-checked on EVERY click; the custom_id grants nothing
        return _deny(ctx)
    try:
        if act == "admin":
            return await screen_admin(ctx, hub._int_arg(arg, "p"))
        if act == "admnew":
            await ctx.i.response.send_modal(NewCardModal(ctx.clone_id, ctx.guild_id))
            return None
        if act == "admcard":
            return await screen_card(ctx, int(value))
        cid = hub._int_arg(arg, "c")
        if act == "admrep":
            await ctx.i.response.send_modal(ReplenishModal(ctx.clone_id, ctx.guild_id, cid))
            return None
        if act == "admoff":
            await ctx.i.response.send_modal(OfferModal(ctx.clone_id, ctx.guild_id, cid))
            return None
        if act == "admtog":
            c = await ce.get_card_admin(cid)
            if c is None:
                raise ce.StoreError("no_card", "Unknown card.")
            await ce.set_store_enabled(cid, not c["store_enabled"])
            return await screen_card(ctx, cid, "✅ Store visibility changed.")
    except Exception as e:
        return hub._notice(ctx, "admin", hub._err_text(e), "admin", "", "Back")
    return await screen_admin(ctx)
