# path: discord_bot/cogs/_views_card_hub.py

"""
The Card Hub: ONE private Components V2 message (no embeds) that edits itself in place. It opens from the "🎴 Cards"
button on the XP leaderboard (no slash command), and has these screens, switched with the select at the top:

  🏠 Home      coins, global level, collection totals, what the next level-up pays
  🛒 Store     global card store, 5 per page, stock / level / rental labels, buy + confirm
  🎴 My cards  your collection (sort + rarity filter + paging) -> card detail -> boost with coins or a boost item
  👤 Profile   stats, featured card, privacy toggles, achievements, look another collector up by ID
  🏆 Rankings  collector leaderboards, this server or every server
  👑 Chief Seats    lease one of this server's 5 chief seats for coins (see _views_card_chief.py)
  💱 Trade & games  open offers, global/local trades, coin transfers, Coin Flip / League Flip / Rob (see _views_card_trade.py)

Persistence: every control is a DynamicItem whose custom_id carries ALL the state it needs (screen, page, sort, copy id)
and NOTHING about who is clicking; who is clicking, what they own, their level, balance, stock and rules are re-read from
the database on every click. Nothing here has a view timeout, so a hub message keeps working after a restart or days
later. Every game rule lives in modules/card_*.py (atomic transactions); this file only draws and calls them.
Colours are soft and muted on purpose: no neon, no glow.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Optional

import discord

from modules import card_boost as cb
from modules import card_economy as ce
from modules import card_profile as cp
from modules import card_rewards

logger = logging.getLogger(__name__)

# ---- look ----
SOFT = discord.Colour.from_rgb(139, 148, 163)
RARITY_COLOUR = {   # muted tones, readable on light and dark
    "common": discord.Colour.from_rgb(150, 156, 163), "rare": discord.Colour.from_rgb(130, 156, 190),
    "epic": discord.Colour.from_rgb(154, 134, 184), "legendary": discord.Colour.from_rgb(200, 180, 142),
    "god": discord.Colour.from_rgb(184, 138, 138), "supreme": discord.Colour.from_rgb(143, 179, 169)}
RARITY_ICON = {"common": "⚪", "rare": "🔵", "epic": "🟣", "legendary": "🟡", "god": "🔴", "supreme": "🟢"}
RARITIES = ("", "common", "rare", "epic", "legendary", "god", "supreme")
SORTS = (("newest", "Newest"), ("strongest", "Strongest"), ("rarest", "Rarest"), ("boosted", "Most boosted"))
ITEM_KEYS = tuple(cb.ITEM_TIERS)                      # index in a custom_id -> item key
ITEM_LABEL = {"boost_shard": "Boost Shard", "boost_core": "Boost Core", "boost_apex": "Boost Apex"}
SCREENS = (("home", "Home", "🏠"), ("store", "Store", "🛒"), ("col", "My cards", "🎴"),
           ("prof", "Profile", "👤"), ("ranks", "Rankings", "🏆"), ("trade", "Trade & games", "💱"),
           ("chief", "Chief Seats", "👑"))
STORE_PAGE = 5
COL_PAGE = 5
METRICS = (("unique", "Most unique cards"), ("copies", "Most card copies"), ("strength", "Highest collection strength"),
           ("boosts", "Most total boosts"), ("limited", "Most limited editions"), ("rarity", "Rarest collection"),
           ("trades", "Most successful trades"))
PRIVACY = (("show_coins", "Coin balance"), ("show_collection", "Collection"), ("show_trades", "Trade activity"))

_ARG = r"[A-Za-z0-9_.\-]*"
_inflight: set = set()           # race guard against double-clicks only; the database is the source of truth


def _cp(clone_id) -> str:
    return "-" if clone_id is None else str(int(clone_id))


def _cf(part: str):
    return None if part == "-" else int(part)


class Ctx:
    """Everything a screen needs. user_id comes from the interaction, never from a custom_id."""
    def __init__(self, interaction: discord.Interaction, clone_id, guild_id: int):
        self.i = interaction
        self.clone_id = clone_id
        self.guild_id = guild_id
        self.user = interaction.user
        self.user_id = interaction.user.id


# ---------------------------------------------------------------- persistent controls
class HubButton(discord.ui.DynamicItem[discord.ui.Button],
                template=rf"^chub:(?P<act>[a-z_]+):(?P<c>-|\d+):(?P<g>\d+):(?P<arg>{_ARG})$"):
    def __init__(self, act: str, clone_id, guild_id: int, arg: str = "", label: str = "", emoji: str = None,
                 style: discord.ButtonStyle = discord.ButtonStyle.secondary, disabled: bool = False):
        self.act, self.clone_id, self.guild_id, self.arg = act, clone_id, guild_id, arg
        super().__init__(discord.ui.Button(
            label=label or None, emoji=emoji, style=style, disabled=disabled,
            custom_id=f"chub:{act}:{_cp(clone_id)}:{guild_id}:{arg}"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match):
        return cls(match["act"], _cf(match["c"]), int(match["g"]), match["arg"])

    async def callback(self, interaction: discord.Interaction):
        await dispatch(interaction, self.act, self.clone_id, self.guild_id, self.arg, None)


class HubSelect(discord.ui.DynamicItem[discord.ui.Select],
                template=rf"^chubs:(?P<act>[a-z_]+):(?P<c>-|\d+):(?P<g>\d+):(?P<arg>{_ARG})$"):
    def __init__(self, act: str, clone_id, guild_id: int, arg: str = "", options: Optional[list] = None,
                 placeholder: str = ""):
        self.act, self.clone_id, self.guild_id, self.arg = act, clone_id, guild_id, arg
        super().__init__(discord.ui.Select(
            placeholder=placeholder or None, min_values=1, max_values=1,
            options=options or [discord.SelectOption(label="…", value="x")],
            custom_id=f"chubs:{act}:{_cp(clone_id)}:{guild_id}:{arg}"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match):
        return cls(match["act"], _cf(match["c"]), int(match["g"]), match["arg"])

    async def callback(self, interaction: discord.Interaction):
        await dispatch(interaction, self.act, self.clone_id, self.guild_id, self.arg, self.item.values[0])


class LookupModal(discord.ui.Modal, title="Look up a collector"):
    who = discord.ui.TextInput(label="Their Discord ID, @mention or trade ID", min_length=5, max_length=120,
                               placeholder="123456789012345678  or  142574D")

    def __init__(self, clone_id, guild_id: int):
        super().__init__()
        self.clone_id, self.guild_id = clone_id, guild_id

    async def on_submit(self, interaction: discord.Interaction):
        ctx = Ctx(interaction, self.clone_id, self.guild_id)
        try:
            target, err = await resolve_user_ref(interaction, str(self.who))
            if err:
                await interaction.response.send_message(err, ephemeral=True)
                return
            await interaction.response.defer()
            await interaction.edit_original_response(view=await screen_prof(ctx, target))
        except Exception:
            logger.warning("[card-hub] lookup failed", exc_info=True)
            if not interaction.response.is_done():
                await interaction.response.send_message("⚠️ I couldn't look that up. Try again.", ephemeral=True)


class HubOpenButton(discord.ui.DynamicItem[discord.ui.Button], template=r"^lvllb_cards:(\d+):(-|\d+)$"):
    """The leaderboard's 🎴 Cards button. Lives here (not in the leaderboard file) so the hub stays one module."""
    def __init__(self, guild_id: int, clone_id):
        self.guild_id, self.clone_id = guild_id, clone_id
        super().__init__(discord.ui.Button(label="Cards", emoji="🎴", style=discord.ButtonStyle.secondary,
                                           custom_id=f"lvllb_cards:{guild_id}:{_cp(clone_id)}"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match):
        return cls(int(match.group(1)), _cf(match.group(2)))

    async def callback(self, interaction: discord.Interaction):
        await open_card_hub(interaction, self.guild_id, self.clone_id)


DYNAMIC_ITEMS = (HubButton, HubSelect, HubOpenButton)


# ---------------------------------------------------------------- helpers
def _ts(dt) -> Optional[int]:
    try:
        return int(dt.timestamp())
    except Exception:
        return None


def _rar(r: str) -> str:
    return f"{RARITY_ICON.get(r, '⚪')} {str(r).capitalize()}"


def _stock_label(card: dict) -> str:
    if card.get("stock_total") is None:
        return "♾️ Unlimited"
    rem = card.get("stock_remaining") or 0
    return "⛔ **OUT OF STOCK**" if rem <= 0 else f"📦 {rem}/{card['stock_total']} left"


def _level_label(card: dict) -> str:
    parts = []
    if card.get("min_level"):
        parts.append(f"Lv {card['min_level']}+")
    if card.get("max_level") is not None:
        parts.append(f"up to Lv {card['max_level']}")
    return " · ".join(parts)


def _offer_label(o: dict) -> str:
    return f"Buy {o['price']:,}🪙" if o["kind"] == "permanent" else f"Rent {o['rental_days']}d {o['price']:,}🪙"


def _serial(card: dict) -> str:
    return f" **#{card['serial_no']:02d} of {card['stock_total']}**" if card.get("serial_no") and card.get("stock_total") else ""


def _nav(ctx: Ctx, current: str) -> discord.ui.ActionRow:
    from config import DISCORD_CLONE_ADMIN_IDS
    screens = SCREENS + ((("admin", "Card admin", "🛠️"),) if ctx.user_id in DISCORD_CLONE_ADMIN_IDS else ())
    opts = [discord.SelectOption(label=name, value=key, emoji=emo, default=(key == current)) for key, name, emo in screens]
    return discord.ui.ActionRow(HubSelect("nav", ctx.clone_id, ctx.guild_id, "", opts, "Go to…"))


def _shell(ctx: Ctx, current: str, text: str, rows=(), colour=None) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=None)
    box = discord.ui.Container(accent_colour=colour or SOFT)
    box.add_item(discord.ui.TextDisplay(text[:3800]))
    box.add_item(discord.ui.Separator())
    box.add_item(_nav(ctx, current))
    for r in rows:
        box.add_item(r)
    view.add_item(box)
    return view


