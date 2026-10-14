# path: discord_bot/cogs/_views_premium_v2.py

"""Premium wizard (Components V2): what `/premium` and every "Go Premium" button open.

Replaces the old one-wall-of-text pitch followed by six loosely grouped plan buttons. Three pages in one
ephemeral message, edited in place:

  home     status, a clearly grouped plan picker (💎 Premium: monthly / yearly / lifetime / first-time offer,
           ✨ Plus: monthly / yearly), and two buttons to learn more. Tapping a plan goes straight to the
           pay card -- the same one tap as before, so organising the information did not add a step.
  perks    every perk, by category (dropdown), each with what Free / Premium / Plus get.
  compare  one table of every plain number (AI chat, scam checks, forms, music ...).

Nothing about payment changed: a plan button runs _views_premium._start_plan_checkout (or the 50% offer's
premium_discount.start_discount_checkout), which end in payments_manual.start_dual_mode_payment exactly
like before, so Gumroad / Paystack routing, Verify and the unlock handlers are untouched. What changed is
what the buyer sees on the way there:
  * the plan screen tells them, before they tap, which checkout they will get (and that the first-time offer
    is Gumroad-only), and
  * the pay card (see _views_premium._start_plan_checkout) is short enough that the pay buttons are on screen.

Every number shown comes from modules/premium_perks.py, which reads it from the code that enforces it.

The view is not persistent (like the plan picker it replaces): it is ephemeral and times out after
TIMEOUT_SECONDS, then says so instead of leaving dead buttons. The entry points that ARE persistent --
/premium, PremiumPitchButton, PremiumSubscribeButton -- are unchanged.
"""

import logging
from dataclasses import dataclass
from typing import Optional

import discord

import config
from database import db
from modules import premium_perks as pp

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 600
_ALL = "__all"          # the dropdown's "Overview" value
OFFER = "offer"         # not a payment_type: the 50% first-time Yearly offer (premium_discount.py)


# ── status ───────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class PremiumStatus:
    active: bool = False
    lifetime: bool = False
    expires_ts: Optional[int] = None
    plus_ts: Optional[int] = None
    unknown: bool = False      # the lookup failed: show the plans anyway rather than an error


async def load_status(guild_id: int, clone_id) -> PremiumStatus:
    from discord_bot.cogs._views_premium import _is_lifetime_expiry
    try:
        row = await db.get_guild_premium(guild_id, clone_id=clone_id)
        if not row or not await db.is_guild_premium_active(guild_id, clone_id):
            return PremiumStatus()
        plus_ts = None
        if row.get("plus_expires_at") and await db.is_guild_plus_active(guild_id, clone_id):
            plus_ts = int(row["plus_expires_at"].timestamp())
        return PremiumStatus(
            active=True, lifetime=_is_lifetime_expiry(row["expires_at"]),
            expires_ts=int(row["expires_at"].timestamp()), plus_ts=plus_ts,
        )
    except Exception:
        logger.exception("[premium] status lookup failed for guild %s", guild_id)
        return PremiumStatus(unknown=True)


def status_text(s: PremiumStatus) -> str:
    if s.unknown:
        return "Couldn't check this server's Premium status just now — you can still pick a plan below."
    if not s.active:
        return "Not active on this server yet."
    if s.lifetime:
        out = "♾️ **Lifetime Premium is active** on this server. Nothing more to pay, ever."
    else:
        out = (f"✅ **Premium is active** on this server until <t:{s.expires_ts}:D> (<t:{s.expires_ts}:R>). "
               "Renew below to add more time.")
    if s.plus_ts:
        out += f"\n✨ **Premium Plus is active** until <t:{s.plus_ts}:D> (<t:{s.plus_ts}:R>)."
    return out


def payment_hint(mode: str) -> str:
    """What the buyer is about to see, matching the admin's payment mode (db.get_payment_mode)."""
    if mode == "gumroad":
        return "-# 🌍 You'll pay with Gumroad (card / PayPal) — cancel anytime."
    if mode == "auto":
        return "-# 💳 Next you'll get a **Pay Now** button — tap it, then **Verify**."
    return ("-# 🌍 Global: Gumroad, cancel anytime · 🇬🇭 Ghana: Paystack, 30 days per payment — "
            "you pick on the next screen.")


async def _payment_mode(clone_id) -> str:
    try:
        return await db.get_payment_mode(clone_id)
    except Exception:
        logger.exception("[premium] payment-mode lookup failed")
        return "split"      # the documented default: both checkouts are offered


# ── compare table (pure) ─────────────────────────────────────────────────

def render_compare_table(rows) -> str:
    """Monospace Free / Prem / Plus table, narrow enough for a phone."""
    w = max(len(r[0]) for r in rows)
    head = f"{'':<{w}}  {'Free':>4}  {'Prem':>4}  {'Plus':>4}"
    body = [f"{label:<{w}}  {free:>4}  {prem:>4}  {plus:>4}" for label, free, prem, plus in rows]
    return "```\n" + "\n".join([head, *body]) + "\n```"


