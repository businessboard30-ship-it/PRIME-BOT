# path: discord_bot/cogs/_views_card_chief.py

"""
Card Hub: 👑 Chief Seats screen. Lease one of this server's 5 chief seats for coins. No new command: one more screen in the
Card Hub, same persistence rules (custom_ids carry only screen + seat rank, never who is clicking; level, balance, seat
state and price are re-read from the database on every click). All rules live in modules/card_chief.py.
"""

from __future__ import annotations

import logging
from typing import Optional

import discord

import config as app_config
from discord_bot.cogs import _views_card_hub as hub
from modules import card_chief
from modules import card_economy as ce

logger = logging.getLogger(__name__)

ACTS = frozenset({"chief", "chiefpick", "chiefok"})
MODAL_ACTS: frozenset = frozenset()
GUARDED = frozenset({"chiefok"})


def _holder(ctx: hub.Ctx, seat: dict) -> str:
    if seat["user_id"] is None:
        return "*empty*"
    name = hub._display_name(ctx, seat["user_id"])
    if seat["leased"]:
        return f"{name} · 🪙 leased until <t:{hub._ts(seat['lease_until'])}:R>"
    return f"{name} · earned on XP rank"


async def screen_chief(ctx: hub.Ctx, note: str = "") -> discord.ui.LayoutView:
    if not ctx.guild_id:
        return hub._notice(ctx, "chief", "⚠️ Open the Card Hub inside a server to see its chief seats.", "home", "", "Home")
    seats = await card_chief.list_seats(ctx.guild_id, ctx.clone_id)
    lines = [f"**{s['seat_rank']}. 👑 {discord.utils.escape_markdown(s['clan_slug'])}** · {s['price']:,}🪙 · {_holder(ctx, s)}"
             for s in seats]
    text = "\n".join([
        "## 👑 Chief Seats", note,
        f"Lease a seat for **{app_config.CHIEF_SEAT_LEASE_DAYS} days**. You need level {app_config.CHIEF_MIN_LEVEL} in this server. "
        "A lease can take a seat from an XP-earned chief, but not from someone else's running lease. "
        f"When it ends the seat goes back to the XP top 5. One seat per person, up to {app_config.CHIEF_SEAT_MAX_LEASE_DAYS} days ahead.",
        "", *lines, "-# Only you can see this."])
    comps = []
    if app_config.CHIEF_SEAT_PURCHASE_ENABLED and seats:
        opts = [discord.SelectOption(label=f"Seat {s['seat_rank']} · {s['clan_slug']}"[:100], value=str(s["seat_rank"]),
                                     description=f"{s['price']:,} coins"[:100], emoji="👑") for s in seats]
        comps.append(discord.ui.ActionRow(hub.HubSelect("chiefpick", ctx.clone_id, ctx.guild_id, "", opts, "Lease a seat…")))
    return hub._shell(ctx, "chief", text, comps)


async def screen_confirm(ctx: hub.Ctx, rank: int) -> discord.ui.LayoutView:
    seats = {s["seat_rank"]: s for s in await card_chief.list_seats(ctx.guild_id, ctx.clone_id)}
    seat = seats.get(rank)
    if seat is None:
        return hub._notice(ctx, "chief", "⚠️ That seat doesn't exist.", "chief", "", "Back")
    mine = next((s for s in seats.values() if s["user_id"] == ctx.user_id), None)
    if seat["user_id"] == ctx.user_id:
        warn = "This **renews** your lease."
    elif seat["leased"]:
        return hub._notice(ctx, "chief", f"⚠️ Seat {rank} is leased right now. Try again when the lease ends.", "chief", "", "Back")
    elif mine is not None:
        return hub._notice(ctx, "chief", f"⚠️ You already hold seat {mine['seat_rank']}. One seat per person.", "chief", "", "Back")
    elif seat["user_id"] is not None:
        warn = f"⚠️ This takes the seat from {hub._display_name(ctx, seat['user_id'])}, who earned it on XP rank."
    else:
        warn = "This seat is empty."
    text = (f"## 👑 Lease seat {rank} · {discord.utils.escape_markdown(seat['clan_slug'])}\n{warn}\n"
            f"**Price:** {seat['price']:,}🪙 for {app_config.CHIEF_SEAT_LEASE_DAYS} days. Coins are only taken if the lease goes through.")
    return hub._shell(ctx, "chief", text, [discord.ui.ActionRow(
        hub._btn(ctx, "chiefok", f"s{rank}", "Confirm lease", "👑", discord.ButtonStyle.success),
        hub._btn(ctx, "chief", "", "Cancel", "✖"))])


async def do_buy(ctx: hub.Ctx, rank: int) -> discord.ui.LayoutView:
    try:
        r = await card_chief.buy_seat(ctx.user_id, ctx.clone_id, ctx.guild_id, rank)
    except Exception as e:
        if not isinstance(e, ce.StoreError):
            logger.warning("[card-chief] purchase failed", exc_info=True)
        return hub._notice(ctx, "chief", hub._err_text(e), "chief", "", "Back")
    try:        # tell the channel; the ousted chief should hear about it. Best effort, never blocks the purchase.
        who = f"<@{ctx.user_id}> " + ("renewed" if r["renewed"] else "leased")
        out = f" Seat taken from <@{r['displaced_id']}>." if r["displaced_id"] else ""
        users = [discord.Object(id=u) for u in (ctx.user_id, r["displaced_id"]) if u]
        if ctx.i.channel is not None:
            await ctx.i.channel.send(f"👑 {who} the **{r['clan_slug']}** chief seat.{out}",
                                     allowed_mentions=discord.AllowedMentions(users=users, roles=False, everyone=False))
    except Exception:
        logger.debug("[card-chief] announce failed", exc_info=True)
    return await screen_chief(ctx, f"✅ {'Renewed' if r['renewed'] else 'Leased'} seat {r['seat_rank']} until "
                                   f"<t:{hub._ts(r['lease_until'])}:f> for {r['price']:,}🪙. Balance: {r['balance']:,}🪙.")


async def route(ctx: hub.Ctx, act: str, arg: str, value: Optional[str]):
    try:
        if act == "chiefpick":
            return await screen_confirm(ctx, int(value))
        if act == "chiefok":
            return await do_buy(ctx, hub._int_arg(arg, "s"))
    except Exception as e:
        return hub._notice(ctx, "chief", hub._err_text(e), "chief", "", "Back")
    return await screen_chief(ctx)