def _btn(ctx: Ctx, act: str, arg: str, label: str, emoji: str = None, style=discord.ButtonStyle.secondary,
         disabled: bool = False) -> HubButton:
    return HubButton(act, ctx.clone_id, ctx.guild_id, arg, label, emoji, style, disabled)


def _notice(ctx: Ctx, current: str, text: str, back_act: str, back_arg: str = "", back_label: str = "Back") -> discord.ui.LayoutView:
    return _shell(ctx, current, text, [discord.ui.ActionRow(_btn(ctx, back_act, back_arg, back_label, "◀"))])


def _err_text(e: Exception) -> str:
    if isinstance(e, ce.StoreError):
        return f"⚠️ {e}"
    return "⚠️ Something went wrong. Nothing was charged. Try again in a moment."


def _page_row(ctx: Ctx, act: str, make_arg, page: int, has_next: bool) -> discord.ui.ActionRow:
    return discord.ui.ActionRow(
        _btn(ctx, act, make_arg(page - 1), "Prev", "◀", disabled=page <= 0),
        discord.ui.Button(label=f"Page {page + 1}", disabled=True, style=discord.ButtonStyle.secondary,
                          custom_id=f"chub_pg:{act}:{page}"),
        _btn(ctx, act, make_arg(page + 1), "Next", "▶", disabled=not has_next))