def perk_lines(perk: pp.Perk) -> list:
    """Two lines per perk: what it is, then what each tier gets."""
    tiers = f"Free: {perk.free or 'not included'} · 💎 Premium: {perk.premium or 'included'}"
    if perk.plus:
        tiers += f" · ✨ Plus: {perk.plus}"
    return [f"{perk.emoji} **{perk.title}** — {perk.blurb}", f"-# {tiers}"]


# ── the view ─────────────────────────────────────────────────────────────

def _premium_plans():
    pct = pp.yearly_savings_pct()
    return (
        ("premium", f"Monthly — ${config.PREMIUM_FEE_USD:g}", discord.ButtonStyle.secondary, "🗓️"),
        ("premium_yearly", f"Yearly — ${config.PREMIUM_YEARLY_FEE_USD:g} (save {pct}%)",
         discord.ButtonStyle.primary, "💎"),
        ("premium_lifetime", f"Lifetime — ${config.PREMIUM_LIFETIME_FEE_USD:g}", discord.ButtonStyle.success, "♾️"),
    )


def _plus_plans():
    return (
        ("premium_plus", f"Plus Monthly — ${config.PREMIUM_PLUS_FEE_USD:g}", discord.ButtonStyle.secondary, "✨"),
        ("premium_plus_yearly", f"Plus Yearly — ${config.PREMIUM_PLUS_YEARLY_FEE_USD:g}",
         discord.ButtonStyle.primary, "✨"),
    )


class PremiumWizardView(discord.ui.LayoutView):
    def __init__(self, guild, clone_id, buyer_id: int, status: PremiumStatus, mode: str = "split",
                 page: str = "home", category: Optional[str] = None):
        super().__init__(timeout=TIMEOUT_SECONDS)
        self.guild = guild
        self.clone_id = clone_id
        self.buyer_id = buyer_id
        self.status = status
        self.mode = mode
        self.page = page
        self.category = category
        self.message = None          # the sent message, so on_timeout can retire it
        self._busy = False           # a checkout is being opened: ignore a second tap on a plan
        self.render()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.buyer_id:
            await interaction.response.send_message(
                "This menu isn't for you — run **/premium** to open your own.", ephemeral=True)
            return False
        return True

    async def on_timeout(self) -> None:
        self.clear_items()
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay("⌛ This menu timed out. Run **/premium** to pick a plan."),
            accent_colour=discord.Color.light_grey()))
        try:
            if self.message is not None:
                await self.message.edit(view=self)
        except Exception:
            logger.debug("[premium] couldn't retire the timed-out menu", exc_info=True)

    # ── rendering ──
    def render(self) -> None:
        self.clear_items()
        build = {"home": self._home, "perks": self._perks, "compare": self._compare}[self.page]
        children, accent = build()
        self.add_item(discord.ui.Container(*children, accent_colour=accent))

    def _button(self, label, style, emoji, callback) -> discord.ui.Button:
        btn = discord.ui.Button(label=label, style=style, emoji=emoji)
        btn.callback = callback
        return btn

    def _row(self, *buttons) -> discord.ui.ActionRow:
        row = discord.ui.ActionRow()
        for b in buttons:
            row.add_item(b)
        return row

    def _plan_button(self, plan, label, style, emoji) -> discord.ui.Button:
        async def cb(interaction: discord.Interaction, _plan=plan):
            await self._checkout(interaction, _plan)
        return self._button(label, style, emoji, cb)

    def _home(self):
        import premium_discount as pd
        s = self.status
        T, S = discord.ui.TextDisplay, discord.ui.Separator
        kids = [
            T("\n".join([
                "## 💎 PrimeBot Premium",
                status_text(s),
                "-# One plan covers your whole server — anyone in it can pay.",
                "",
                f"💎 **Premium** — all {len(pp.perks())} perks, for everyone in the server.",
                f"✨ **Premium Plus** — everything in Premium, plus {pp.plus_extras()}.",
            ])),
            S(),
        ]
        if s.lifetime:
            kids.append(T("♾️ **Lifetime Premium** — you already have every Premium perk, forever."))
        else:
            kids.append(T("**💎 Premium** — pick how you'd like to pay"))
            kids.append(self._row(*(self._plan_button(*p) for p in _premium_plans())))
            kids.append(self._row(self._plan_button(
                OFFER, f"First-time offer — Yearly ${pd.discounted_usd():g} (50% off)",
                discord.ButtonStyle.success, "🎟️")))
            kids.append(T("-# 🎟️ The first-time offer is for a first Premium payment only, "
                          "and checks out on Gumroad (card / PayPal)."))
        kids += [
            S(),
            T("**✨ Premium Plus** — Premium and higher AI limits"),
            self._row(*(self._plan_button(*p) for p in _plus_plans())),
            S(),
            self._row(
                self._button("What's included", discord.ButtonStyle.secondary, "📋", self._to("perks")),
                self._button("Compare plans", discord.ButtonStyle.secondary, "⚖️", self._to("compare"))),
            T("\n".join([payment_hint(self.mode),
                         "-# Not included: clones & temporary XP boosts. Anything you bought separately stays yours."])),
        ]
        return kids, discord.Color.gold()

    def _perks(self):
        T, S = discord.ui.TextDisplay, discord.ui.Separator
        cats = pp.CATEGORIES
        if self.category is None:
            lines = ["## 📋 What's included", "Everything Premium unlocks, by category. Free keeps the core of each feature.", ""]
            for c in cats:
                titles = " · ".join(p.title for p in pp.perks_in(c.key))
                lines.append(f"{c.emoji} **{c.label}** — {titles}")
            lines += ["", "-# Pick a category below to see what Free, Premium and Plus each get."]
        else:
            cat = pp.CATEGORY_BY_KEY[self.category]
            lines = [f"## {cat.emoji} {cat.label}"]
            for p in pp.perks_in(cat.key):
                lines += ["", *perk_lines(p)]

        select = discord.ui.Select(
            placeholder="Browse by category",
            options=[discord.SelectOption(label="Overview", value=_ALL, emoji="📋", default=self.category is None),
                     *(discord.SelectOption(label=c.label, value=c.key, emoji=c.emoji,
                                            description=f"{len(pp.perks_in(c.key))} perks",
                                            default=self.category == c.key) for c in cats)])

        async def pick(interaction: discord.Interaction):
            val = select.values[0]
            await self._show(interaction, "perks", None if val == _ALL else val)
        select.callback = pick
        row = discord.ui.ActionRow()
        row.add_item(select)
        return [T("\n".join(lines)), S(), row, self._nav("compare")], discord.Color.blurple()

    def _compare(self):
        T, S = discord.ui.TextDisplay, discord.ui.Separator
        only = " · ".join(pp.premium_only_titles())
        text = "\n".join([
            "## ⚖️ Free vs Premium vs Plus",
            render_compare_table(pp.limit_rows()),
            f"✅ **Premium & Plus only:** {only}",
            f"✨ **Plus adds:** {pp.plus_extras()}. Everything else is the same as Premium.",
            f"-# 💎 ${config.PREMIUM_FEE_USD:g}/month · ${config.PREMIUM_YEARLY_FEE_USD:g}/year · "
            f"${config.PREMIUM_LIFETIME_FEE_USD:g} lifetime   ✨ ${config.PREMIUM_PLUS_FEE_USD:g}/month · "
            f"${config.PREMIUM_PLUS_YEARLY_FEE_USD:g}/year",
        ])
        return [T(text), S(), self._nav("perks")], discord.Color.blurple()

    def _nav(self, other: str) -> discord.ui.ActionRow:
        """Back to the plans (the one obvious next step) plus a hop to the sibling info page."""
        sibling = {"perks": ("What's included", "📋"), "compare": ("Compare plans", "⚖️")}[other]
        return self._row(
            self._button("Back to plans", discord.ButtonStyle.success, "💳", self._to("home")),
            self._button(sibling[0], discord.ButtonStyle.secondary, sibling[1], self._to(other)))

    # ── navigation ──
    def _to(self, page: str):
        async def cb(interaction: discord.Interaction):
            await self._show(interaction, page, None)
        return cb

    async def _show(self, interaction: discord.Interaction, page: str, category: Optional[str]) -> None:
        self.page, self.category = page, category
        self.render()
        await interaction.response.edit_message(view=self)

    # ── paying ──
    async def _checkout(self, interaction: discord.Interaction, plan: str) -> None:
        """Hands off to the existing checkout. Anything that goes wrong tells the buyer instead of leaving
        the 'thinking' spinner hanging."""
        if self._busy:
            await interaction.response.send_message("Opening your checkout — one moment…", ephemeral=True)
            return
        self._busy = True
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
            if plan == OFFER:
                import premium_discount as pd
                if (await load_status(self.guild.id, self.clone_id)).lifetime:
                    await interaction.followup.send("♾️ This server already has **Lifetime Premium**.", ephemeral=True)
                    return
                await pd.start_discount_checkout(interaction, self.guild, self.clone_id)
            else:
                from discord_bot.cogs._views_premium import _start_plan_checkout
                await _start_plan_checkout(interaction, self.guild, self.clone_id, plan)
        except Exception:
            logger.exception("[premium] couldn't open checkout for plan %s in guild %s", plan, self.guild.id)
            try:
                await interaction.followup.send(
                    "Couldn't open the checkout just now — tap the plan again in a moment.", ephemeral=True)
            except Exception:
                pass
        finally:
            self._busy = False


async def open_premium_wizard(interaction: discord.Interaction, guild, clone_id, *, page: str = "home") -> None:
    """Call AFTER interaction.response.defer(ephemeral=True) -- the same contract as send_premium_pitch."""
    status = await load_status(guild.id, clone_id)
    mode = await _payment_mode(clone_id)
    view = PremiumWizardView(guild, clone_id, interaction.user.id, status, mode, page=page)
    view.message = await interaction.followup.send(view=view, ephemeral=True)