def _int_arg(arg: str, prefix: str, default: int = 0) -> int:
    m = re.search(rf"(?:^|\.){prefix}(\d+)(?:\.|$)", arg or "")
    return int(m.group(1)) if m else default


# ---------------------------------------------------------------- resolving "who" (reuses the Cat Game ID rules)
async def resolve_user_ref(interaction: discord.Interaction, text: str):
    """(user_id, None) or (None, friendly error). Accepts a Discord ID, an @mention, or a Cat Game trade ID. Wrong
    guesses are rate limited with the same counter the Cat Game ID box uses, and unknown and unreachable get one answer."""
    from modules import cat_ids
    raw = (text or "").strip()
    m = re.fullmatch(r"<@!?(\d{15,20})>|(\d{15,20})", raw)
    if m:
        uid = int(m.group(1) or m.group(2))
        if uid == interaction.user.id:
            return None, "That's you 😄"
        if not cat_ids.lookup_allowed(interaction.user.id):
            return None, "That's a lot of lookups that didn't match anyone. Please wait a little while and try again."
        user = interaction.client.get_user(uid)
        if user is None:
            try:
                user = await interaction.client.fetch_user(uid)
            except discord.HTTPException:
                user = None
        if user is None or user.bot:
            cat_ids.note_failed_lookup(interaction.user.id)
            return None, "🤔 I can't find a person with that ID."
        return uid, None
    if cat_ids.parse(raw) is None:
        return None, ("🤔 That doesn't look like an ID. Use their Discord ID (17-20 digits), an @mention, or their trade ID "
                      "(6 digits and a letter, like `142574D`).")
    if not cat_ids.lookup_allowed(interaction.user.id):
        return None, "That's a lot of IDs that don't exist. Please wait a little while and try again."
    ident = await cat_ids.find_identity(raw)
    if not ident or ident[0] == interaction.user.id:
        cat_ids.note_failed_lookup(interaction.user.id)
        return None, "🤔 I don't know that ID."
    return ident[0], None


def _display_name(ctx: Ctx, user_id: int) -> str:
    g = ctx.i.guild
    m = g.get_member(user_id) if g else None
    u = m or ctx.i.client.get_user(user_id)
    return discord.utils.escape_markdown(u.display_name if u else f"User {user_id}")


# ---------------------------------------------------------------- screens
async def screen_home(ctx: Ctx) -> discord.ui.LayoutView:
    prof, lvl = await asyncio.gather(cp.get_profile(ctx.user_id, ctx.user_id, ctx.clone_id), ce.global_level(ctx.user_id))
    st = prof["stats"] or {}
    text = "\n".join([
        "## 🎴 Card Hub",
        f"**🪙 Coins:** {prof['coins'] or 0:,}",
        f"**⭐ Level:** {lvl} (across every server)",
        f"**🎴 Cards:** {st.get('copies', 0)} cop{'y' if st.get('copies', 0) == 1 else 'ies'} · {st.get('unique', 0)} unique"
        f" · 💪 {st.get('strength', 0):,} total strength",
        f"**🎁 Next level-up pays:** {card_rewards.reward_for(lvl + 1):,} coins (each level pays once)",
        *([f"**🧩 Next boost item:** a {ITEM_LABEL[card_rewards.item_for(nl)[0]]} at level {nl}"]
          if (nl := next(( l for l in range(lvl + 1, lvl + 6) if card_rewards.item_for(l)), None)) else []),
        "",
        "Earn coins by leveling up, then spend them in the 🛒 Store or on boosts, trade with friends or play a game in 💱 Trade & games. Pick a screen below.",
        "-# Only you can see this."])
    return _shell(ctx, "home", text)


async def _store_page(ctx: Ctx, page: int):
    rows = await ce.list_store(ctx.clone_id, limit=STORE_PAGE + 1, offset=page * STORE_PAGE)
    has_next = len(rows) > STORE_PAGE
    rows = rows[:STORE_PAGE]
    for r in rows:
        r["offers"] = ce._jl(r.get("offers"), [])
        r["skills"] = ce._jl(r.get("skills"), [])
    return rows, has_next


async def screen_store(ctx: Ctx, page: int = 0) -> discord.ui.LayoutView:
    page = max(0, page)
    rows, has_next = await _store_page(ctx, page)
    if not rows:
        text = "## 🛒 Card Store\nNothing is on sale right now. Check back soon." if page == 0 else \
            "## 🛒 Card Store\nThat page is empty."
        return _shell(ctx, "store", text, [discord.ui.ActionRow(_btn(ctx, "store", "p0", "First page", "⏮"))] if page else [])
    lines = ["## 🛒 Card Store", "-# Stock is shared by every server. Limited cards show their serial number once bought."]
    options = []
    for r in rows:
        offers = " · ".join(_offer_label(o) for o in sorted(r["offers"], key=lambda o: (o["kind"] != "permanent", o["price"]))) \
            or "Not for sale"
        skills = f" · ✨ {len(r['skills'])} skill{'s' if len(r['skills']) != 1 else ''}" if r["skills"] else ""
        lines.append(f"**{r.get('emoji') or '🎴'} {discord.utils.escape_markdown(r['name'])}** · {_rar(r['rarity'])} · "
                     f"💪 {r['base_strength']}{skills}\n{_stock_label(r)}"
                     f"{(' · ' + _level_label(r)) if _level_label(r) else ''} · {offers}")
        sold_out = r.get("stock_total") is not None and (r.get("stock_remaining") or 0) <= 0
        if not sold_out:
            for o in r["offers"]:
                options.append(discord.SelectOption(
                    label=f"{r['name']} · {_offer_label(o)}"[:100], value=str(o["offer_id"]),
                    description=(_level_label(r) or str(r["rarity"]).capitalize())[:100]))
    rows_ui = []
    if options:
        rows_ui.append(discord.ui.ActionRow(HubSelect("pick", ctx.clone_id, ctx.guild_id, f"p{page}", options[:25],
                                                      "Choose a card to buy or rent…")))
    rows_ui.append(_page_row(ctx, "store", lambda p: f"p{max(0, p)}", page, has_next))
    return _shell(ctx, "store", "\n\n".join(lines), rows_ui)


async def screen_confirm(ctx: Ctx, offer_id: int, page: int) -> discord.ui.LayoutView:
    o, prof, lvl = await asyncio.gather(ce.get_offer(ctx.clone_id, offer_id),
                                        cp.get_profile(ctx.user_id, ctx.user_id, ctx.clone_id), ce.global_level(ctx.user_id))
    back = discord.ui.ActionRow(_btn(ctx, "store", f"p{page}", "Back to store", "◀"))
    if o is None:
        return _shell(ctx, "store", "⚠️ That offer isn't available any more.", [back])
    bal = prof["coins"] or 0
    problems = []
    if o.get("stock_total") is not None and (o.get("stock_remaining") or 0) <= 0:
        problems.append("⛔ **OUT OF STOCK**")
    if lvl < o["min_level"]:
        problems.append(f"🔒 Needs level {o['min_level']} (you are level {lvl}).")
    if o.get("max_level") is not None and lvl > o["max_level"]:
        problems.append(f"🔒 Only for level {o['max_level']} and below (you are level {lvl}).")
    if bal < o["price"]:
        problems.append(f"🪙 You need {o['price'] - bal:,} more coins.")
    kind = (f"**Rental, {o['rental_days']} days.** The card disappears when the rental ends, and a rented card can't be "
            f"traded or boosted.") if o["kind"] == "rental" else "**Permanent.** It's yours to keep, boost and trade."
    text = "\n".join([
        "## 🛒 Confirm purchase",
        f"**{o.get('emoji') or '🎴'} {discord.utils.escape_markdown(o['name'])}** · {_rar(o['rarity'])} · 💪 {o['base_strength']}",
        kind, f"**Price:** {o['price']:,} 🪙 · **Your coins:** {bal:,} → {max(0, bal - o['price']):,}", _stock_label(o),
        *problems])
    ok = _btn(ctx, "buyok", f"o{offer_id}.p{page}", "Confirm", "✅", discord.ButtonStyle.success, disabled=bool(problems))
    return _shell(ctx, "store", text, [discord.ui.ActionRow(ok, _btn(ctx, "store", f"p{page}", "Cancel", "✖"))],
                  RARITY_COLOUR.get(o["rarity"]))


async def do_buy(ctx: Ctx, offer_id: int, page: int) -> discord.ui.LayoutView:
    lvl = await ce.global_level(ctx.user_id)          # read here, server-side, never from the button
    try:
        res = await ce.buy_offer(ctx.user_id, ctx.clone_id, offer_id, lvl)
    except Exception as e:
        if not isinstance(e, ce.StoreError):
            logger.warning("[card-hub] purchase failed", exc_info=True)
        return _notice(ctx, "store", _err_text(e), "store", f"p{page}", "Back to store")
    new = []
    try:
        new = await cp.check_achievements(ctx.user_id, ctx.clone_id)
    except Exception:
        logger.warning("[card-hub] achievement check failed", exc_info=True)
    card = await ce.get_offer(ctx.clone_id, offer_id) or {}
    serial = f" **#{res['serial_no']:02d} of {res['stock_total']}**" if res.get("serial_no") else ""
    when = f"\nRental ends <t:{_ts(res['rental_expires_at'])}:R>." if res.get("rental_expires_at") else ""
    ach = ("\n🏅 New achievement: " + ", ".join(cp.ACHIEVEMENTS[k][0] for k in new if k in cp.ACHIEVEMENTS)) if new else ""
    text = (f"## ✅ {'Rented' if res['kind'] == 'rental' else 'Bought'}\n**{card.get('name', 'Card')}**{serial} is in your "
            f"collection.\n**Paid:** {res['price']:,} 🪙 · **Left:** {res['balance']:,} 🪙{when}{ach}")
    return _shell(ctx, "store", text, [discord.ui.ActionRow(_btn(ctx, "col", "p0.s0.r0", "View my cards", "🎴"),
                                                              _btn(ctx, "store", f"p{page}", "Back to store", "◀"))],
                  RARITY_COLOUR.get(card.get("rarity")))


# ---- collection ----
def _col_arg(page: int, sort: int, rar: int) -> str:
    return f"p{max(0, page)}.s{sort}.r{rar}"


async def screen_col(ctx: Ctx, page: int = 0, sort: int = 0, rar: int = 0) -> discord.ui.LayoutView:
    sort = sort if 0 <= sort < len(SORTS) else 0
    rar = rar if 0 <= rar < len(RARITIES) else 0
    page = max(0, page)
    res = await cp.browse_collection(ctx.user_id, ctx.user_id, ctx.clone_id, rarity=RARITIES[rar] or None,
                                     sort=SORTS[sort][0], limit=COL_PAGE, offset=page * COL_PAGE)
    total, cards = res["total"], res["cards"]
    arg = _col_arg(page, sort, rar)
    sort_sel = discord.ui.ActionRow(HubSelect(
        "colsort", ctx.clone_id, ctx.guild_id, arg,
        [discord.SelectOption(label=f"Sort: {lbl}", value=str(i), default=(i == sort)) for i, (_k, lbl) in enumerate(SORTS)],
        "Sort"))
    rar_sel = discord.ui.ActionRow(HubSelect(
        "colrar", ctx.clone_id, ctx.guild_id, arg,
        [discord.SelectOption(label=("Rarity: all" if not r else f"Rarity: {r.capitalize()}"), value=str(i), default=(i == rar))
         for i, r in enumerate(RARITIES)], "Rarity"))
    if not cards:
        text = "## 🎴 My cards\nNo cards here yet. Buy one in the 🛒 Store." if not (total or rar) else \
            "## 🎴 My cards\nNo cards match this filter."
        return _shell(ctx, "col", text, [sort_sel, rar_sel])
    lines = [f"## 🎴 My cards · {total} cop{'y' if total == 1 else 'ies'}",
             f"-# Sorted by {SORTS[sort][1].lower()}" + (f" · {RARITIES[rar]} only" if rar else "")]
    options = []
    for c in cards:
        flags = []
        if c["boost_level"]:
            flags.append(f"+{c['boost_level']}")
        if c["rental_expires_at"]:
            flags.append(f"⏳ ends <t:{_ts(c['rental_expires_at'])}:R>")
        if c["trade_locked"]:
            flags.append("🔒 in a trade")
        if c["duplicate"]:
            flags.append("duplicate")
        if c["tradeable"] and not c["rental_expires_at"] and not c["trade_locked"]:
            flags.append("tradeable")
        lines.append(f"**{discord.utils.escape_markdown(c['name'])}**{_serial(c)} · {_rar(c['rarity'])} · 💪 {c['strength']}"
                     + (f" · {' · '.join(flags)}" if flags else ""))
        options.append(discord.SelectOption(label=f"{c['name']}{' #' + str(c['serial_no']) if c['serial_no'] else ''}"[:100],
                                            value=str(c["copy_id"]), description=f"{str(c['rarity']).capitalize()} · 💪 {c['strength']}"))
    rows = [discord.ui.ActionRow(HubSelect("inspect", ctx.clone_id, ctx.guild_id, arg, options, "Inspect a card…")),
            sort_sel, rar_sel,
            _page_row(ctx, "col", lambda p: _col_arg(p, sort, rar), page, (page + 1) * COL_PAGE < total)]
    return _shell(ctx, "col", "\n".join(lines), rows)


async def screen_detail(ctx: Ctx, copy_id: int, back: str, note: str = "") -> discord.ui.LayoutView:
    d, items = await asyncio.gather(cp.get_copy_detail(ctx.user_id, copy_id, ctx.clone_id), cb.list_items(ctx.user_id, ctx.clone_id))
    to_list = _btn(ctx, "col", back, "Back to my cards", "◀")
    if d is None or d["owner_id"] != ctx.user_id:
        return _shell(ctx, "col", "⚠️ That card isn't in your collection any more.", [discord.ui.ActionRow(to_list)])
    prof = await cp.get_profile(ctx.user_id, ctx.user_id, ctx.clone_id)
    bal = prof["coins"] or 0
    plan = cb.next_boost_plan(d["rarity"], d["boost_level"], d["max_boost_level"])
    skills = d["skills"]
    skill_lines = [f"{'✅' if s in d['active_skills'] else '🔒'} **{s.get('name', s.get('key', 'Skill'))}**"
                   f" (unlocks at boost {int(s.get('unlock_level', 0))})" for s in skills] or ["No skills."]
    nxt = d["next_skill"]
    rented = d["rental_expires_at"] is not None
    lines = [f"## {d.get('emoji') or '🎴'} {discord.utils.escape_markdown(d['name'])}{_serial(d)}",
             f"{_rar(d['rarity'])} · **💪 {d['strength']}** (base {d['base_strength']}) · boost **{d['boost_level']}/{d['max_boost_level']}**",
             *skill_lines]
    if nxt:
        lines.append(f"-# Next skill: {nxt.get('name', nxt.get('key'))} at boost {int(nxt['unlock_level'])}")
    if rented:
        lines.append(f"⏳ Rental, ends <t:{_ts(d['rental_expires_at'])}:R>. Rented cards can't be boosted or traded.")
    elif d["trade_locked"]:
        lines.append("🔒 In a pending trade.")
    elif not d["tradeable"]:
        lines.append("🚫 This card can't be traded.")
    if plan and not rented:
        need = (f" · needs a **{ITEM_LABEL[next(k for k, t in cb.ITEM_TIERS.items() if t == plan['tier'])]}** or better"
                if plan["tier"] else "")
        lines.append(f"**Next boost (to {plan['target']}):** {plan['coins']:,} 🪙{need} · you have {bal:,} 🪙")
    elif not plan:
        lines.append("✨ Fully boosted.")
    if note:
        lines.append(note)
    body = "\n".join(lines)

    view = discord.ui.LayoutView(timeout=None)
    box = discord.ui.Container(accent_colour=RARITY_COLOUR.get(d["rarity"], SOFT))
    img = d.get("image_url")
    if isinstance(img, str) and img.startswith(("http://", "https://")):
        sec = discord.ui.Section(accessory=discord.ui.Thumbnail(media=img))
        sec.add_item(discord.ui.TextDisplay(body[:3800]))
        box.add_item(sec)
    else:
        box.add_item(discord.ui.TextDisplay(body[:3800]))
    box.add_item(discord.ui.Separator())
    box.add_item(_nav(ctx, "col"))
    can_boost = bool(plan) and not rented and not d["trade_locked"]
    acts = []
    if can_boost:
        acts.append(_btn(ctx, "boost", f"c{copy_id}.i0.{back}", f"Boost · {plan['coins']:,}🪙", "⬆",
                         discord.ButtonStyle.primary, disabled=(plan["tier"] > 0 or bal < plan["coins"])))
        for idx, key in enumerate(ITEM_KEYS, start=1):
            if items.get(key) and cb.ITEM_TIERS[key] >= plan["tier"]:
                acts.append(_btn(ctx, "boost", f"c{copy_id}.i{idx}.{back}", f"Use {ITEM_LABEL[key]} ×{items[key]}", "💠"))
    if not rented:
        acts.append(_btn(ctx, "feature", f"c{copy_id}.{back}", "Make signature", "⭐"))
    acts.append(to_list)
    for i in range(0, len(acts), 5):
        box.add_item(discord.ui.ActionRow(*acts[i:i + 5]))
    view.add_item(box)
    return view


async def do_boost(ctx: Ctx, copy_id: int, item_idx: int, back: str) -> discord.ui.LayoutView:
    item = ITEM_KEYS[item_idx - 1] if 1 <= item_idx <= len(ITEM_KEYS) else None
    try:
        res = await cb.boost_card(ctx.user_id, ctx.clone_id, copy_id, use_item=item)
    except Exception as e:
        if not isinstance(e, ce.StoreError):
            logger.warning("[card-hub] boost failed", exc_info=True)
        return await screen_detail(ctx, copy_id, back, _err_text(e))
    new = []
    try:
        new = await cp.check_achievements(ctx.user_id, ctx.clone_id)
    except Exception:
        logger.warning("[card-hub] achievement check failed", exc_info=True)
    paid = f"{res['coins_spent']:,} 🪙" if not res["item"] else ITEM_LABEL.get(res["item"], "item")
    extra = ("\n🏅 New achievement: " + ", ".join(cp.ACHIEVEMENTS[k][0] for k in new if k in cp.ACHIEVEMENTS)) if new else ""
    return await screen_detail(ctx, copy_id, back, f"✅ Boosted to **{res['level']}** for {paid}. Strength is now **{res['strength']}**.{extra}")


async def do_feature(ctx: Ctx, copy_id: int, back: str) -> discord.ui.LayoutView:
    try:
        st = await cp.get_settings(ctx.user_id, ctx.clone_id)
        ids = [i for i in (st["featured_copy_ids"] or []) if i != copy_id]
        await cp.set_featured(ctx.user_id, ctx.clone_id, ids[: cp.MAX_FEATURED - 1] + [copy_id], signature=copy_id)
    except Exception as e:
        if not isinstance(e, ce.StoreError):
            logger.warning("[card-hub] feature failed", exc_info=True)
        return await screen_detail(ctx, copy_id, back, _err_text(e))
    return await screen_detail(ctx, copy_id, back, "⭐ This is now your signature card, shown at the top of your profile.")


# ---- profile ----
async def screen_prof(ctx: Ctx, target_id: Optional[int] = None) -> discord.ui.LayoutView:
    target_id = target_id or ctx.user_id
    own = target_id == ctx.user_id
    prof, ach = await asyncio.gather(cp.get_profile(ctx.user_id, target_id, ctx.clone_id), cp.list_achievements(target_id, ctx.clone_id))
    st = prof["stats"]
    name = "Your collection" if own else f"{_display_name(ctx, target_id)}'s collection"
    lines = [f"## 👤 {name}"]
    lines.append(f"**🪙 Coins:** {prof['coins']:,}" if prof["coins"] is not None else "**🪙 Coins:** 🔒 private")
    if st is None:
        lines.append("🔒 This collector keeps their collection private.")
    else:
        lines.append(f"**🎴 Cards:** {st['copies']} cop{'y' if st['copies'] == 1 else 'ies'} · {st['unique']} unique · 💪 {st['strength']:,} strength · "
                     f"+{st['boosts']} boosts")
        lines.append(f"**🏅 Rarity:** {st['highest_rarity'] and st['highest_rarity'].capitalize() or 'none yet'} highest · "
                     f"{st['limited']} limited · {st['high_rarity']} high-rarity")
        lines.append(f"**📚 Completion:** {st['completion_pct']}% of {st['catalogue_size']} cards")
    lines.append(f"**🔁 Trades:** {prof['trades']}" if prof["trades"] is not None else "**🔁 Trades:** 🔒 private")
    if ach:
        lines.append("**🏅 Achievements:** " + ", ".join(a["title"] for a in ach[:6]) + (" …" if len(ach) > 6 else ""))
    sig = prof["signature"]
    if sig:
        lines.append(f"\n⭐ **Signature:** {discord.utils.escape_markdown(sig['name'])}"
                     f"{' #' + str(sig['serial_no']) if sig['serial_no'] else ''} · {_rar(sig['rarity'])} · "
                     f"💪 {sig['strength']} · boost {sig['boost_level']}")
    others = [c for c in prof["featured"] if not sig or c["copy_id"] != sig["copy_id"]]
    if others:
        lines.append("**Featured:** " + " · ".join(f"{discord.utils.escape_markdown(c['name'])} (💪 {c['strength']})" for c in others))
    body = "\n".join(lines)

    view = discord.ui.LayoutView(timeout=None)
    box = discord.ui.Container(accent_colour=RARITY_COLOUR.get(sig["rarity"], SOFT) if sig else SOFT)
    who = ctx.user if own else (ctx.i.guild.get_member(target_id) if ctx.i.guild else None) or ctx.i.client.get_user(target_id)
    if who is not None:
        sec = discord.ui.Section(accessory=discord.ui.Thumbnail(media=who.display_avatar.url))
        sec.add_item(discord.ui.TextDisplay(body[:3800]))
        box.add_item(sec)
    else:
        box.add_item(discord.ui.TextDisplay(body[:3800]))
    box.add_item(discord.ui.Separator())
    box.add_item(_nav(ctx, "prof"))
    if own:
        s = prof["settings"]
        box.add_item(discord.ui.ActionRow(*[
            _btn(ctx, "priv", key, f"{label}: {'shown' if s.get(key) else 'private'}", "👁" if s.get(key) else "🙈")
            for key, label in PRIVACY]))
        box.add_item(discord.ui.ActionRow(_btn(ctx, "ach", "", "Achievements", "🏅"),
                                          _btn(ctx, "lookup", "", "Look up a collector", "🔎")))
    else:
        box.add_item(discord.ui.ActionRow(_btn(ctx, "prof", "", "My profile", "◀"),
                                          _btn(ctx, "lookup", "", "Look up another", "🔎")))
    view.add_item(box)
    return view


async def do_privacy(ctx: Ctx, field: str) -> discord.ui.LayoutView:
    if field in {k for k, _ in PRIVACY}:
        s = await cp.get_settings(ctx.user_id, ctx.clone_id)
        await cp.update_settings(ctx.user_id, ctx.clone_id, **{field: not s.get(field)})
    return await screen_prof(ctx)


async def screen_ach(ctx: Ctx) -> discord.ui.LayoutView:
    stats, earned = await asyncio.gather(cp.get_stats(ctx.user_id, ctx.clone_id), cp.list_achievements(ctx.user_id, ctx.clone_id))
    got = {a["key"] for a in earned}
    lines = ["## 🏅 Achievements", "-# Badges and titles only. They never pay coins or boosts."]
    for key, (title, stat, need) in cp.ACHIEVEMENTS.items():
        have = stats.get(stat, 0)
        lines.append(f"{'✅' if key in got else '🔒'} **{title}** · {min(have, need)}/{need} {stat.replace('_', ' ')}")
    return _notice(ctx, "prof", "\n".join(lines), "prof", "", "Back to profile")


# ---- rankings ----
def _rank_arg(metric: int, local: int) -> str:
    return f"m{metric}.l{local}"


async def screen_ranks(ctx: Ctx, metric: int = 0, local: int = 1) -> discord.ui.LayoutView:
    metric = metric if 0 <= metric < len(METRICS) else 0
    guild = ctx.i.guild
    local = 1 if (local and guild is not None) else 0
    key, title = METRICS[metric]
    member_ids = [m.id for m in guild.members if not m.bot] if local else None
    rows = await cp.leaderboard(key, ctx.clone_id, member_ids=member_ids, limit=10)
    where = f"in {discord.utils.escape_markdown(guild.name)}" if local else "across every server"
    lines = [f"## 🏆 {title}", f"-# Permanent cards only (rentals don't count) · {where}"]
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    for r in rows:
        mark = medals.get(r["rank"]) or "`#" + str(r["rank"]) + "`"
        lines.append(f"{mark} {_display_name(ctx, r['user_id'])} · **{r['score']:,}**")
    if not rows:
        lines.append("Nobody is on this board yet.")
    arg = _rank_arg(metric, local)
    sel = discord.ui.ActionRow(HubSelect(
        "rankm", ctx.clone_id, ctx.guild_id, arg,
        [discord.SelectOption(label=t, value=str(i), default=(i == metric)) for i, (_k, t) in enumerate(METRICS)], "Ranking"))
    scope = discord.ui.ActionRow(
        _btn(ctx, "ranks", _rank_arg(metric, 1), "This server", "🏠", discord.ButtonStyle.primary if local else discord.ButtonStyle.secondary,
             disabled=guild is None),
        _btn(ctx, "ranks", _rank_arg(metric, 0), "Every server", "🌍", discord.ButtonStyle.secondary if local else discord.ButtonStyle.primary))
    return _shell(ctx, "ranks", "\n".join(lines), [sel, scope])


# ---------------------------------------------------------------- routing
async def route(ctx: Ctx, act: str, arg: str, value: Optional[str]):
    """Returns a LayoutView, or None when the action opened a modal itself."""
    from discord_bot.cogs import _views_card_trade as vt      # lazy: that module imports this one
    if act in vt.ACTS:
        return await vt.route(ctx, act, arg, value)
    from discord_bot.cogs import _views_card_chief as vc
    if act in vc.ACTS:
        return await vc.route(ctx, act, arg, value)
    from discord_bot.cogs import _views_card_admin as va
    if act in va.ACTS or (act == "nav" and value == "admin"):
        return await va.route(ctx, "admin" if act == "nav" else act, arg, value)
    if act == "nav":
        screens = {"home": screen_home, "store": screen_store, "col": screen_col, "prof": screen_prof, "ranks": screen_ranks,
                   "trade": vt.screen_trade, "chief": vc.screen_chief}
        return await screens.get(value, screen_home)(ctx)
    if act == "home":
        return await screen_home(ctx)
    if act == "store":
        return await screen_store(ctx, _int_arg(arg, "p"))
    if act == "pick":
        return await screen_confirm(ctx, int(value), _int_arg(arg, "p"))
    if act == "buyok":
        return await do_buy(ctx, _int_arg(arg, "o"), _int_arg(arg, "p"))
    if act == "col":
        return await screen_col(ctx, _int_arg(arg, "p"), _int_arg(arg, "s"), _int_arg(arg, "r"))
    if act == "colsort":
        return await screen_col(ctx, 0, int(value), _int_arg(arg, "r"))
    if act == "colrar":
        return await screen_col(ctx, 0, _int_arg(arg, "s"), int(value))
    if act == "inspect":
        return await screen_detail(ctx, int(value), arg)
    back = re.sub(r"^c\d+\.(?:i\d+\.)?", "", arg or "")
    if act == "boost":
        return await do_boost(ctx, _int_arg(arg, "c"), _int_arg(arg, "i"), back)
    if act == "feature":
        return await do_feature(ctx, _int_arg(arg, "c"), back)
    if act == "prof":
        return await screen_prof(ctx)
    if act == "priv":
        return await do_privacy(ctx, arg)
    if act == "ach":
        return await screen_ach(ctx)
    if act == "lookup":
        await ctx.i.response.send_modal(LookupModal(ctx.clone_id, ctx.guild_id))
        return None
    if act == "ranks":
        return await screen_ranks(ctx, _int_arg(arg, "m"), _int_arg(arg, "l", 1))
    if act == "rankm":
        return await screen_ranks(ctx, int(value), _int_arg(arg, "l", 1))
    return await screen_home(ctx)


async def dispatch(interaction: discord.Interaction, act: str, clone_id, guild_id: int, arg: str, value: Optional[str]) -> None:
    """Every hub click lands here. Never raises: a stale or broken control gets a private note and the hub stays usable."""
    ctx = Ctx(interaction, clone_id, guild_id)
    from discord_bot.cogs import _views_card_trade as vt
    from discord_bot.cogs import _views_card_admin as va
    from discord_bot.cogs import _views_card_chief as vc
    guard = (ctx.user_id, act, arg, value) if act in ("buyok", "boost") or act in vt.GUARDED or act in va.GUARDED or act in vc.GUARDED else None
    if guard is not None:
        if guard in _inflight:
            if not interaction.response.is_done():
                await interaction.response.defer()
            return
        _inflight.add(guard)
    try:
        if act != "lookup" and act not in vt.MODAL_ACTS and act not in va.MODAL_ACTS:
            await interaction.response.defer()
        view = await route(ctx, act, arg, value)
        if view is not None:
            await interaction.edit_original_response(view=view)
    except discord.NotFound:
        logger.debug("[card-hub] interaction expired", exc_info=True)
    except Exception:
        logger.warning("[card-hub] action %s failed", act, exc_info=True)
        try:
            msg = "⚠️ Something went wrong. Your coins and cards are safe. Try again."
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except Exception:
            pass
    finally:
        if guard is not None:
            _inflight.discard(guard)


async def open_card_hub(interaction: discord.Interaction, guild_id: int, clone_id) -> None:
    """Opens the hub as a private message from the leaderboard's Cards button."""
    ctx = Ctx(interaction, clone_id, guild_id)
    try:
        await interaction.response.defer(ephemeral=True)
        await interaction.followup.send(view=await screen_home(ctx), ephemeral=True)
    except Exception:
        logger.warning("[card-hub] open failed", exc_info=True)
        try:
            await interaction.followup.send("⚠️ The card hub is unavailable right now. Try again in a moment.", ephemeral=True)
        except Exception:
            pass
